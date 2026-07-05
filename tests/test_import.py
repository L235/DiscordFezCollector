"""Smoke tests: importing the application must succeed and be side-effect free."""

import os
import subprocess
import sys
from pathlib import Path


def test_imports():
    from src.main import bot

    assert bot is not None

    from src.config import CONFIG

    assert isinstance(CONFIG, dict)


def test_import_has_no_filesystem_side_effects(tmp_path):
    """Importing the app must not create state files or log directories.

    Runs in a subprocess with a clean cwd so we observe a fresh import, not
    the modules already loaded by this test session.
    """
    repo_root = Path(__file__).resolve().parent.parent
    env = os.environ | {"PYTHONPATH": str(repo_root)}
    result = subprocess.run(
        [sys.executable, "-c", "import src.main"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    created = [p for p in tmp_path.iterdir()]
    assert created == [], f"import created files: {created}"
