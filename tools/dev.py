"""Deterministic, worktree-aware development workflow coordinator."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = ROOT / ".run"
STACK_ENV_FILE = RUN_DIR / "stack.env"
PROCESS_FILE = RUN_DIR / "processes.json"
PLAYWRIGHT_VERSION = "0.1.17"
STANDARD_PORTS = (8000, 5432, 8080, 3000, 4317, 4318)
PROCESS_COMMANDS = {
    "app": ("uv", "run", "litestar", "run"),
    "worker": ("uv", "run", "litestar", "workers", "run"),
}
SAFE_SESSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ToolError(RuntimeError):
    """Expected user-facing developer-tool failure."""


@dataclass(frozen=True, slots=True)
class StackConfig:
    root: str
    project: str
    app_port: int
    postgres_port: int
    nginx_port: int
    grafana_port: int
    otlp_grpc_port: int
    otlp_http_port: int

    @property
    def ports(self) -> tuple[int, ...]:
        return (
            self.app_port,
            self.postgres_port,
            self.nginx_port,
            self.grafana_port,
            self.otlp_grpc_port,
            self.otlp_http_port,
        )

    @property
    def app_url(self) -> str:
        return f"http://127.0.0.1:{self.nginx_port}"

    @property
    def grafana_url(self) -> str:
        return f"http://127.0.0.1:{self.grafana_port}"


def notice(message: str) -> None:
    print(f"[lsstack] {message}", flush=True)


def run(
    command: list[str],
    *,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=capture,
        check=check,
    )


def git(*arguments: str, cwd: Path = ROOT, capture: bool = True) -> str:
    result = run(["git", *arguments], cwd=cwd, capture=capture)
    return result.stdout.strip() if capture else ""


def read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", maxsplit=1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value.startswith(("'", '"')):
            value = value[1:-1]
        values[key.strip()] = value
    return values


def write_dotenv(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(f"{key}={value}\n" for key, value in values.items())
    path.write_text(content)


def ensure_local_env() -> None:
    target = ROOT / ".env"
    if target.exists():
        return
    source = ROOT / ".env.example"
    if not source.exists():
        raise ToolError(".env.example is missing; cannot create local configuration.")
    shutil.copyfile(source, target)
    notice("Created .env from .env.example.")


def setup() -> None:
    ensure_local_env()
    notice("Synchronizing the frozen dependency set.")
    run(["uv", "sync", "--all-groups", "--frozen"])
    notice("Development environment is ready.")


def _primary_checkout() -> bool:
    common = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir"))
    return common.parent.resolve() == ROOT.resolve()


def _project_name() -> str:
    digest = hashlib.sha256(str(ROOT.resolve()).encode()).hexdigest()[:10]
    return f"lsstack-{digest}"


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _preferred_ports() -> tuple[int, ...]:
    override = os.environ.get("LSSTACK_PORT_BASE")
    if override is not None:
        try:
            base = int(override)
        except ValueError as error:
            raise ToolError("LSSTACK_PORT_BASE must be an integer.") from error
        if not 1024 <= base <= 65529:
            raise ToolError("LSSTACK_PORT_BASE must be between 1024 and 65529.")
        return tuple(base + offset for offset in range(6))
    if _primary_checkout():
        return STANDARD_PORTS
    digest = hashlib.sha256(str(ROOT.resolve()).encode()).hexdigest()
    base = 20_000 + (int(digest[:8], 16) % 2_000) * 10
    return tuple(base + offset for offset in range(6))


def _allocate_ports() -> tuple[int, ...]:
    preferred = _preferred_ports()
    if all(_port_is_free(port) for port in preferred):
        return preferred
    if os.environ.get("LSSTACK_PORT_BASE") is not None:
        busy = [str(port) for port in preferred if not _port_is_free(port)]
        raise ToolError(
            "Requested LSSTACK_PORT_BASE includes busy ports: " + ", ".join(busy)
        )
    start = 40_000 + (int(hashlib.sha256(str(ROOT).encode()).hexdigest()[:6], 16) % 500)
    for block in range(500):
        base = start + block * 10
        candidate = tuple(base + offset for offset in range(6))
        if candidate[-1] > 65_535:
            break
        if all(_port_is_free(port) for port in candidate):
            notice(
                "Preferred ports were busy; selected fallback block "
                f"{candidate[0]}-{candidate[-1]}."
            )
            return candidate
    raise ToolError(
        "Unable to find a free six-port block. Set LSSTACK_PORT_BASE explicitly."
    )


def _config_from_values(values: dict[str, str]) -> StackConfig:
    try:
        config = StackConfig(
            root=values["LSSTACK_ROOT"],
            project=values["COMPOSE_PROJECT_NAME"],
            app_port=int(values["APP_PORT"]),
            postgres_port=int(values["POSTGRES_PORT"]),
            nginx_port=int(values["NGINX_PORT"]),
            grafana_port=int(values["GRAFANA_PORT"]),
            otlp_grpc_port=int(values["OTLP_GRPC_PORT"]),
            otlp_http_port=int(values["OTLP_HTTP_PORT"]),
        )
    except (KeyError, ValueError) as error:
        raise ToolError(
            f"{STACK_ENV_FILE} is incomplete or malformed; remove it and retry."
        ) from error
    if Path(config.root).resolve() != ROOT.resolve():
        raise ToolError(
            f"{STACK_ENV_FILE} belongs to {config.root}, not the current worktree."
        )
    return config


def load_config(*, create: bool = True) -> StackConfig | None:
    if STACK_ENV_FILE.exists():
        return _config_from_values(read_dotenv(STACK_ENV_FILE))
    if not create:
        return None
    ports = _allocate_ports()
    config = StackConfig(
        root=str(ROOT.resolve()),
        project=_project_name(),
        app_port=ports[0],
        postgres_port=ports[1],
        nginx_port=ports[2],
        grafana_port=ports[3],
        otlp_grpc_port=ports[4],
        otlp_http_port=ports[5],
    )
    write_dotenv(
        STACK_ENV_FILE,
        {
            "LSSTACK_ROOT": config.root,
            "COMPOSE_PROJECT_NAME": config.project,
            "APP_PORT": str(config.app_port),
            "POSTGRES_PORT": str(config.postgres_port),
            "NGINX_PORT": str(config.nginx_port),
            "GRAFANA_PORT": str(config.grafana_port),
            "OTLP_GRPC_PORT": str(config.otlp_grpc_port),
            "OTLP_HTTP_PORT": str(config.otlp_http_port),
        },
    )
    return config


def _url_port(value: str, port: int) -> str:
    parsed = urlsplit(value)
    hostname = parsed.hostname or "127.0.0.1"
    username = parsed.username or ""
    password = parsed.password
    credentials = ""
    if username:
        credentials = quote(username, safe="")
        if password is not None:
            credentials += ":" + quote(password, safe="")
        credentials += "@"
    netloc = f"{credentials}{hostname}:{port}"
    return urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )


def stack_environment(config: StackConfig) -> dict[str, str]:
    values = {**os.environ, **read_dotenv(ROOT / ".env")}
    values.update(read_dotenv(STACK_ENV_FILE))
    for key in (
        "DATABASE_URL",
        "ADMIN_DATABASE_URL",
        "TEST_DATABASE_URL",
        "SAQ_DATABASE_URL",
    ):
        if key in values:
            values[key] = _url_port(values[key], config.postgres_port)
    values.update(
        {
            "PUBLIC_BASE_URL": config.app_url,
            "PROXY_BASE_URL": f"http://host.docker.internal:{config.app_port}",
            "OTLP_ENDPOINT": f"http://127.0.0.1:{config.otlp_grpc_port}",
            "NGINX_UPSTREAM_PORT": str(config.app_port),
            "COMPOSE_PROJECT_NAME": config.project,
        }
    )
    return values


def compose(
    config: StackConfig,
    *arguments: str,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return run(
        [
            "docker",
            "compose",
            "--project-name",
            config.project,
            *arguments,
        ],
        env=stack_environment(config),
        capture=capture,
        check=check,
    )


def _docker_ready() -> None:
    if shutil.which("docker") is None:
        raise ToolError("Docker is not installed or is not on PATH.")
    result = run(["docker", "info"], capture=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ToolError(f"Docker is unavailable: {detail}")
    result = run(["docker", "compose", "version"], capture=True, check=False)
    if result.returncode != 0:
        raise ToolError("Docker Compose v2 is required.")


def _load_processes() -> dict[str, Any]:
    if not PROCESS_FILE.exists():
        return {"root": str(ROOT.resolve()), "processes": {}}
    try:
        payload = json.loads(PROCESS_FILE.read_text())
    except json.JSONDecodeError as error:
        raise ToolError(f"{PROCESS_FILE} contains invalid JSON.") from error
    if not isinstance(payload, dict):
        raise ToolError(f"{PROCESS_FILE} must contain a JSON object.")
    typed_payload = cast("dict[str, Any]", payload)
    if typed_payload.get("root") != str(ROOT.resolve()):
        raise ToolError(f"{PROCESS_FILE} belongs to another worktree.")
    if not isinstance(typed_payload.get("processes"), dict):
        raise ToolError(f"{PROCESS_FILE} has an invalid process map.")
    return typed_payload


def _save_processes(payload: dict[str, Any]) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    PROCESS_FILE.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_command(pid: int) -> str:
    result = run(
        ["ps", "-p", str(pid), "-o", "command="],
        capture=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _pid_started_signature(pid: int) -> str:
    result = run(
        ["ps", "-p", str(pid), "-o", "lstart="],
        capture=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def pid_owned(name: str, metadata: dict[str, Any]) -> bool:
    pid = metadata.get("pid")
    if not isinstance(pid, int) or not pid_alive(pid):
        return False
    started_signature = metadata.get("started_signature")
    if (
        not isinstance(started_signature, str)
        or not started_signature
        or _pid_started_signature(pid) != started_signature
    ):
        return False
    command = _pid_command(pid)
    expected = " ".join(PROCESS_COMMANDS[name][2:])
    return expected in command


def _tail(path: Path, lines: int = 80) -> str:
    if not path.exists():
        return "(log file does not exist)"
    content = path.read_text(errors="replace").splitlines()
    return "\n".join(content[-lines:]) or "(log file is empty)"


def _start_process(
    name: str,
    command: list[str],
    config: StackConfig,
    payload: dict[str, Any],
) -> str:
    processes: dict[str, Any] = payload["processes"]
    existing = processes.get(name)
    if isinstance(existing, dict):
        metadata = cast("dict[str, Any]", existing)
        pid = metadata.get("pid")
        if isinstance(pid, int) and pid_alive(pid):
            if not pid_owned(name, metadata):
                raise ToolError(
                    f"PID {pid} recorded for {name} is a foreign process; "
                    "refusing to replace or stop it."
                )
            return f"{name} already running as PID {pid}"
        processes.pop(name, None)
        _save_processes(payload)

    log_path = RUN_DIR / f"{name}.log"
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("a")
    process = subprocess.Popen(  # noqa: S603
        command,
        cwd=ROOT,
        env=stack_environment(config),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        text=True,
    )
    log_handle.close()
    started_signature = _pid_started_signature(process.pid)
    if not started_signature:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        raise ToolError(
            f"Could not record an ownership signature for {name} PID {process.pid}."
        )
    processes[name] = {
        "pid": process.pid,
        "pgid": os.getpgid(process.pid),
        "command": command,
        "started_signature": started_signature,
        "started_at": datetime.now(UTC).isoformat(),
        "log": str(log_path),
    }
    _save_processes(payload)
    time.sleep(0.75)
    if process.poll() is not None:
        processes.pop(name, None)
        _save_processes(payload)
        raise ToolError(
            f"{name} exited during startup with status {process.returncode}.\n"
            f"--- {name} log ---\n{_tail(log_path)}"
        )
    return f"started {name} as PID {process.pid}"


def _stop_process(
    name: str,
    payload: dict[str, Any],
    *,
    timeout_seconds: float = 10,
) -> str:
    processes: dict[str, Any] = payload["processes"]
    metadata = processes.get(name)
    if not isinstance(metadata, dict):
        return f"{name} is not recorded"
    typed_metadata = cast("dict[str, Any]", metadata)
    pid = typed_metadata.get("pid")
    pgid = typed_metadata.get("pgid")
    if not isinstance(pid, int):
        processes.pop(name, None)
        _save_processes(payload)
        return f"removed malformed {name} metadata"
    if not pid_alive(pid):
        processes.pop(name, None)
        _save_processes(payload)
        return f"removed stale {name} PID {pid}"
    if not pid_owned(name, typed_metadata):
        raise ToolError(
            f"PID {pid} recorded for {name} no longer matches; refusing to kill it."
        )
    target_group = pgid if isinstance(pgid, int) else os.getpgid(pid)
    os.killpg(target_group, signal.SIGTERM)
    deadline = time.monotonic() + timeout_seconds
    while pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if pid_alive(pid):
        os.killpg(target_group, signal.SIGKILL)
    processes.pop(name, None)
    _save_processes(payload)
    return f"stopped {name} PID {pid}"


def _processes_healthy(payload: dict[str, Any]) -> tuple[bool, str]:
    for name in ("app", "worker"):
        metadata = payload["processes"].get(name)
        if not isinstance(metadata, dict) or not pid_owned(
            name, cast("dict[str, Any]", metadata)
        ):
            return False, f"{name} process is not running"
    return True, "host processes are running"


def _wait_ready(
    config: StackConfig,
    payload: dict[str, Any],
    *,
    timeout_seconds: float = 60,
) -> None:
    url = config.app_url + "/readyz"
    deadline = time.monotonic() + timeout_seconds
    last_error = "not attempted"
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        healthy, detail = _processes_healthy(payload)
        if not healthy:
            raise ToolError(f"Startup failed: {detail}.")
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310
                if response.status == 200:
                    notice(f"Application became ready after {attempt} attempt(s).")
                    return
                last_error = f"HTTP {response.status}"
        except (urllib.error.URLError, TimeoutError) as error:
            last_error = str(error)
        time.sleep(1)
    raise ToolError(
        f"Application did not become ready at {url} within "
        f"{timeout_seconds:.0f}s (last error: {last_error})."
    )


def _compose_diagnostics(config: StackConfig) -> str:
    sections: list[str] = []
    for label, arguments in (
        ("Compose status", ("ps", "--all")),
        ("Compose logs", ("logs", "--no-color", "--tail", "100")),
    ):
        result = compose(config, *arguments, capture=True, check=False)
        output = (result.stdout + result.stderr).strip()
        sections.append(f"--- {label} ---\n{output or '(no output)'}")
    return "\n".join(sections)


def _host_diagnostics() -> str:
    return "\n".join(
        f"--- {name} log ---\n{_tail(RUN_DIR / f'{name}.log')}"
        for name in ("app", "worker")
    )


def up() -> None:
    setup()
    _docker_ready()
    config = load_config()
    assert config is not None
    notice(f"Using Compose project {config.project}.")
    notice("Starting PostgreSQL, Nginx, and observability services.")
    result = compose(config, "up", "-d", "--wait", capture=True, check=False)
    if result.returncode != 0:
        raise ToolError(
            "Docker Compose failed to become healthy.\n"
            + (result.stdout + result.stderr)
            + "\n"
            + _compose_diagnostics(config)
        )
    try:
        notice("Applying database migrations.")
        result = run(
            ["uv", "run", "alembic", "upgrade", "head"],
            env=stack_environment(config),
            capture=True,
            check=False,
        )
        if result.returncode != 0:
            raise ToolError(  # noqa: TRY301
                "Database migration failed.\n" + result.stdout + result.stderr
            )

        payload = _load_processes()
        app_command = [
            "uv",
            "run",
            "litestar",
            "run",
            "--host",
            "0.0.0.0",  # noqa: S104 - Nginx reaches this process from Docker.
            "--port",
            str(config.app_port),
        ]
        if os.environ.get("CI", "").casefold() not in {"1", "true"}:
            app_command.insert(4, "--reload")
        notice(_start_process("app", app_command, config, payload))
        notice(
            _start_process(
                "worker",
                ["uv", "run", "litestar", "workers", "run"],
                config,
                payload,
            )
        )
        _wait_ready(config, payload)
    except ToolError as error:
        raise ToolError(
            f"{error}\n{_host_diagnostics()}\n{_compose_diagnostics(config)}"
        ) from error
    show_urls(config)
    notice("Use `just logs app`, `just logs worker`, or `just diagnose` if needed.")


def down(*, volumes: bool = False) -> None:
    config = load_config(create=False)
    payload = _load_processes()
    for name in ("worker", "app"):
        notice(_stop_process(name, payload))
    if config is None:
        notice("No Compose stack has been allocated for this worktree.")
        return
    if shutil.which("docker") is None:
        notice("Docker is unavailable; host processes are stopped.")
        return
    arguments = ["down", "--remove-orphans"]
    if volumes:
        arguments.insert(1, "--volumes")
    result = compose(config, *arguments, capture=True, check=False)
    if result.returncode != 0:
        raise ToolError("Compose shutdown failed:\n" + result.stdout + result.stderr)
    notice(
        "Stopped Compose services"
        + (
            " and removed this project's volumes."
            if volumes
            else "; volumes preserved."
        )
    )


def show_urls(config: StackConfig | None = None, *, json_output: bool = False) -> None:
    resolved = config or load_config()
    assert resolved is not None
    values = {
        "application": resolved.app_url,
        "grafana": resolved.grafana_url,
        "postgres": f"127.0.0.1:{resolved.postgres_port}",
        "otlp_grpc": f"127.0.0.1:{resolved.otlp_grpc_port}",
        "compose_project": resolved.project,
    }
    if json_output:
        print(json.dumps(values, indent=2, sort_keys=True))
        return
    for label, value in values.items():
        print(f"{label}: {value}")


def _parse_compose_status(value: str) -> list[dict[str, Any]]:
    if not value:
        return []
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        records: list[dict[str, Any]] = []
        for line in value.splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ToolError(
                    "Docker Compose returned malformed status JSON."
                ) from error
            if not isinstance(record, dict):
                raise ToolError(
                    "Docker Compose status entries must be JSON objects."
                ) from None
            records.append(cast("dict[str, Any]", record))
        return records
    if isinstance(payload, list):
        return cast("list[dict[str, Any]]", payload)
    if isinstance(payload, dict):
        return [cast("dict[str, Any]", payload)]
    raise ToolError("Docker Compose returned an unsupported status JSON value.")


def _compose_summaries(
    records: list[dict[str, Any]],
) -> list[dict[str, str | int]]:
    fields = ("Service", "State", "Health", "Status", "Ports", "ExitCode")
    return [
        {
            field.casefold(): value
            for field in fields
            if isinstance((value := record.get(field)), (str, int))
        }
        for record in records
    ]


def status(*, json_output: bool = False) -> int:
    config = load_config(create=False)
    payload = _load_processes()
    host: dict[str, dict[str, Any]] = {}
    for name in ("app", "worker"):
        metadata = payload["processes"].get(name)
        typed_metadata = (
            cast("dict[str, Any]", metadata) if isinstance(metadata, dict) else None
        )
        host[name] = {
            "running": typed_metadata is not None and pid_owned(name, typed_metadata),
            "pid": typed_metadata.get("pid") if typed_metadata is not None else None,
        }
    compose_output = ""
    compose_records: list[dict[str, Any]] = []
    if config is not None and shutil.which("docker") is not None:
        result = compose(
            config,
            "ps",
            "--all",
            "--format",
            "json",
            capture=True,
            check=False,
        )
        compose_output = result.stdout.strip()
        if result.returncode == 0:
            compose_records = _parse_compose_status(compose_output)
    compose_summaries = _compose_summaries(compose_records)
    result_payload = {
        "allocated": config is not None,
        "config": asdict(config) if config is not None else None,
        "host": host,
        "compose": compose_summaries,
    }
    if json_output:
        print(json.dumps(result_payload, indent=2, sort_keys=True))
    else:
        if config is None:
            notice("No stack allocated for this worktree.")
        else:
            print(f"Compose project: {config.project}")
            show_urls(config)
        for name, details in host.items():
            state = "running" if details["running"] else "stopped"
            pid = f" (PID {details['pid']})" if details["pid"] else ""
            print(f"{name}: {state}{pid}")
        if compose_summaries:
            print("--- compose ---")
            for details in compose_summaries:
                health = (
                    f", health={details['health']}" if details.get("health") else ""
                )
                print(
                    f"{details.get('service', 'unknown')}: "
                    f"{details.get('state', 'unknown')}{health} "
                    f"({details.get('ports', 'no published ports')})"
                )
    return 0 if all(details["running"] for details in host.values()) else 1


def diagnose() -> None:
    config = load_config(create=False)
    status()
    print(_host_diagnostics())
    if config is not None and shutil.which("docker") is not None:
        print(_compose_diagnostics(config))


def logs(
    service: str,
    *,
    follow: bool,
    tail: int,
    since: str | None,
) -> None:
    if service in {"app", "worker"}:
        path = RUN_DIR / f"{service}.log"
        if not path.exists():
            raise ToolError(f"{path} does not exist; start the stack first.")
        command = ["tail", "-n", str(tail)]
        if follow:
            command.append("-F")
        command.append(str(path))
        run(command)
        return
    config = load_config(create=False)
    if config is None:
        raise ToolError("No Compose stack has been allocated for this worktree.")
    arguments = ["logs", "--no-color", "--tail", str(tail)]
    if follow:
        arguments.append("--follow")
    if since is not None:
        arguments.extend(["--since", since])
    if service != "all":
        arguments.append(service)
    compose(config, *arguments)


def migrate() -> None:
    config = load_config()
    assert config is not None
    run(
        ["uv", "run", "alembic", "upgrade", "head"],
        env=stack_environment(config),
    )


def revision(message: str) -> None:
    if not message.strip():
        raise ToolError("Migration message must not be empty.")
    config = load_config()
    assert config is not None
    run(
        [
            "uv",
            "run",
            "alembic",
            "revision",
            "--autogenerate",
            "-m",
            message,
        ],
        env=stack_environment(config),
    )


def seed_database() -> None:
    config = load_config()
    assert config is not None
    run(
        ["uv", "run", "python", "-m", "tools.seed"],
        env=stack_environment(config),
    )


def reset_database(confirmation: str) -> None:
    config = load_config()
    assert config is not None
    expected = f"reset-{config.project}"
    if confirmation != expected:
        raise ToolError(
            "Database reset removes only this worktree's local volumes, but it is "
            f"destructive. Retry with `just db-reset {expected}`."
        )
    down(volumes=True)
    PROCESS_FILE.unlink(missing_ok=True)
    up()


def execute_with_stack(
    command: list[str],
    *,
    overrides: list[str],
) -> int:
    config = load_config()
    assert config is not None
    environment = stack_environment(config)
    for override in overrides:
        if "=" not in override:
            raise ToolError(f"--set value must use KEY=VALUE, got {override!r}.")
        key, value = override.split("=", maxsplit=1)
        environment[key] = value
    return subprocess.run(  # noqa: S603
        command,
        cwd=ROOT,
        env=environment,
        check=False,
    ).returncode


def _duration_seconds(value: str) -> float:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd])", value)
    if match is None:
        raise ToolError("Duration must use a suffix such as 30s, 15m, 2h, or 1d.")
    amount = float(match.group(1))
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
    return amount * multiplier


def _lgtm_json(config: StackConfig, path: str, parameters: dict[str, str]) -> Any:
    if path.startswith("/loki/"):
        port = 3100
    elif path.startswith("/api/v1/"):
        port = 9090
    else:
        port = 3200
    url = f"http://127.0.0.1:{port}{path}"
    if parameters:
        url += "?" + urlencode(parameters)
    result = compose(
        config,
        "exec",
        "-T",
        "lgtm",
        "curl",
        "--fail",
        "--silent",
        "--show-error",
        url,
        capture=True,
        check=False,
    )
    if result.returncode != 0:
        raise ToolError(
            "LGTM query failed. Run `just obs check` and `just logs lgtm`.\n"
            + result.stdout
            + result.stderr
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ToolError("LGTM returned malformed JSON:\n" + result.stdout) from error


def _log_entries(payload: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not isinstance(payload, dict):
        return entries
    payload_object = cast("dict[str, Any]", payload)
    data = cast("dict[str, Any]", payload_object.get("data", {}))
    results = cast("list[dict[str, Any]]", data.get("result", []))
    for stream in results:
        labels = cast("dict[str, Any]", stream.get("stream", {}))
        values = cast("list[tuple[str, str]]", stream.get("values", []))
        for timestamp, line in values:
            entries.append({"timestamp": timestamp, "labels": labels, "line": line})
    entries.sort(key=lambda entry: entry["timestamp"])
    return entries


def observe_logs(
    config: StackConfig,
    *,
    query: str,
    since: str,
    limit: int,
    json_output: bool,
) -> list[dict[str, Any]]:
    start = datetime.now(UTC) - timedelta(seconds=_duration_seconds(since))
    payload = _lgtm_json(
        config,
        "/loki/api/v1/query_range",
        {
            "query": query,
            "limit": str(limit),
            "start": str(int(start.timestamp() * 1_000_000_000)),
        },
    )
    entries = _log_entries(payload)
    if json_output:
        print(json.dumps(entries, indent=2, sort_keys=True))
    elif not entries:
        notice(
            "No matching logs yet. Generate traffic, allow exporter ingestion, "
            "then retry."
        )
    else:
        for entry in entries:
            service = entry["labels"].get("service_name", "unknown")
            print(f"{entry['timestamp']} {service}: {entry['line']}")
    return entries


def observe_traces(
    config: StackConfig,
    *,
    query: str,
    limit: int,
    json_output: bool,
) -> list[dict[str, Any]]:
    payload = _lgtm_json(
        config,
        "/api/search",
        {"q": query, "limit": str(limit)},
    )
    payload_dict = cast("dict[str, Any]", payload) if isinstance(payload, dict) else {}
    typed_traces = cast("list[dict[str, Any]]", payload_dict.get("traces", []))
    if json_output:
        print(json.dumps(typed_traces, indent=2, sort_keys=True))
    elif not typed_traces:
        notice(
            "No matching traces yet. Generate traffic, allow exporter ingestion, "
            "then retry."
        )
    else:
        for trace in typed_traces:
            print(
                f"{trace.get('traceID', '?')} "
                f"{trace.get('rootServiceName', '?')} "
                f"{trace.get('rootTraceName', '?')} "
                f"duration_ms={trace.get('durationMs', '?')}"
            )
    return typed_traces


def observe_trace(
    config: StackConfig,
    trace_id: str,
    *,
    json_output: bool,
) -> Any:
    if not re.fullmatch(r"[0-9a-fA-F]{16,32}", trace_id):
        raise ToolError("Trace ID must be 16-32 hexadecimal characters.")
    payload = _lgtm_json(config, f"/api/traces/{trace_id}", {})
    print(json.dumps(payload, indent=2 if json_output else None, sort_keys=json_output))
    return payload


def observe_metrics(
    config: StackConfig,
    *,
    query: str,
    json_output: bool,
) -> list[dict[str, Any]]:
    payload = _lgtm_json(config, "/api/v1/query", {"query": query})
    if isinstance(payload, dict):
        data = cast(
            "dict[str, Any]",
            cast("dict[str, Any]", payload).get("data", {}),
        )
        results = cast("list[dict[str, Any]]", data.get("result", []))
    else:
        results = []
    if json_output:
        print(json.dumps(results, indent=2, sort_keys=True))
    elif not results:
        notice(
            "No matching metric series yet. Generate traffic, allow exporter "
            "ingestion, then retry."
        )
    else:
        for result in results:
            print(
                f"{json.dumps(result.get('metric', {}), sort_keys=True)} "
                f"value={result.get('value')}"
            )
    return results


def _trace_ids(value: Any) -> set[str]:
    serialized = json.dumps(value)
    return set(
        re.findall(
            r"(?:trace[_-]?id.{0,20}?)([0-9a-fA-F]{32})",
            serialized,
            flags=re.IGNORECASE,
        )
    )


def observe_correlate(
    config: StackConfig,
    request_id: str,
    *,
    json_output: bool,
) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", request_id):
        raise ToolError("Request ID contains unsupported characters.")
    payload = _lgtm_json(
        config,
        "/loki/api/v1/query_range",
        {
            "query": ('{service_name=~"lsstack|nginx"} |= ' + json.dumps(request_id)),
            "limit": "200",
        },
    )
    entries = _log_entries(payload)
    trace_ids = sorted(_trace_ids(payload))
    traces = [
        _lgtm_json(config, f"/api/traces/{trace_id}", {}) for trace_id in trace_ids
    ]
    result = {
        "request_id": request_id,
        "logs": entries,
        "trace_ids": trace_ids,
        "traces": traces,
    }
    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    print(f"request_id: {request_id}")
    print(f"log_entries: {len(entries)}")
    print(f"trace_ids: {', '.join(trace_ids) if trace_ids else '(none yet)'}")
    if not entries:
        notice("No correlated telemetry yet; wait for ingestion and retry.")


def observe_check(
    config: StackConfig,
    *,
    timeout_seconds: int,
    json_output: bool,
) -> int:
    deadline = time.monotonic() + timeout_seconds
    result = {
        "app_logs": False,
        "worker_logs": False,
        "nginx_logs": False,
        "metrics": False,
        "traces": False,
    }
    while time.monotonic() <= deadline:
        result["app_logs"] = bool(
            _log_entries(
                _lgtm_json(
                    config,
                    "/loki/api/v1/query_range",
                    {"query": '{service_name="lsstack"}', "limit": "1"},
                )
            )
        )
        result["nginx_logs"] = bool(
            _log_entries(
                _lgtm_json(
                    config,
                    "/loki/api/v1/query_range",
                    {"query": '{service_name="nginx"}', "limit": "1"},
                )
            )
        )
        result["worker_logs"] = bool(
            _log_entries(
                _lgtm_json(
                    config,
                    "/loki/api/v1/query_range",
                    {
                        "query": (
                            '{service_name="lsstack"} |= "outbox_message_acknowledged"'
                        ),
                        "limit": "1",
                    },
                )
            )
        )
        metric_payload = _lgtm_json(
            config,
            "/api/v1/query",
            {"query": "event_loop_lag_seconds"},
        )
        result["metrics"] = bool(metric_payload.get("data", {}).get("result", []))
        trace_payload = _lgtm_json(
            config,
            "/api/search",
            {"q": '{ resource.service.name = "lsstack" }', "limit": "1"},
        )
        result["traces"] = bool(trace_payload.get("traces", []))
        if all(result.values()):
            break
        if time.monotonic() <= deadline:
            time.sleep(1)
    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for name, seen in result.items():
            print(f"{name}: {'ok' if seen else 'missing'}")
    return 0 if all(result.values()) else 1


def browser_install() -> None:
    run(
        [
            "pnpm",
            "dlx",
            f"@playwright/cli@{PLAYWRIGHT_VERSION}",
            "install-browser",
            "chromium",
        ]
    )


def browser(session: str, arguments: list[str]) -> int:
    if SAFE_SESSION.fullmatch(session) is None:
        raise ToolError(
            "Browser session must use 1-64 letters, numbers, dots, dashes, or "
            "underscores and may not contain path separators."
        )
    if not arguments:
        raise ToolError("Pass a Playwright CLI command, such as `open <url>`.")
    config = load_config()
    assert config is not None
    output = ROOT / "output" / "playwright" / session
    output.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        raise ToolError("pnpm is unavailable; enter the Nix development shell.")
    result = subprocess.run(  # noqa: S603
        [
            pnpm,
            "dlx",
            f"@playwright/cli@{PLAYWRIGHT_VERSION}",
            f"-s={config.project}-{session}",
            *arguments,
        ],
        cwd=output,
        env=environment,
        check=False,
    )
    if result.returncode != 0:
        notice(
            "If Chromium is missing, run `just browser-install` once and retry. "
            f"Session artifacts remain under {output}."
        )
    return result.returncode


def _worktrees() -> list[dict[str, str]]:
    blocks = git("worktree", "list", "--porcelain").split("\n\n")
    parsed: list[dict[str, str]] = []
    for block in blocks:
        record: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            record[key] = value
        if record:
            parsed.append(record)
    return parsed


def clean_worktree(branch: str) -> None:  # noqa: PLR0912
    if branch in {"main", "master"}:
        raise ToolError("The primary branch may never be removed by this command.")
    target = next(
        (
            entry
            for entry in _worktrees()
            if entry.get("branch") == f"refs/heads/{branch}"
        ),
        None,
    )
    if target is None:
        raise ToolError(f"No worktree currently has branch {branch!r} checked out.")
    target_path = Path(target["worktree"]).resolve()
    if target_path == ROOT.resolve():
        raise ToolError("Run worktree cleanup from a different checkout.")

    pr_result = run(
        ["gh", "pr", "view", branch, "--json", "state,mergedAt,url"],
        capture=True,
        check=False,
    )
    if pr_result.returncode != 0:
        raise ToolError("Unable to verify the pull request:\n" + pr_result.stderr)
    pr = json.loads(pr_result.stdout)
    if pr.get("state") != "MERGED" or not pr.get("mergedAt"):
        raise ToolError("Refusing cleanup because the pull request is not merged.")

    if git("-C", str(target_path), "status", "--porcelain"):
        raise ToolError(f"Refusing cleanup because {target_path} has local changes.")

    process_file = target_path / ".run" / "processes.json"
    if process_file.exists():
        process_payload = json.loads(process_file.read_text())
        for name, metadata in process_payload.get("processes", {}).items():
            pid = metadata.get("pid")
            if isinstance(pid, int) and pid_alive(pid):
                raise ToolError(
                    f"Refusing cleanup because {name} PID {pid} is still running. "
                    f"Run `just -d {target_path} down` first."
                )

    stack_file = target_path / ".run" / "stack.env"
    if stack_file.exists():
        project = read_dotenv(stack_file).get("COMPOSE_PROJECT_NAME")
        if not project:
            raise ToolError(
                f"Refusing cleanup because {stack_file} has no Compose project name."
            )
        if shutil.which("docker") is None:
            raise ToolError(
                "Docker is unavailable, so the target Compose stack cannot be "
                "verified as stopped."
            )
        containers = run(
            [
                "docker",
                "ps",
                "--all",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--format",
                "{{.ID}} {{.Names}}",
            ],
            capture=True,
            check=False,
        )
        if containers.returncode != 0:
            raise ToolError(
                "Unable to verify the target Compose stack:\n"
                + containers.stdout
                + containers.stderr
            )
        if containers.stdout.strip():
            raise ToolError(
                f"Refusing cleanup because Compose project {project} still has "
                f"containers:\n{containers.stdout.strip()}\n"
                f"Run `just -d {target_path} down` first."
            )

    git("worktree", "remove", str(target_path), capture=False)
    git("branch", "-d", branch, capture=False)
    git("worktree", "prune", capture=False)
    notice(f"Removed merged worktree {target_path} and local branch {branch}.")
    notice("Remote branches are never deleted by this command.")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("setup")
    subparsers.add_parser("up")
    subparsers.add_parser("down")
    subparsers.add_parser("restart")
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--json", action="store_true")
    urls_parser = subparsers.add_parser("urls")
    urls_parser.add_argument("--json", action="store_true")
    subparsers.add_parser("diagnose")

    logs_parser = subparsers.add_parser("logs")
    logs_parser.add_argument("service", nargs="?", default="all")
    logs_parser.add_argument("--follow", "-f", action="store_true")
    logs_parser.add_argument("--tail", type=int, default=100)
    logs_parser.add_argument("--since")

    subparsers.add_parser("db-migrate")
    revision_parser = subparsers.add_parser("db-revision")
    revision_parser.add_argument("message")
    subparsers.add_parser("db-seed")
    reset_parser = subparsers.add_parser("db-reset")
    reset_parser.add_argument("confirmation", nargs="?", default="")

    exec_parser = subparsers.add_parser("exec")
    exec_parser.add_argument("--set", action="append", default=[])
    exec_parser.add_argument("exec_command", nargs=argparse.REMAINDER)

    obs_parser = subparsers.add_parser("obs")
    obs_parser.add_argument("--json", action="store_true")
    obs_subparsers = obs_parser.add_subparsers(dest="obs_command", required=True)
    obs_logs = obs_subparsers.add_parser("logs")
    obs_logs.add_argument(
        "--query",
        default='{service_name=~"lsstack|nginx"}',
    )
    obs_logs.add_argument("--since", default="15m")
    obs_logs.add_argument("--limit", type=int, default=100)
    obs_traces = obs_subparsers.add_parser("traces")
    obs_traces.add_argument(
        "--query",
        default='{ resource.service.name = "lsstack" }',
    )
    obs_traces.add_argument("--limit", type=int, default=20)
    obs_trace = obs_subparsers.add_parser("trace")
    obs_trace.add_argument("trace_id")
    obs_metrics = obs_subparsers.add_parser("metrics")
    obs_metrics.add_argument("--query", default="event_loop_lag_seconds")
    obs_correlate = obs_subparsers.add_parser("correlate")
    obs_correlate.add_argument("request_id")
    obs_check = obs_subparsers.add_parser("check")
    obs_check.add_argument("--timeout", type=int, default=60)

    subparsers.add_parser("browser-install")
    browser_parser = subparsers.add_parser("browser")
    browser_parser.add_argument("session")
    browser_parser.add_argument("browser_args", nargs=argparse.REMAINDER)

    cleanup_parser = subparsers.add_parser("worktree-clean")
    cleanup_parser.add_argument("branch")
    return parser


def main(  # noqa: PLR0911, PLR0912, PLR0915
    arguments: list[str] | None = None,
) -> int:
    args = _parser().parse_args(arguments)
    try:
        if args.command == "setup":
            setup()
        elif args.command == "up":
            up()
        elif args.command == "down":
            down()
        elif args.command == "restart":
            down()
            up()
        elif args.command == "status":
            return status(json_output=args.json)
        elif args.command == "urls":
            show_urls(json_output=args.json)
        elif args.command == "diagnose":
            diagnose()
        elif args.command == "logs":
            logs(
                args.service,
                follow=args.follow,
                tail=args.tail,
                since=args.since,
            )
        elif args.command == "db-migrate":
            migrate()
        elif args.command == "db-revision":
            revision(args.message)
        elif args.command == "db-seed":
            seed_database()
        elif args.command == "db-reset":
            reset_database(args.confirmation)
        elif args.command == "exec":
            if args.exec_command[:1] == ["--"]:
                args.exec_command = args.exec_command[1:]
            if not args.exec_command:
                raise ToolError("exec requires a command after `--`.")  # noqa: TRY301
            return execute_with_stack(args.exec_command, overrides=args.set)
        elif args.command == "obs":
            config = load_config(create=False)
            if config is None:
                raise ToolError(  # noqa: TRY301
                    "Start the stack before querying observability."
                )
            if args.obs_command == "logs":
                observe_logs(
                    config,
                    query=args.query,
                    since=args.since,
                    limit=args.limit,
                    json_output=args.json,
                )
            elif args.obs_command == "traces":
                observe_traces(
                    config,
                    query=args.query,
                    limit=args.limit,
                    json_output=args.json,
                )
            elif args.obs_command == "trace":
                observe_trace(config, args.trace_id, json_output=args.json)
            elif args.obs_command == "metrics":
                observe_metrics(
                    config,
                    query=args.query,
                    json_output=args.json,
                )
            elif args.obs_command == "correlate":
                observe_correlate(
                    config,
                    args.request_id,
                    json_output=args.json,
                )
            elif args.obs_command == "check":
                return observe_check(
                    config,
                    timeout_seconds=args.timeout,
                    json_output=args.json,
                )
        elif args.command == "browser-install":
            browser_install()
        elif args.command == "browser":
            return browser(args.session, args.browser_args)
        elif args.command == "worktree-clean":
            clean_worktree(args.branch)
    except ToolError as error:
        print(f"[lsstack] ERROR: {error}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        command = " ".join(str(part) for part in error.cmd)
        print(
            f"[lsstack] ERROR: command failed with status "
            f"{error.returncode}: {command}",
            file=sys.stderr,
        )
        return error.returncode or 1
    except OSError as error:
        print(f"[lsstack] ERROR: operating-system failure: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
