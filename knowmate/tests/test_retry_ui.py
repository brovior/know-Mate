"""Execute the production failure-panel JavaScript with controllable promises."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_failure_frontend_executable_contract() -> None:
    """Check live refresh, stale responses, filters, timers and retry admission."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the executable frontend harness")
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [node, str(Path(__file__).with_name("retry_ui_harness.js")),
         str(root / "knowmate/app/ui/app.js")],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "10 failure frontend scenarios passed" in result.stdout
