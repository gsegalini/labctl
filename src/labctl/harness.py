"""Command builders for the agent harnesses. Nothing here runs a process.

Every turn passes its message on stdin, never in argv: argv elements are
limited to 128 KiB, and a message starting with '-' would parse as an option.
Builders return (argv, stdin text).
"""

import json

from labctl.install import toml_str

# The experimenter's whole tool surface: --tools limits what exists (no MCP servers, no
# subagents, no edits), dontAsk + --allowedTools what runs without asking. About half the
# input tokens of a turn with the default tools.
CLAUDE_TOOLS = ["--tools", "Bash,Read,Grep,Glob", "--strict-mcp-config", "--permission-mode", "dontAsk",
                "--allowedTools", "Bash(labctl:*)", "Bash(nvidia-smi:*)", "Read", "Grep", "Glob"]
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


def resume(harness: str, session_id: str, message: str, *, model: str | None = None, role: str | None = None,
           in_git: bool = True) -> tuple[list[str], str]:
    """A later turn. `role`: the experimenter's role prompt; None for a manager, whose session
    runs under the project's own permission settings."""
    if harness == "claude":
        if role is None:
            return ["claude", "-p", "--resume", session_id, "--output-format", "json"], message
        # claude ignores a new system prompt on resume, but rebuilds it from the flags after compaction
        return (["claude", "-p", "--resume", session_id, "--output-format", "json",
                 "--append-system-prompt", _check_arg("role prompt", role), *CLAUDE_TOOLS], message)
    if harness == "codex":  # codex does not keep the model across resumes
        return (["codex", "exec", "resume", "--json", "-m", model, "-c", 'sandbox_mode="workspace-write"',
                 *CODEX_NET, *_git_flag(in_git), session_id, "-"], message)
    if harness == "opencode":
        return ["opencode", "run", "--format", "json", "-s", session_id], message
    raise ValueError(f"unknown harness {harness!r}")


def codex_queue(session_id: str, message: str) -> tuple[list[str], str]:
    """Deliver into a codex session that is open interactively (argv only: bounded)."""
    data = message.encode()
    if len(data) > MAX_ARG:  # keep both ends: the last event and the log tail come last
        half = (MAX_ARG - 100) // 2
        head = data[:half].decode(errors="ignore")
        tail = data[-half:].decode(errors="ignore").partition("\n")[2]  # no partial line: it could forge a header
        message = f"{head}\n[labctl: message truncated]\n{tail}"
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


def _usage_line(inp: int, cached: int, out: int, cost=None) -> str:
    """input counts cached tokens too, as claude and codex report it."""
    return (f"input={inp} (cached {cached}) output={out}"
            + (f" cost_usd={cost:.4f}" if isinstance(cost, (int, float)) else ""))


def usage_from(harness: str, stdout: str) -> str | None:
    """Token usage reported by the turn, as one line, or None if the harness reported none."""
    if harness == "opencode":  # one step_finish per model call: add them up
        steps = [obj["part"] for obj in json_objects(stdout)
                 if obj.get("type") == "step_finish" and isinstance(obj.get("part"), dict) and obj["part"].get("tokens")]
        if not steps:
            return None
        inp = cached = out = cost = 0
        for part in steps:
            t, cache = part["tokens"], part["tokens"].get("cache") or {}
            hit = cache.get("read", 0) + cache.get("write", 0)
            inp, cached, out = inp + t.get("input", 0) + hit, cached + hit, out + t.get("output", 0)
            cost += part.get("cost") or 0
        return _usage_line(inp, cached, out, cost)
    for obj in reversed(json_objects(stdout)):
        kind = obj.get("type")
        if harness == "claude" and kind == "result" and obj.get("usage"):
            u = obj["usage"]
            cached = u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
            return _usage_line(u.get("input_tokens", 0) + cached, cached, u.get("output_tokens", 0),
                               obj.get("total_cost_usd"))
        if harness == "codex" and kind == "turn.completed" and obj.get("usage"):
            u = obj["usage"]
            return _usage_line(u.get("input_tokens", 0), u.get("cached_input_tokens", 0), u.get("output_tokens", 0))
    return None
