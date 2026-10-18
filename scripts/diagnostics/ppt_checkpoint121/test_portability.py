"""Offline portability checks; no Office activation or installed payload is required."""
import hashlib
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
import pytest
from make_fixture import create, TITLE, BODY


def test_fixture_is_synthetic_and_never_overwrites(tmp_path):
    output = tmp_path / "space directory" / "fixture"
    deck = create(output)
    before = deck.read_bytes()
    with zipfile.ZipFile(deck) as archive:
        xml = ET.fromstring(archive.read("ppt/slides/slide1.xml"))
    assert [e.text for e in xml.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}t")] == [TITLE, BODY]
    metadata = json.loads((output / "fixture.json").read_text())
    assert metadata["sha256"] == hashlib.sha256(before).hexdigest()
    assert not metadata["office_started"] and not metadata["historical_binary_reproduced"]
    with pytest.raises(FileExistsError):
        create(output)
    assert deck.read_bytes() == before


def test_historical_live_entry_is_locked_before_import_or_com(tmp_path):
    runner = Path(__file__).with_name("run.py")
    result = subprocess.run([sys.executable, str(runner), "--live"], cwd=tmp_path,
                            capture_output=True, timeout=10)
    assert result.returncode != 0
    assert b"handoff_live_not_reviewed" in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_preserved_sources_have_no_old_workstation_paths():
    root = Path(__file__).parent
    for name in ("run.py", "controller_support.py", "verify_pins.py", "observe_processes.py"):
        text = (root / name).read_text()
        assert "C:" + chr(92) + "Users" not in text
        assert "ppt-pair-121-122-ready/source-candidate121" not in text
    assert "pins['fixture_path']" in (root / "run.py").read_text()
    assert "SOURCE = Path(__file__).resolve().parents[3]" in (root / "controller_support.py").read_text()
