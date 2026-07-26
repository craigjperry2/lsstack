from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
CORE = (ROOT / "src/app/domain", ROOT / "src/app/application")
FORBIDDEN_CORE_IMPORTS = {
    "anyio",
    "asyncio",
    "litestar",
    "opentelemetry",
    "psycopg",
    "saq",
    "sqlalchemy",
}


def python_files(*directories: Path) -> tuple[Path, ...]:
    return tuple(
        path
        for directory in directories
        for path in directory.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def test_domain_and_application_are_synchronous_and_framework_free() -> None:
    failures: list[str] = []
    for path in python_files(*CORE):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef | ast.Await):
                failures.append(f"{path.relative_to(ROOT)}:{node.lineno}: async syntax")
            if isinstance(node, ast.Import):
                imported = {alias.name.partition(".")[0] for alias in node.names}
                if blocked := imported & FORBIDDEN_CORE_IMPORTS:
                    failures.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}: {sorted(blocked)}"
                    )
            if isinstance(node, ast.ImportFrom) and node.module:
                top_level = node.module.partition(".")[0]
                if top_level in FORBIDDEN_CORE_IMPORTS:
                    failures.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}: {top_level}"
                    )
    assert not failures, "\n".join(failures)


def test_unowned_background_tasks_are_forbidden_repository_wide() -> None:
    failures: list[str] = []
    for path in python_files(ROOT / "src", ROOT / "tests"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "asyncio"
                and node.func.attr == "create_task"
            ):
                failures.append(  # noqa: PERF401
                    f"{path.relative_to(ROOT)}:{node.lineno}"
                )
    assert not failures, "\n".join(failures)


def test_base_exception_handlers_explicitly_preserve_cancellation() -> None:
    failures: list[str] = []
    for path in python_files(ROOT / "src"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            base_index = next(
                (
                    index
                    for index, handler in enumerate(node.handlers)
                    if isinstance(handler.type, ast.Name)
                    and handler.type.id == "BaseException"
                ),
                None,
            )
            if base_index is None:
                continue
            earlier = node.handlers[:base_index]
            preserves_cancel = any(
                (
                    isinstance(handler.type, ast.Attribute)
                    and handler.type.attr == "CancelledError"
                )
                or (
                    isinstance(handler.type, ast.Name)
                    and handler.type.id == "CancelledError"
                )
                for handler in earlier
            )
            if not preserves_cancel:
                failures.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not failures, "\n".join(failures)


def test_templates_use_only_local_assets_and_no_inline_script() -> None:
    for path in (ROOT / "src/app/templates").rglob("*.html"):
        source = path.read_text()
        assert "https://" not in source
        assert "http://" not in source
        assert "<script>" not in source
        assert "javascript:" not in source.casefold()


def test_domain_dataclasses_are_frozen() -> None:
    failures: list[str] = []
    for path in python_files(ROOT / "src/app/domain"):
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            dataclass_decorator = next(
                (
                    decorator
                    for decorator in node.decorator_list
                    if isinstance(decorator, ast.Name | ast.Call)
                    and (
                        (
                            isinstance(decorator, ast.Name)
                            and decorator.id == "dataclass"
                        )
                        or (
                            isinstance(decorator, ast.Call)
                            and isinstance(decorator.func, ast.Name)
                            and decorator.func.id == "dataclass"
                        )
                    )
                ),
                None,
            )
            if dataclass_decorator is None:
                continue
            frozen = isinstance(dataclass_decorator, ast.Call) and any(
                keyword.arg == "frozen"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in dataclass_decorator.keywords
            )
            if not frozen:
                failures.append(f"{path.relative_to(ROOT)}:{node.lineno}:{node.name}")
    assert not failures, "\n".join(failures)


def test_no_obvious_mutable_process_global_request_state() -> None:
    failures: list[str] = []
    request_state_words = {"current", "request", "session", "user"}
    for path in python_files(ROOT / "src/app/adapters"):
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if not isinstance(node, ast.Assign | ast.AnnAssign):
                continue
            value = node.value
            if not isinstance(value, ast.Dict | ast.List | ast.Set):
                continue
            names: list[str] = []
            if isinstance(node, ast.Assign):
                names = [
                    target.id for target in node.targets if isinstance(target, ast.Name)
                ]
            elif isinstance(node.target, ast.Name):
                names = [node.target.id]
            if any(
                word in name.casefold()
                for name in names
                for word in request_state_words
            ):
                failures.append(f"{path.relative_to(ROOT)}:{node.lineno}:{names}")
    assert not failures, "\n".join(failures)


def test_application_result_types_do_not_expose_orm_or_framework_objects() -> None:
    failures: list[str] = []
    blocked_names = {
        "AsyncSession",
        "Cookie",
        "Redirect",
        "Request",
        "Response",
        "Session",
        "Template",
    }
    for path in python_files(ROOT / "src/app/application"):
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if not isinstance(node, ast.ClassDef) or not node.name.endswith(
                ("Result", "Session", "Message")
            ):
                continue
            for statement in node.body:
                if not isinstance(statement, ast.AnnAssign):
                    continue
                annotation_names = {
                    child.id
                    for child in ast.walk(statement.annotation)
                    if isinstance(child, ast.Name)
                }
                if blocked := annotation_names & blocked_names:
                    failures.append(
                        f"{path.relative_to(ROOT)}:{statement.lineno}:{sorted(blocked)}"
                    )
    assert not failures, "\n".join(failures)
