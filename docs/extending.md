# Extending Daedalus

Two things plug in, each through a Python entry point (or, for built-ins, a module in this package).
Nothing in `cli.py` changes.

| Extension              | Entry-point group     | What it does                                              |
|------------------------|-----------------------|-----------------------------------------------------------|
| Harness integration    | `daedalus.harnesses`  | Installs the ambient gate into a harness (hooks, plugins, rules files) |
| Adapter                | `daedalus.adapters`   | Launches agents: builders and reviewers                   |

`daedalus plugins` lists both, built-in or third-party, available or not, and (for harnesses) detected
in this repository or not.

## A harness integration in about 30 lines

```python
# acme_daedalus/harness.py
from pathlib import Path
import shutil

from daedalus.plugins import HarnessIntegration

RULES = "Run `daedalus status` before you stop. The gate decides when work is done.\n"


def install(root: Path) -> list[str]:
    """Idempotent. Return human-readable lines describing what was done."""
    rules = root / ".acme" / "rules.md"
    rules.parent.mkdir(exist_ok=True)
    rules.write_text(RULES, encoding="utf-8")
    return [f"wrote {rules.relative_to(root).as_posix()}"]


def uninstall(root: Path) -> list[str]:
    (root / ".acme" / "rules.md").unlink(missing_ok=True)
    return ["removed .acme/rules.md"]


def detect(root: Path) -> bool:
    return (root / ".acme").is_dir() or shutil.which("acme") is not None


INTEGRATION = HarnessIntegration(
    name="acme",
    description="Acme: rules file in .acme/",
    install=install,
    detect=detect,
    adapter_preset=None,  # or the `adapter:` name that launches this harness
    uninstall=uninstall,  # optional
)
```

Register it in your package's `pyproject.toml`:

```toml
[project.entry-points."daedalus.harnesses"]
acme = "acme_daedalus.harness:INTEGRATION"
```

After `pip install`, `daedalus install acme` works, `daedalus install --all-detected` includes it when
`detect` is true, and so does `daedalus init`. `daedalus init --harness acme` installs only what you name
(repeat the flag for several). An entry point may also resolve to a zero-argument function that returns
a `HarnessIntegration`. A built-in name (`claude-code`, `codex`, `opencode`, `pi`, `hermes`) cannot be
shadowed by a plugin.

Keep `install` idempotent, keep what it writes committable, and raise `daedalus.core.errors.DaedalusError`
for problems the user must fix.

## An adapter

Either a factory, `(cfg: dict) -> Adapter`, where `cfg` is the `adapter:` block of `.daedalus.yml`
(see [adapter-contract.md](adapter-contract.md)):

```python
def make_acme(cfg: dict) -> Adapter:
    return AcmeAdapter(model=cfg.get("model"))
```

or a preset dict, run by the generic `CommandAdapter` in the same shape as `PRESETS` in
`src/daedalus/adapters/command.py`:

```python
ACME = {
    "argv": ["acme", "exec"],
    "builder_args": ["--auto"],        # builder launches only
    "read_only_args": ["--read-only"], # review launches only
    "prompt_via": "arg",               # stdin | arg | message
    "parse": "text",
}
```

```toml
[project.entry-points."daedalus.adapters"]
acme = "acme_daedalus.adapter:make_acme"   # or ...:ACME
```

Then `adapter: {name: acme}` in `.daedalus.yml` selects it. Built-in presets are consulted first, so a
plugin cannot replace `claude-code`, `codex` or `aider`.

**Read-only reviews.** A third-party preset is never treated as locked. Only a preset that declares
`"locked_read_only": True` gets the built-in locked-profile rules (config may set only `name`, `model`,
`review_model` and a short allowlist of flags). Otherwise config `args` reach builder launches only, and
`daedalus plugins` shows the preset's read-only mode: `locked`, `read_only_args` (a flag list that cannot
be changed by config, but that Daedalus did not vet), or `none`. A factory's read-only behaviour is
`unknown (factory)`: your adapter must honour `AgentRequest.read_only` itself.

## Testing

Fake the entry points rather than installing a package. See `tests/unit/test_plugins.py`:

```python
import importlib.metadata
from types import SimpleNamespace

from daedalus import cli, plugins


def test_acme(monkeypatch, tmp_path):
    ep = SimpleNamespace(name="acme", load=lambda: INTEGRATION)
    monkeypatch.setattr(importlib.metadata, "entry_points",
                        lambda *, group: [ep] if group == "daedalus.harnesses" else [])
    plugins.reset_cache()  # discovery is cached
    assert cli.main(["-C", str(repo), "install", "acme"]) == 0
```

Or call `INTEGRATION.install(tmp_path)` directly and assert on the files and the returned lines.
