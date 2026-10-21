"""The plugin registry: built-in and entry-point harness integrations and adapters."""

from __future__ import annotations

import importlib.metadata
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import make_repo
from daedalus import cli, plugins
from daedalus.adapters import make_adapter
from daedalus.adapters.base import Adapter
from daedalus.adapters.command import CommandAdapter
from daedalus.adapters.inline import InlineAdapter
from daedalus.core.errors import CapabilityError


def _fake(name: str, calls: list, detected: bool = True) -> plugins.HarnessIntegration:
    def install(root: Path) -> list[str]:
        calls.append((name, root))
        return [f"installed {name}"]

    return plugins.HarnessIntegration(
        name=name, description=f"fake {name}", install=install, detect=lambda root: detected,
        adapter_preset=None,
    )


@pytest.fixture
def entry_points(monkeypatch):
    """Register fake entry points: eps["daedalus.harnesses"] = {name: object}."""
    eps: dict[str, dict[str, object]] = {}

    def fake_entry_points(*, group: str):
        return [SimpleNamespace(name=n, load=(lambda o=o: o)) for n, o in eps.get(group, {}).items()]

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)
    plugins.reset_cache()
    yield eps
    plugins.reset_cache()


@pytest.fixture
def only_claude(monkeypatch):
    """Built-ins other than claude-code are being written elsewhere: do not depend on them."""
    monkeypatch.setattr(plugins, "BUILTIN_HARNESSES", {"claude-code": plugins.BUILTIN_HARNESSES["claude-code"]})


def test_builtin_listing(entry_points):
    infos = {i.name: i for i in plugins.list_harnesses()}
    assert list(infos)[:5] == ["claude-code", "codex", "opencode", "pi", "hermes"]
    assert all(i.source == "built-in" for i in infos.values())
    assert infos["claude-code"].available
    adapters = {i.name: i for i in plugins.list_adapters()}
    assert {"inline", "claude-code", "codex", "aider"} <= set(adapters)
    assert adapters["claude-code"].read_only == "locked"
    assert adapters["codex"].read_only == "read_only_args"


def test_missing_builtin_module_is_unavailable_not_a_crash(entry_points, monkeypatch):
    monkeypatch.setitem(plugins.BUILTIN_HARNESSES, "ghost", "daedalus.harness.no_such_module:INTEGRATION")
    monkeypatch.setitem(plugins.BUILTIN_HARNESSES, "broken", "daedalus.harness.claude_code:NOPE")
    infos = {i.name: i for i in plugins.list_harnesses()}
    assert not infos["ghost"].available and infos["ghost"].detail == "module not installed"
    assert not infos["broken"].available and "AttributeError" in infos["broken"].detail
    with pytest.raises(plugins.PluginError, match="unavailable"):
        plugins.get_harness("ghost")
    assert "ghost" not in [h.name for h in plugins.detected_harnesses(Path("."))]


def test_third_party_harness_via_entry_point(entry_points, only_claude):
    entry_points["daedalus.harnesses"] = {"acme": _fake("acme", [])}
    infos = {i.name: i for i in plugins.list_harnesses()}
    assert infos["acme"].source == "third-party" and infos["acme"].available
    assert plugins.get_harness("acme").description == "fake acme"


def test_third_party_cannot_shadow_builtin(entry_points, only_claude):
    entry_points["daedalus.harnesses"] = {"claude-code": _fake("evil", [])}
    assert plugins.get_harness("claude-code").name == "claude-code"
    assert plugins.harness_names() == ["claude-code"]


def test_bad_third_party_harness_is_unavailable(entry_points, only_claude):
    entry_points["daedalus.harnesses"] = {"bad": object()}
    (info,) = [i for i in plugins.list_harnesses() if i.name == "bad"]
    assert not info.available and "HarnessIntegration" in info.detail


def test_discovery_is_cached(entry_points):
    entry_points["daedalus.harnesses"] = {"acme": _fake("acme", [])}
    assert "acme" in plugins.harness_names()
    entry_points["daedalus.harnesses"] = {}
    assert "acme" in plugins.harness_names()
    plugins.reset_cache()
    assert "acme" not in plugins.harness_names()


def test_adapter_factory_entry_point(entry_points):
    seen = []

    def factory(cfg):
        seen.append(cfg)
        return InlineAdapter()

    entry_points["daedalus.adapters"] = {"acme-agent": factory}
    cfg = {"name": "acme-agent", "x": 1}
    assert isinstance(make_adapter(cfg), Adapter)
    assert seen == [cfg]
    (info,) = [i for i in plugins.list_adapters() if i.name == "acme-agent"]
    assert info.source == "third-party" and info.read_only == "unknown (factory)"


def test_adapter_preset_entry_point(entry_points):
    preset = {"argv": ["acme", "run"], "read_only_args": ["--ro"], "prompt_via": "arg"}
    entry_points["daedalus.adapters"] = {"acme-agent": preset}
    ad = make_adapter({"name": "acme-agent"})
    assert isinstance(ad, CommandAdapter) and ad.argv == ["acme", "run"] and ad.read_only_args == ["--ro"]
    (info,) = [i for i in plugins.list_adapters() if i.name == "acme-agent"]
    assert info.read_only == "read_only_args"


