# pyright: reportPrivateUsage=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false

from __future__ import annotations

import json
import signal
import subprocess
from typing import TYPE_CHECKING

import pytest

from app.config import Settings
from tools import dev
from tools.seed import validate_seed_target

if TYPE_CHECKING:
    from pathlib import Path


def stack_config(root: Path) -> dev.StackConfig:
    return dev.StackConfig(
        root=str(root),
        project="lsstack-test",
        app_port=20_000,
        postgres_port=20_001,
        nginx_port=20_002,
        grafana_port=20_003,
        otlp_grpc_port=20_004,
        otlp_http_port=20_005,
    )


def test_linked_worktree_port_preference_is_stable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    monkeypatch.setattr(dev, "_primary_checkout", lambda: False)

    first = dev._preferred_ports()
    second = dev._preferred_ports()

    assert first == second
    assert len(set(first)) == 6
    assert first == tuple(range(first[0], first[0] + 6))


def test_primary_checkout_prefers_documented_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LSSTACK_PORT_BASE", raising=False)
    monkeypatch.setattr(dev, "_primary_checkout", lambda: True)
    assert dev._preferred_ports() == dev.STANDARD_PORTS


def test_explicit_port_block_refuses_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LSSTACK_PORT_BASE", "24000")
    monkeypatch.setattr(dev, "_port_is_free", lambda port: port != 24_003)
    with pytest.raises(dev.ToolError, match="busy ports: 24003"):
        dev._allocate_ports()


def test_allocator_uses_bounded_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LSSTACK_PORT_BASE", raising=False)
    monkeypatch.setattr(dev, "_preferred_ports", lambda: tuple(range(8000, 8006)))
    monkeypatch.setattr(
        dev,
        "_port_is_free",
        lambda port: port >= 40_010,
    )
    monkeypatch.setattr(dev.hashlib, "sha256", lambda _value: _FakeHash())

    assert dev._allocate_ports() == tuple(range(40_010, 40_016))


class _FakeHash:
    def hexdigest(self) -> str:
        return "000000" + ("0" * 58)


def test_load_config_persists_and_reuses_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / ".run"
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    monkeypatch.setattr(dev, "RUN_DIR", run_dir)
    monkeypatch.setattr(dev, "STACK_ENV_FILE", run_dir / "stack.env")
    monkeypatch.setattr(dev, "_allocate_ports", lambda: tuple(range(21_000, 21_006)))
    monkeypatch.setattr(dev, "_project_name", lambda: "lsstack-stable")

    first = dev.load_config()
    second = dev.load_config()

    assert first == second
    assert first is not None
    assert first.project == "lsstack-stable"
    assert first.ports == tuple(range(21_000, 21_006))


def test_process_metadata_for_another_worktree_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process_file = tmp_path / "processes.json"
    process_file.write_text(json.dumps({"root": "/elsewhere", "processes": {}}))
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    monkeypatch.setattr(dev, "PROCESS_FILE", process_file)

    with pytest.raises(dev.ToolError, match="another worktree"):
        dev._load_processes()


def test_pid_ownership_requires_matching_start_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dev, "pid_alive", lambda _pid: True)
    monkeypatch.setattr(dev, "_pid_command", lambda _pid: "litestar workers run")
    monkeypatch.setattr(dev, "_pid_started_signature", lambda _pid: "new process")

    assert (
        dev.pid_owned(
            "worker",
            {"pid": 123, "started_signature": "previous process"},
        )
        is False
    )
    assert (
        dev.pid_owned(
            "worker",
            {"pid": 123, "started_signature": "new process"},
        )
        is True
    )


def test_stop_refuses_a_reused_foreign_pid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(dev, "PROCESS_FILE", tmp_path / "processes.json")
    monkeypatch.setattr(dev, "pid_alive", lambda _pid: True)
    monkeypatch.setattr(dev, "pid_owned", lambda _name, _metadata: False)
    payload = {
        "root": str(dev.ROOT.resolve()),
        "processes": {"app": {"pid": 123, "pgid": 123}},
    }

    with pytest.raises(dev.ToolError, match="refusing to kill"):
        dev._stop_process("app", payload)


def test_stale_process_metadata_is_removed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process_file = tmp_path / "processes.json"
    monkeypatch.setattr(dev, "PROCESS_FILE", process_file)
    monkeypatch.setattr(dev, "pid_alive", lambda _pid: False)
    payload = {
        "root": str(dev.ROOT.resolve()),
        "processes": {"worker": {"pid": 456, "pgid": 456}},
    }

    result = dev._stop_process("worker", payload)

    assert "stale" in result
    assert payload["processes"] == {}


