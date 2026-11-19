"""admin_api's migrations match its models, whatever the project's DEFAULT_AUTO_FIELD."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("settings", ["tests.settings", "tests.settings_bigautofield"])
def test_no_missing_migrations(settings):
    cmd = [sys.executable, "-m", "django", "makemigrations", "admin_api", "--check", "--dry-run"]
    result = subprocess.run([*cmd, f"--settings={settings}"], check=False, cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