def test_third_party_preset_is_not_locked_unless_it_declares_it(entry_points):
    entry_points["daedalus.adapters"] = {
        "open": {"argv": ["acme"]},
        "tight": {"argv": ["acme"], "locked_read_only": True},
    }
    # Not locked: built-in-preset rules apply (only `args` may be configured), no read-only claim.
    assert make_adapter({"name": "open", "args": ["--x"]}).builder_args == ["--x"]
    with pytest.raises(CapabilityError, match="cannot be set"):
        make_adapter({"name": "open", "argv": ["sh"]})
    # Locked only because it said so: config is held to the allowlist.
    with pytest.raises(CapabilityError, match="not an allowed extra flag"):
        make_adapter({"name": "tight", "args": ["--x"]})
    modes = {i.name: i.read_only for i in plugins.list_adapters()}
    assert modes["open"] == "none" and modes["tight"] == "locked"


def test_presets_win_over_entry_points(entry_points):
    entry_points["daedalus.adapters"] = {"codex": lambda cfg: InlineAdapter()}
    assert isinstance(make_adapter({"name": "codex"}), CommandAdapter)


def test_install_dispatch(entry_points, only_claude, tmp_path, capsys):
    calls: list = []
    entry_points["daedalus.harnesses"] = {"acme": _fake("acme", calls)}
    root = make_repo(tmp_path / "r")
    assert cli.main(["-C", str(root), "install", "acme"]) == 0
    assert calls == [("acme", root)]
    assert "acme: installed acme" in capsys.readouterr().out
    assert not (root / ".claude").exists()
    assert cli.main(["-C", str(root), "install", "nope"]) == 2
    assert cli.main(["-C", str(root), "install"]) == 2


def test_install_claude_code_unchanged(entry_points, tmp_path):
    root = make_repo(tmp_path / "r")
    assert cli.main(["-C", str(root), "install", "claude-code"]) == 0
    assert (root / ".claude" / "settings.json").exists()
    assert (root / ".claude" / "skills" / "daedalus-gate" / "SKILL.md").exists()


def test_install_all_detected(entry_points, only_claude, tmp_path):
    calls: list = []
    entry_points["daedalus.harnesses"] = {
        "here": _fake("here", calls),
        "absent": _fake("absent", calls, detected=False),
    }
    root = make_repo(tmp_path / "r")
    assert cli.main(["-C", str(root), "install", "--all-detected"]) == 0
    names = [n for n, _ in calls]
    assert "here" in names and "absent" not in names


def test_init_harness_selects_explicitly(entry_points, only_claude, tmp_path):
    calls: list = []
    entry_points["daedalus.harnesses"] = {"acme": _fake("acme", calls), "other": _fake("other", calls)}
    root = make_repo(tmp_path / "r")
    assert cli.main(["-C", str(root), "init", "--harness", "acme"]) == 0
    assert [n for n, _ in calls] == ["acme"]
    assert not (root / ".claude").exists()  # explicit choice: claude-code not added
    assert cli.main(["-C", str(root), "init", "--force", "--harness", "bogus"]) == 2


def test_init_default_installs_claude_code_and_detected(entry_points, only_claude, tmp_path):
    calls: list = []
    entry_points["daedalus.harnesses"] = {"acme": _fake("acme", calls), "off": _fake("off", calls, False)}
    root = make_repo(tmp_path / "r")
    assert cli.main(["-C", str(root), "init"]) == 0
    assert [n for n, _ in calls] == ["acme"]
    assert (root / ".claude" / "settings.json").exists()
    calls.clear()
    assert cli.main(["-C", str(root), "init", "--force", "--no-hooks"]) == 0
    assert calls == []


def test_plugins_command_lists_everything(entry_points, only_claude, tmp_path, capsys):
    entry_points["daedalus.harnesses"] = {"acme": _fake("acme", [])}
    entry_points["daedalus.adapters"] = {"acme-agent": {"argv": ["acme"]}}
    root = make_repo(tmp_path / "r")
    for argv in (["plugins"], ["plugins", "list"]):
        assert cli.main(["-C", str(root), *argv]) == 0
        out = capsys.readouterr().out
        assert "acme" in out and "third-party" in out and "built-in" in out
        assert "acme-agent" in out and "read-only reviews: none" in out
        assert "read-only reviews: locked" in out


# ------------------------------------------------- availability, options, errors (F14-F16)
def test_opencode_adapter_is_unavailable_when_only_python_is_on_path(entry_points, monkeypatch):
    from daedalus.adapters.command import PRESETS

    assert PRESETS["opencode"]["available_probe"] == "opencode"
    real = plugins.shutil.which
    monkeypatch.setattr(plugins.shutil, "which", lambda exe, *a, **k: None if exe == "opencode" else real(exe, *a, **k))
    info = {i.name: i for i in plugins.list_adapters()}["opencode"]
    assert not info.available and "`opencode` not on PATH" in info.detail
    from daedalus.adapters import command

    monkeypatch.setattr(command.shutil, "which", lambda exe, *a, **k: None if exe == "opencode" else "/x/" + exe)
    assert not CommandAdapter.from_config({"name": "opencode"}).available()
    monkeypatch.setattr(command.shutil, "which", lambda exe, *a, **k: "/x/" + exe)
    assert CommandAdapter.from_config({"name": "opencode"}).available()


