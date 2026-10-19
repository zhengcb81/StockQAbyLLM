import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _dev_pins() -> dict:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = text.split("dev = [", 1)[1].split("]", 1)[0]
    return {name: version for name, version in re.findall(r'"([A-Za-z0-9_.-]+)==([^"]+)"', block)}


def _lock_pins() -> dict:
    pins = {}
    for line in (ROOT / "requirements-lock.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "==" in line:
            name, version = line.split("==", 1)
            pins[name.split("[", 1)[0].lower()] = version
    return pins


def test_check_tools_are_pinned_and_match_the_lock_file():
    dev, lock = _dev_pins(), _lock_pins()
    for name in ("black", "isort", "mypy", "bandit", "pytest"):
        assert name in dev, f"{name} must be pinned in dev extras"
        assert dev[name] == lock[name], f"{name} pin differs from requirements-lock.txt"
