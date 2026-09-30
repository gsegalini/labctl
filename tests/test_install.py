import tomllib

import pytest

from labctl.install import (MARKER, ROLES, install, load_role, model_for,
                            split_frontmatter)


@pytest.fixture(autouse=True)
def no_user_config(tmp_path, monkeypatch):
    monkeypatch.setenv("LABCTL_CONFIG", str(tmp_path / "absent.toml"))


def agent_files(harness, root):
    base, ext = {"claude": (".claude/agents", "md"), "codex": (".codex/agents", "toml"),
                 "opencode": (".opencode/agents", "md")}[harness]
    return {name: root / base / f"{name}.{ext}" for name in ROLES}


def skill_file(harness, root):
    return root / (".claude" if harness == "claude" else ".agents") / "skills/labctl-manager/SKILL.md"


def parse_agent(harness, path):
    """Return (metadata, prompt) of an installed agent file."""
    if harness == "codex":
        data = tomllib.loads(path.read_text())
        return data, data["developer_instructions"]
    return split_frontmatter(path.read_text())


def test_roles_parse():
    for name in ROLES:
        role = load_role(name)
        assert role.name == name
        assert role.tier in {"frontier", "strong", "cheap"}
        assert role.description and role.prompt.strip()
    assert [n for n in ROLES if load_role(n).read_only] == ["explorer"]


@pytest.mark.parametrize("harness", ["claude", "codex", "opencode"])
def test_install(harness, tmp_path):
    written = install(harness, tmp_path)
    expected = set(agent_files(harness, tmp_path).values()) | {skill_file(harness, tmp_path)}
    assert set(written) == expected
    assert all(p.is_file() for p in expected)

    for name, path in agent_files(harness, tmp_path).items():
        role = load_role(name)
        meta, prompt = parse_agent(harness, path)
        assert meta["model"] == model_for(harness, role.tier)
        assert prompt.strip() == role.prompt.strip()
        text = path.read_text()
        restricted = {"claude": "disallowedTools", "codex": "sandbox_mode",
                      "opencode": "edit: deny"}[harness]
        assert (restricted in text) == (name == "explorer")
        if harness == "codex":
            assert meta["name"] == name and meta["description"] == role.description
            assert (meta.get("sandbox_mode") == "read-only") == (name == "explorer")
        else:
            assert role.description in meta["description"]
        if harness == "opencode":
            assert meta["mode"] == "subagent"

    meta, body = split_frontmatter(skill_file(harness, tmp_path).read_text())
    assert meta["name"] == "labctl-manager" and "experiment" in meta["description"]
    assert "[labctl wake]" in body


def test_reinstall_overwrites_own_files_only(tmp_path):
    first = install("claude", tmp_path)
    first[0].write_text(first[0].read_text() + "stale edit\n")
    install("claude", tmp_path)
    assert "stale edit" not in first[0].read_text()

    first[0].write_text("my own agent\n")
    with pytest.raises(FileExistsError):
        install("claude", tmp_path)
    assert first[0].read_text() == "my own agent\n"


def test_user_config_overrides_per_key(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text('[claude]\ncheap = "my-small-model"\n')
    monkeypatch.setenv("LABCTL_CONFIG", str(config))
    assert model_for("claude", "cheap") == "my-small-model"
    assert model_for("claude", "strong") == "opus"  # untouched keys keep defaults
    install("claude", tmp_path / "proj")
    meta, _ = split_frontmatter((tmp_path / "proj/.claude/agents/explorer.md").read_text())
    assert meta["model"] == "my-small-model"


def test_codex_prompt_with_awkward_text_round_trips(tmp_path):
    from labctl.install import toml_str
    for text in ["a\nb '''quoted''' \"x\" \\ end\n", "plain \"q\" \\n", "line\nwith 'quote'\n"]:
        assert tomllib.loads(f"v = {toml_str(text)}")["v"] == text


def test_unknown_harness(tmp_path):
    with pytest.raises(ValueError):
        install("vim", tmp_path)
    with pytest.raises(ValueError):
        model_for("vim", "cheap")
    assert not any(tmp_path.iterdir())


def test_marker_is_in_every_file(tmp_path):
    for harness in ["claude", "codex", "opencode"]:
        for path in install(harness, tmp_path / harness):
            assert MARKER in path.read_text()
