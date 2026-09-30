"""Command builders for the agent harnesses. Nothing here runs a process.

Every turn passes its message on stdin, never in argv: argv elements are
limited to 128 KiB, and a message starting with '-' would parse as an option.
Builders return (argv, stdin text).
"""

import json

from labctl.install import toml_str

CLAUDE_TOOLS = ["--permission-mode", "dontAsk", "--allowedTools", "Bash(labctl:*)", "Bash(nvidia-smi:*)",
                "Read", "Grep", "Glob"]
CODEX_NET = ["-c", "sandbox_workspace_write.network_access=true"]
MAX_ARG = 100_000  # bytes; Linux refuses a single argv element above 128 KiB


def _git_flag(in_git: bool) -> list[str]:
    return [] if in_git else ["--skip-git-repo-check"]


def _check_arg(name: str, value: str) -> str:
    if len(value.encode()) > MAX_ARG:
        raise ValueError(f"{name} is {len(value.encode())} bytes; the limit for a command-line argument is {MAX_ARG}")
    return value


def start(harness: str, *, model: str, role: str, message: str, session_id: str | None = None,
          in_git: bool = True) -> tuple[list[str], str]:
    """First turn of a new session. claude needs the session id labctl chose."""
    if harness == "claude":
        return (["claude", "-p", "--session-id", session_id, "--model", model, "--output-format", "json",
                 "--append-system-prompt", _check_arg("role prompt", role), *CLAUDE_TOOLS], message)
    if harness == "codex":
        instructions = _check_arg("role prompt", f"developer_instructions={toml_str(role)}")
        return (["codex", "exec", "--json", "-m", model, "-c", instructions, "-s", "workspace-write",
                 *CODEX_NET, *_git_flag(in_git), "-"], message)
    if harness == "opencode":  # no system-prompt flag: the role opens the first message
        return ["opencode", "run", "--format", "json", "-m", model], f"{role}\n\n{message}"
    raise ValueError(f"unknown harness {harness!r}")


def resume(harness: str, session_id: str, message: str, *, model: str | None = None,
           in_git: bool = True) -> tuple[list[str], str]:
    if harness == "claude":
        return ["claude", "-p", "--resume", session_id, "--output-format", "json", *CLAUDE_TOOLS], message
    if harness == "codex":  # codex does not keep the model across resumes
        return (["codex", "exec", "resume", "--json", "-m", model, "-c", 'sandbox_mode="workspace-write"',
                 *CODEX_NET, *_git_flag(in_git), session_id, "-"], message)
    if harness == "opencode":
        return ["opencode", "run", "--format", "json", "-s", session_id], message
    raise ValueError(f"unknown harness {harness!r}")


def codex_queue(session_id: str, message: str) -> tuple[list[str], str]:
    """Deliver into a codex session that is open interactively (argv only: bounded)."""
    if len(message.encode()) > MAX_ARG:
        message = message.encode()[:MAX_ARG - 100].decode(errors="ignore") + "\n[labctl: message truncated]"
    return ["codex", "queue", "--thread", session_id, f"--message={message}"], ""


def session_not_found(output: str) -> bool:
    """Whether a failed resume says the session does not exist."""
    low = output.lower()
    return "no conversation found" in low or "not found" in low or "no such session" in low


def attach(harness: str, session_id: str, model: str | None = None) -> list[str]:
    if harness == "claude":
        return ["claude", "--resume", session_id]
    if harness == "codex":
        return ["codex", "resume", "-m", model, session_id]
    if harness == "opencode":
        return ["opencode", "-s", session_id]
    raise ValueError(f"unknown harness {harness!r}")


def json_objects(stdout: str) -> list[dict]:
    """JSON objects in the output: the whole of it (claude) or one per line (codex, opencode)."""
    objs = []
    for chunk in [stdout, *stdout.splitlines()]:
        try:
            obj = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(obj, dict):
            objs.append(obj)
    return objs


def session_id_from(harness: str, stdout: str) -> str | None:
    """The session id reported by a --json / --output-format json turn."""
    for obj in json_objects(stdout):
        if harness == "claude" and obj.get("type") == "result" and obj.get("session_id"):
            return str(obj["session_id"])
        if harness == "codex" and obj.get("type") == "thread.started" and obj.get("thread_id"):
            return str(obj["thread_id"])
        if harness == "opencode" and obj.get("sessionID"):
            return str(obj["sessionID"])
    return None


def reply_from(harness: str, stdout: str) -> tuple[str | None, bool]:
    """(the agent's final reply, whether the harness reported an error) from JSON output."""
    reply, error = None, False
    for obj in json_objects(stdout):
        kind = obj.get("type")
        if harness == "claude" and kind == "result":
            reply, error = obj.get("result"), bool(obj.get("is_error"))
        elif harness == "codex":
            item = obj.get("item") or {}
            if kind == "item.completed" and item.get("type") == "agent_message":
                reply = item.get("text")
            error = error or kind == "turn.failed"  # "error" events include retried hiccups
        elif harness == "opencode":
            part = obj.get("part") or {}
            if kind == "text" and part.get("text"):
                reply = part["text"]
            error = error or kind == "error"
    return (reply if isinstance(reply, str) else None), error
