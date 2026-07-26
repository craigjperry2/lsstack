"""Codex shell-command guardrails for destructive Git and GitHub operations."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

SHELL_SEPARATORS = {";", "&&", "||", "|", "&"}


def _chunks(command: str) -> list[list[str]]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    lexer.commenters = ""
    chunks: list[list[str]] = [[]]
    for token in lexer:
        if token in SHELL_SEPARATORS or set(token) <= set(";&|"):
            if chunks[-1]:
                chunks.append([])
            continue
        chunks[-1].append(token)
    return [chunk for chunk in chunks if chunk]


def _find_tool(tokens: list[str], name: str) -> int | None:
    for index, token in enumerate(tokens):
        if Path(token).name == name:
            return index
    return None


def _has_short_flag(tokens: list[str], flag: str) -> bool:
    return any(
        token.startswith("-") and not token.startswith("--") and flag in token[1:]
        for token in tokens
    )


def _github_reason(tokens: list[str]) -> str | None:
    index = _find_tool(tokens, "gh")
    if index is None:
        return None
    arguments = tokens[index + 1 :]
    for position, argument in enumerate(arguments):
        if argument == "pr" and "merge" in arguments[position + 1 :]:
            return "`gh pr merge` is a human-only action in this repository."
    return None


def _git_reason(tokens: list[str]) -> str | None:  # noqa: PLR0911
    index = _find_tool(tokens, "git")
    if index is None:
        return None
    arguments = tokens[index + 1 :]

    if "push" in arguments:
        push_arguments = arguments[arguments.index("push") + 1 :]
        if (
            any(
                argument in {"--force", "--force-with-lease", "--force-if-includes"}
                for argument in push_arguments
            )
            or _has_short_flag(push_arguments, "f")
            or any(argument.startswith("+") for argument in push_arguments)
        ):
            return "Force-pushing is blocked by repository policy."
        if (
            "--delete" in push_arguments
            or _has_short_flag(push_arguments, "d")
            or any(argument.startswith(":") for argument in push_arguments)
        ):
            return "Deleting remote branches is blocked by repository policy."

    if "reset" in arguments and "--hard" in arguments:
        return "`git reset --hard` is blocked because it discards local work."

    if "clean" in arguments:
        clean_arguments = arguments[arguments.index("clean") + 1 :]
        if "--force" in clean_arguments or _has_short_flag(clean_arguments, "f"):
            return "Forced `git clean` is blocked because it deletes untracked files."

    if "branch" in arguments:
        branch_arguments = arguments[arguments.index("branch") + 1 :]
        if "-D" in branch_arguments or (
            "--delete" in branch_arguments and "--force" in branch_arguments
        ):
            return "Forced local branch deletion is blocked by repository policy."

    if "worktree" in arguments and "remove" in arguments:
        remove_arguments = arguments[arguments.index("remove") + 1 :]
        if "--force" in remove_arguments:
            return "Forced worktree removal is blocked by repository policy."

    return None


def blocked_reason(command: str) -> str | None:
    """Return the policy reason when a shell command must be denied."""
    try:
        chunks = _chunks(command)
    except ValueError:
        return "Refusing a malformed shell command that could not be inspected."
    for tokens in chunks:
        reason = _github_reason(tokens) or _git_reason(tokens)
        if reason is not None:
            return reason
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as error:
        print(f"Unable to inspect command: {error}", file=sys.stderr)
        return 2
    command = payload.get("tool_input", {}).get("command")
    if not isinstance(command, str):
        return 0
    reason = blocked_reason(command)
    if reason is None:
        return 0
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
