"""Compile production-flow telemetry fixtures; elevation is an explicit manual gate."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


def test_child_production_flow_fixture_compiles(tmp_path: Path) -> None:
    """Keep actual Main/elevation/mutation control flow in a private admin fixture."""
    if os.name != "nt":
        pytest.skip("Windows/.NET Framework required")
    from knowmate.tests.ppt_child_flow_build import build_flow_fixture

    executable = build_flow_fixture(tmp_path / "flow")
    provenance = json.loads((executable.parent / "provenance.json").read_text(encoding="utf-8"))
    assert executable.is_file() and provenance["flow_boundaries"]
    assert not (executable.parent / "native").exists()
    escaped_worker = subprocess.run([str(executable), "worker", "root_owner", r"C:\Windows", "on", "/elevated-install", "33e979c6-c5c4-40af-845a-64c80517d1ae"], capture_output=True, timeout=15)
    assert escaped_worker.returncode == 1
    assert "fixed_worker_identity" in (executable.parent / "result.txt").read_text(encoding="utf-8")
    assert not (executable.parent / "native").exists()
    escaped_selector = subprocess.run([str(executable), "case", r"C:\Windows"], capture_output=True, timeout=15)
    assert escaped_selector.returncode == 1
    assert "fixed_fixture_only" in (executable.parent / "result.txt").read_text(encoding="utf-8")
    assert not (executable.parent / "native").exists()
    assert provenance["sources"]["PptSetupChildPipe.cs"]["original_sha256"] == provenance["sources"]["PptSetupChildPipe.cs"]["bound_sha256"]
    assert provenance["sources"]["PptSetupChildServer.cs"]["original_sha256"] == provenance["sources"]["PptSetupChildServer.cs"]["bound_sha256"]
