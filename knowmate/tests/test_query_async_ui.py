"""Run the production JavaScript through controllable browser/Bridge boundary cases."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_query_frontend_executable_contract() -> None:
    """Exercise navigation, persistence acknowledgments and out-of-order query messages."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the executable frontend harness")
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [node, str(Path(__file__).with_name("query_async_ui_harness.js")),
         str(root / "knowmate/app/ui/app.js")],
        check=False, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "28 frontend scenarios passed" in result.stdout