def test_process_cleanup_escalates_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signals: list[signal.Signals] = []
    monkeypatch.setattr(dev, "PROCESS_FILE", tmp_path / "processes.json")
    monkeypatch.setattr(dev, "pid_alive", lambda _pid: True)
    monkeypatch.setattr(dev, "pid_owned", lambda _name, _metadata: True)
    monkeypatch.setattr(
        dev.os,
        "killpg",
        lambda _pgid, sent_signal: signals.append(sent_signal),
    )
    payload = {
        "root": str(dev.ROOT.resolve()),
        "processes": {"app": {"pid": 123, "pgid": 123}},
    }

    dev._stop_process("app", payload, timeout_seconds=0)

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert payload["processes"] == {}


def test_start_reuses_an_owned_live_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(dev, "pid_alive", lambda _pid: True)
    monkeypatch.setattr(dev, "pid_owned", lambda _name, _metadata: True)
    payload = {
        "root": str(dev.ROOT.resolve()),
        "processes": {"app": {"pid": 123, "pgid": 123}},
    }

    result = dev._start_process(
        "app",
        ["uv", "run", "litestar", "run"],
        stack_config(tmp_path),
        payload,
    )

    assert result == "app already running as PID 123"


def test_browser_rejects_path_traversal() -> None:
    with pytest.raises(dev.ToolError, match="path separators"):
        dev.browser("../escape", ["open", "http://localhost"])


def test_browser_uses_pinned_pnpm_dlx_and_artifact_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recorded: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> object:
        recorded["command"] = command
        recorded.update(kwargs)
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(dev, "ROOT", tmp_path)
    monkeypatch.setattr(dev, "load_config", lambda: stack_config(tmp_path))
    monkeypatch.setattr(dev.shutil, "which", lambda _name: "/nix/store/bin/pnpm")
    monkeypatch.setattr(dev.subprocess, "run", fake_run)

    assert dev.browser("flow", ["screenshot"]) == 0
    assert recorded["command"] == [
        "/nix/store/bin/pnpm",
        "dlx",
        f"@playwright/cli@{dev.PLAYWRIGHT_VERSION}",
        "-s=lsstack-test-flow",
        "screenshot",
    ]
    assert recorded["cwd"] == tmp_path / "output" / "playwright" / "flow"


def test_browser_install_explicitly_installs_chromium(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[str] = []
    monkeypatch.setattr(
        dev,
        "run",
        lambda command, **_kwargs: recorded.extend(command),
    )

    dev.browser_install()

    assert recorded == [
        "pnpm",
        "dlx",
        f"@playwright/cli@{dev.PLAYWRIGHT_VERSION}",
        "install-browser",
        "chromium",
    ]


def test_observability_response_parsers() -> None:
    logs = dev._log_entries(
        {
            "data": {
                "result": [
                    {
                        "stream": {"service_name": "lsstack"},
                        "values": [["2", "second"], ["1", "first"]],
                    }
                ]
            }
        }
    )
    assert [entry["line"] for entry in logs] == ["first", "second"]
    assert dev._trace_ids({"trace_id": "a" * 32}) == {"a" * 32}


def test_lgtm_rejects_malformed_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        dev,
        "compose",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="not-json",
            stderr="",
        ),
    )

    with pytest.raises(dev.ToolError, match="malformed JSON"):
        dev._lgtm_json(stack_config(tmp_path), "/api/search", {})


def test_compose_status_parser_accepts_array_and_json_lines() -> None:
    assert dev._parse_compose_status('[{"Service":"postgres"}]') == [
        {"Service": "postgres"}
    ]
    assert dev._parse_compose_status('{"Service":"postgres"}\n{"Service":"nginx"}') == [
        {"Service": "postgres"},
        {"Service": "nginx"},
    ]
    assert dev._compose_summaries(
        [
            {
                "Service": "postgres",
                "State": "running",
                "Health": "healthy",
                "Labels": "intentionally omitted",
            }
        ]
    ) == [{"service": "postgres", "state": "running", "health": "healthy"}]


def test_seed_target_rejects_nonlocal_environment() -> None:
    settings = Settings(
        environment="production",
        debug=False,
        database_url="postgresql+psycopg://app:pass@127.0.0.1:5432/app",
        session_secret="a-secure-session-secret-value-123456789",
        csrf_secret="a-different-csrf-secret-value-123456",
        session_cookie_secure=True,
        public_base_url="https://example.test",
    )
    with pytest.raises(RuntimeError, match="local or development"):
        validate_seed_target(settings)


def test_seed_target_rejects_nonloopback_database() -> None:
    settings = Settings(
        environment="local",
        database_url="postgresql+psycopg://app:pass@db.example.test:5432/app",
        session_secret="a-secure-session-secret-value-123456789",
        csrf_secret="a-different-csrf-secret-value-123456",
        session_cookie_secure=False,
        public_base_url="http://localhost:8080",
    )
    with pytest.raises(RuntimeError, match=r"localhost or 127\.0\.0\.1"):
        validate_seed_target(settings)
