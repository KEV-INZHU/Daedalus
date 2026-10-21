"""A tiny stdlib test runner, so the demo needs no pytest. Runs every test_* function in tests/."""
import importlib.util
import pathlib
import sys
import traceback

sys.path.insert(0, ".")
failed = passed = 0
for path in sorted(pathlib.Path("tests").glob("test_*.py")):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in sorted(n for n in vars(mod) if n.startswith("test_")):
        try:
            getattr(mod, name)()
            passed += 1
        except Exception:
            failed += 1
            print(f"FAILED {path}::{name}")
            traceback.print_exc()
print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
