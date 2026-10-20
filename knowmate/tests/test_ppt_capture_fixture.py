"""Reject business documents and altered synthetic decks before any Office call."""
from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts.diagnostics.ppt_capture.fixture import create_fixture, verify_fixture
from scripts.diagnostics.ppt_capture.gate import digest


@pytest.fixture
def synthetic(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    """Generate an actual Office-free PPTX and pin its fresh provenance."""
    pytest.importorskip("pptx")
    path = create_fixture(tmp_path / "fixture")
    metadata = json.loads((path.parent / "fixture.json").read_text(encoding="utf-8"))
    pins = {"fixture_provenance_sha256": digest(path.parent / "fixture.json"),
            "fixture_text_length": metadata["text_length"],
            "fixture_text_sha256": metadata["text_sha256"]}
    return path, pins


def test_generated_fixture_is_accepted_and_output_is_never_reused(synthetic):
    path, pins = synthetic
    verify_fixture(path, pins)
    with pytest.raises(FileExistsError):
        create_fixture(path.parent)


def test_arbitrary_document_has_no_valid_generator_provenance(tmp_path: Path):
    pptx = pytest.importorskip("pptx")
    deck = pptx.Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[0])
    slide.shapes.title.text = "Business document"
    path = tmp_path / "business.pptx"
    deck.save(path)
    with pytest.raises(FileNotFoundError):
        verify_fixture(path, {})


@pytest.mark.parametrize("change", ["content", "external_relationship", "embedded_binary"])
def test_changed_deck_is_rejected_even_when_hashes_are_recomputed(synthetic, change):
    path, pins = synthetic
    with ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    if change == "content":
        parts["ppt/slides/slide1.xml"] = parts["ppt/slides/slide1.xml"].replace(
            b"Synthetic capture validation only.", b"Business document contents."
        )
    elif change == "external_relationship":
        parts["ppt/slides/_rels/slide1.xml.rels"] = (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            b'<Relationship Id="rIdExternal" Type="hyperlink" '
            b'Target="https://example.invalid/" TargetMode="External"/></Relationships>'
        )
    else:
        parts["ppt/embeddings/object.bin"] = b"embedded object"
    with ZipFile(path, "w") as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    metadata_path = path.parent / "fixture.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["sha256"] = digest(path)
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    pins["fixture_provenance_sha256"] = digest(metadata_path)
    reason = {"content": "capture_fixture_content",
              "external_relationship": "capture_fixture_external_relationship",
              "embedded_binary": "capture_fixture_member"}[change]
    with pytest.raises(ValueError, match=reason):
        verify_fixture(path, pins)
