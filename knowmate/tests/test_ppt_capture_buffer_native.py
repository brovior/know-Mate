"""Office-free Windows x86/x64 transport gate; Linux skips do not authorize live."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native mapping/STA gate is UNEXECUTED")
@pytest.mark.parametrize("architecture", ["x86", "x64"])
@pytest.mark.parametrize("mode", ["buffer", "lazy"])
def test_real_mapping_publish_loss_and_lazy_binding(tmp_path: Path, architecture: str, mode: str) -> None:
    """Exercise real shared-memory publication and pre-Bind construction without Office."""
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework/v4.0.30319/csc.exe"
    if not compiler.is_file():
        pytest.skip(".NET compiler unavailable: native gate remains UNEXECUTED")
    executable = tmp_path / ("capture_" + architecture + ".exe")
    sources = [ROOT / "scripts/ppt_reader/PptCaptureBuffer.cs", ROOT / "scripts/ppt_reader/PptExternalTrace.cs", ROOT / "knowmate/tests/ppt_capture_buffer_harness.cs"]
    result = subprocess.run([str(compiler), "/nologo", "/utf8output", "/unsafe", "/target:exe", "/platform:" + architecture,
        "/define:AEGIS_PPT_EXTERNAL_DIAGNOSTIC;AEGIS_PPT_CAPTURE_DIAGNOSTIC", "/out:" + str(executable),
        *map(str, sources)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    result = subprocess.run([str(executable), mode], capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.strip()) == {"result": "PASS", "office_started": False}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows C# event-body regression is UNEXECUTED")
@pytest.mark.parametrize("architecture", ["x86", "x64"])
def test_cancel_argument_telemetry_preserves_actual_callback_values(tmp_path: Path, architecture: str) -> None:
    """Execute real sink bodies with local PIA type stubs and null document arguments."""
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework/v4.0.30319/csc.exe"
    if not compiler.is_file():
        pytest.skip(".NET compiler unavailable: event regression remains UNEXECUTED")
    executable = tmp_path / "capture_events.exe"
    result = subprocess.run([str(compiler), "/nologo", "/utf8output", "/target:exe", "/platform:" + architecture,
        "/out:" + str(executable), str(ROOT / "scripts/ppt_events/PptEventSink.cs"),
        str(ROOT / "knowmate/tests/ppt_capture_event_harness.cs")], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    result = subprocess.run([str(executable)], capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.strip()) == {"result": "PASS", "office_started": False}
