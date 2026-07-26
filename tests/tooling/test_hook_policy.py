from __future__ import annotations

import pytest

from tools.hook_policy import blocked_reason


@pytest.mark.parametrize(
    "command",
    [
        "gh pr merge 123",
        "gh --repo owner/repo pr merge --squash 123",
        "git push --force origin codex/example",
        "git push -f origin HEAD",
        "git push origin +HEAD:main",
        "git push origin --delete codex/example",
        "git reset --hard HEAD~1",
        "git clean -fd",
        "git branch -D codex/example",
        "git worktree remove --force /tmp/example",
        "git status && gh pr merge 123",
    ],
)
def test_dangerous_commands_are_blocked(command: str) -> None:
    assert blocked_reason(command)


@pytest.mark.parametrize(
    "command",
    [
        "git status --short",
        "git diff --check",
        "git push -u origin codex/example",
        "git branch -d merged-feature",
        "git worktree remove /tmp/clean-merged-worktree",
        "gh pr create --draft --fill",
        "gh pr checks --watch",
    ],
)
def test_normal_commands_are_allowed(command: str) -> None:
    assert blocked_reason(command) is None


def test_malformed_shell_command_fails_closed() -> None:
    assert blocked_reason("git status '") is not None