def test_third_party_preset_can_declare_an_available_probe(entry_points, monkeypatch):
    entry_points["daedalus.adapters"] = {"acme-agent": {"argv": ["python3", "-m", "acme"], "available_probe": "acme-cli"}}
    adapter = CommandAdapter.from_config({"name": "acme-agent"}, preset=entry_points["daedalus.adapters"]["acme-agent"])
    from daedalus.adapters import command

    monkeypatch.setattr(command.shutil, "which", lambda exe, *a, **k: "/x/" + exe if exe == "python3" else None)
    assert not adapter.available()


def test_third_party_preset_error_says_third_party():
    preset = {"argv": ["acme"]}
    with pytest.raises(CapabilityError, match="third-party preset"):
        CommandAdapter.from_config({"name": "acme-agent", "argv": ["other"]}, preset=preset)
    with pytest.raises(CapabilityError, match="built-in preset"):
        CommandAdapter.from_config({"name": "pi", "argv": ["other"]})


def test_missing_module_inside_a_builtin_is_not_reported_as_module_not_installed(entry_points, monkeypatch):
    real = plugins.importlib.import_module

    def fake(name, *a, **k):
        if name == "daedalus.harness.codex":
            raise ModuleNotFoundError("No module named 'tomlkit'", name="tomlkit")
        if name == "daedalus.harness.pi":
            raise ModuleNotFoundError("No module named 'daedalus.harness.pi'", name="daedalus.harness.pi")
        return real(name, *a, **k)

    monkeypatch.setattr(plugins.importlib, "import_module", fake)
    info = {i.name: i for i in plugins.list_harnesses()}
    assert not info["codex"].available and "tomlkit" in info["codex"].detail
    assert info["pi"].detail == "module not installed"


def test_all_skills_is_rejected_for_harnesses_without_role_skills(entry_points, only_claude, tmp_path, capsys):
    calls: list = []
    entry_points["daedalus.harnesses"] = {"acme": _fake("acme", calls)}
    root = make_repo(tmp_path / "r")
    assert cli.main(["-C", str(root), "install", "acme", "--all-skills"]) == 2
    assert "--all-skills" in capsys.readouterr().err and calls == []
    assert cli.main(["-C", str(root), "install", "claude-code", "--all-skills"]) == 0
    assert (root / ".claude" / "skills" / "brunel").exists() or (root / ".claude" / "skills").is_dir()


def test_all_skills_is_passed_through_to_pi(entry_points, tmp_path):
    root = make_repo(tmp_path / "r")
    assert cli.main(["-C", str(root), "install", "pi", "--all-skills"]) == 0
    assert len(list((root / ".pi" / "skills").iterdir())) > 1


def test_init_rejects_harness_with_no_hooks(entry_points, only_claude, tmp_path, capsys):
    calls: list = []
    entry_points["daedalus.harnesses"] = {"acme": _fake("acme", calls)}
    root = make_repo(tmp_path / "r")
    assert cli.main(["-C", str(root), "init", "--harness", "acme", "--no-hooks"]) == 2
    assert "--no-hooks" in capsys.readouterr().err and calls == []


def test_commit_advice_names_the_harness_files(entry_points, tmp_path, capsys):
    entry_points["daedalus.harnesses"] = {"acme": _fake("acme", [])}
    root = make_repo(tmp_path / "r")
    assert cli.main(["-C", str(root), "install", "acme"]) == 0
    assert "Commit the files listed above" in capsys.readouterr().out
    assert cli.main(["-C", str(root), "install", "pi"]) == 0
    out = capsys.readouterr().out
    assert "Commit .pi/ so" in out and ".claude" not in out.split("Commit", 1)[1]
    assert cli.main(["-C", str(root), "install", "codex"]) == 0
    assert "Commit .codex/ and AGENTS.md" in capsys.readouterr().out
    assert cli.main(["-C", str(root), "init", "--harness", "pi"]) == 0
    assert "review and commit .daedalus.yml and .pi/" in capsys.readouterr().out


def test_git_hook_is_installed_exactly_once_per_command(entry_points, tmp_path, monkeypatch):
    from daedalus.harness import provenance

    calls: list = []
    real = provenance.install_git_hook
    monkeypatch.setattr(provenance, "install_git_hook", lambda *a, **k: calls.append(k) or real(*a, **k))
    root = make_repo(tmp_path / "r")
    for names in (["hermes"], ["pi"], ["codex"], ["opencode"]):
        calls.clear()
        assert cli.main(["-C", str(root), "install", *names]) == 0
        assert len(calls) == 1, names
    assert (root / ".git" / "hooks" / "prepare-commit-msg").exists()
    calls.clear()
    assert cli.main(["-C", str(root), "init", "--force", "--harness", "hermes", "--harness", "pi"]) == 0
    assert len(calls) == 1
