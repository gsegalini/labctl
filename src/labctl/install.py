"""Render labctl's role prompts and manager skill into a project for one harness."""

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).parent
ROLES = ("implementer", "experimenter", "explorer")
HARNESSES = ("claude", "codex", "opencode")
MARKER = "# written by labctl install; rerun it to update"


@dataclass(frozen=True)
class Role:
    name: str
    description: str
    tier: str
    read_only: bool
    prompt: str


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split `---`-delimited `key: value` frontmatter from the body."""
    head, sep, body = text.removeprefix("---\n").partition("\n---\n")
    if not text.startswith("---\n") or not sep:
        raise ValueError("missing --- frontmatter")
    meta = {}
    for line in head.splitlines():
        key, _, value = line.partition(":")
        if key.strip() and not key.startswith("#"):
            meta[key.strip()] = value.strip()
    return meta, body


def load_role(name: str) -> Role:
    meta, body = split_frontmatter((HERE / "roles" / f"{name}.md").read_text())
    return Role(meta["name"], meta["description"], meta["tier"],
                meta["read_only"] == "true", body.strip() + "\n")


def config_path() -> Path:
    return Path(os.environ.get("LABCTL_CONFIG", "~/.config/labctl/config.toml")).expanduser()


def model_for(harness: str, tier: str) -> str:
    """Model for a role tier; the user config overrides defaults.toml per key."""
    models = tomllib.loads((HERE / "defaults.toml").read_text())
    if config_path().exists():
        user = tomllib.loads(config_path().read_text())
        for h, table in user.items():
            if isinstance(table, dict):
                models.setdefault(h, {}).update(table)
    if harness not in models:
        raise ValueError(f"unknown harness {harness!r}; expected one of {HARNESSES}")
    if tier not in models[harness]:
        raise ValueError(f"no model for tier {tier!r} in harness {harness!r}")
    return models[harness][tier]


def toml_str(text: str) -> str:
    if "\n" in text and "'''" not in text:
        return "'''\n" + text + "'''"  # literal string: no escaping
    return json.dumps(text, ensure_ascii=False)  # a JSON string is a valid TOML basic string


def render_role(role: Role, harness: str) -> str:
    model = model_for(harness, role.tier)
    desc = json.dumps(role.description, ensure_ascii=False)  # valid YAML double-quoted scalar
    if harness == "claude":
        extra = "disallowedTools: Edit, Write, NotebookEdit\n" if role.read_only else ""
        return (f"---\n{MARKER}\nname: {role.name}\ndescription: {desc}\n"
                f"model: {model}\n{extra}---\n{role.prompt}")
    if harness == "codex":
        extra = 'sandbox_mode = "read-only"\n' if role.read_only else ""
        return (f"{MARKER}\nname = {toml_str(role.name)}\ndescription = {toml_str(role.description)}\n"
                f"model = {toml_str(model)}\n{extra}"
                f"developer_instructions = {toml_str(role.prompt)}\n")
    if harness == "opencode":
        extra = "permission:\n  edit: deny\n" if role.read_only else ""
        return (f"---\n{MARKER}\ndescription: {desc}\nmode: subagent\n"
                f"model: {model}\n{extra}---\n{role.prompt}")
    raise ValueError(f"unknown harness {harness!r}; expected one of {HARNESSES}")


def install(harness: str, project_dir: Path) -> list[Path]:
    """Write the three roles and the manager skill; return the written paths.

    Overwrites files written by an earlier install (they carry MARKER) and
    refuses to overwrite any other existing file.
    """
    if harness not in HARNESSES:
        raise ValueError(f"unknown harness {harness!r}; expected one of {HARNESSES}")
    project_dir = Path(project_dir)
    agents, ext = {"claude": (".claude/agents", "md"), "codex": (".codex/agents", "toml"),
                   "opencode": (".opencode/agents", "md")}[harness]
    skills = ".claude/skills" if harness == "claude" else ".agents/skills"
    files = {project_dir / agents / f"{name}.{ext}": render_role(load_role(name), harness)
             for name in ROLES}
    skill = (HERE / "skill" / "SKILL.md").read_text()
    files[project_dir / skills / "labctl-manager" / "SKILL.md"] = skill.replace("---\n", f"---\n{MARKER}\n", 1)

    for path in files:
        if path.exists() and MARKER not in path.read_text():
            raise FileExistsError(f"{path} exists and was not written by labctl; move it away first")
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return list(files)
