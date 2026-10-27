"""Execute exclusion policy progress and admission frontend regressions."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_policy_frontend_executable_contract() -> None:
    """Verify pending, running, restored, partial and uncertain policy states."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the executable frontend harness")
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [node, str(Path(__file__).with_name("bulk_policy_ui_harness.js")),
         str(root / "knowmate/app/ui/app.js")],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "18 exclusion policy frontend scenarios passed" in result.stdout
