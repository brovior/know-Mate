"""PPT 비교 도구의 평문 비저장·오류 판정·순서 보존을 가짜 COM으로 검증한다."""
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import diag_ppt_read as cli
from scripts.ppt_probe import readers
from scripts.ppt_probe.fixture import create_pptx


def _reply(nodes=None, **changes):
    payload = {"schema": 1, "slide": 1, "seconds": 0.1,
               "counts": {"shapes": 2, "groups": 0, "tables": 1, "cells": 4, "errors": 0},
               "nodes": nodes or [{"kind": "text", "text": " SECRET_TITLE "},
                                  {"kind": "table", "cells": [["SECRET_A\r\nB", ""], ["", ""]]}]}
    payload.update(changes)
    return json.dumps(payload)


def test_decode_preserves_table_blanks_and_newline_contract():
    texts, counts, seconds = readers.decode_slide(_reply(), 1)
    assert texts == ["SECRET_TITLE", "SECRET_A  B | "]
    assert counts["cells"] == 4 and seconds == 0.1


@pytest.mark.parametrize("changes", [
    {"schema": 2}, {"slide": 2}, {"counts": {}}, {"seconds": float("nan")},
    {"seconds": float("inf")}, {"nodes": [{"kind": "table", "cells": [[123]]}]},
    {"nodes": [{"kind": "unknown", "text": "SECRET"}]},
])
def test_malformed_response_cannot_pass(changes):
    with pytest.raises(readers.ProbeFailure):
        readers.decode_slide(_reply(**changes), 1)


def test_comparison_detects_order_empty_slide_and_errors_without_content(capsys):
    reference = readers.Snapshot([["SECRET_A", "SECRET_B"], []])
    reordered = readers.Snapshot([["SECRET_B", "SECRET_A"], []])
    verdict = readers.compare(reference, reordered)
    assert not verdict["equal"] and verdict["mismatch_slides"] == [1]
    assert not readers.compare(reference, readers.Snapshot([["SECRET_A", "SECRET_B"]]))["equal"]
    partial = readers.Snapshot(reference.slides, {**reference.counts, "errors": 1})
    assert not readers.compare(partial, partial)["equal"]
    cli._emit(verdict)
    assert "SECRET" not in capsys.readouterr().out


def test_real_document_conversion_is_rejected_before_office(tmp_path, monkeypatch, capsys):
    path = tmp_path / "business.ppt"
    path.write_bytes(b"SECRET_ORIGINAL")
    monkeypatch.setattr(cli.com_reader, "_get_ppt_app", lambda: pytest.fail("Office must not start"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["--file", str(path), "--method", "conversion"])
    assert exc.value.code == 2
    assert path.read_bytes() == b"SECRET_ORIGINAL"
    assert list(tmp_path.iterdir()) == [path]
    assert "SECRET" not in capsys.readouterr().err


def test_self_test_needs_no_office_and_does_not_claim_performance(monkeypatch, capsys):
    monkeypatch.setattr(cli.com_reader, "_get_ppt_app", lambda: pytest.fail("Office must not start"))
    assert cli.main(["--self-test"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["equal"] and not output["performance_verified"]
    assert output["environment"] == "fake_contract"


def test_unavailable_addin_does_not_enable_or_install_it():
    class Addin:
        @property
        def Connect(self):
            return False

        @Connect.setter
        def Connect(self, value):
            pytest.fail("must not enable add-in")

    app = SimpleNamespace(COMAddIns=SimpleNamespace(Item=lambda name: Addin()))
    with pytest.raises(readers.ProbeFailure, match="addin_not_loaded"):
        cli._service(app)


def test_addin_reads_each_slide_once_and_keeps_plaintext_in_memory(tmp_path, caplog):
    calls = []

    def extract(prs, number):
        calls.append(number)
        return _reply(slide=number)

    prs = SimpleNamespace(Slides=SimpleNamespace(Count=2))
    result = readers.read_addin(prs, tmp_path / "deck.ppt", SimpleNamespace(ExtractSlide=extract))
    assert calls == [1, 2]
    assert result.slides[0] == result.slides[1]
    assert result.counts["cells"] == 8
    assert "SECRET" not in caplog.text
    assert list(tmp_path.iterdir()) == []


def test_opened_closes_document_on_read_failure_without_quit(tmp_path):
    actions = []
    prs = SimpleNamespace(Close=lambda: actions.append("close"))

    def open_document(path, **options):
        assert options == {"ReadOnly": True, "WithWindow": False}
        actions.append("open")
        return prs

    app = SimpleNamespace(Presentations=SimpleNamespace(Open=open_document))

    def fail(document):
        raise RuntimeError("SECRET_ERROR")

    with pytest.raises(RuntimeError):
        readers.opened(app, tmp_path / "deck.ppt", fail)
    assert actions == ["open", "close"]


def test_opened_does_not_requery_office_after_fatal_rpc(tmp_path):
    prs = SimpleNamespace(Close=lambda: pytest.fail("fatal RPC must not call Close"))
    app = SimpleNamespace(Presentations=SimpleNamespace(Open=lambda *args, **kwargs: prs))

    def fail(document):
        raise readers.com_reader.OfficeComPoisonError("POWERPNT.EXE", 0x80010108)

    with pytest.raises(readers.com_reader.OfficeComPoisonError):
        readers.opened(app, tmp_path / "deck.ppt", fail)


def test_close_failure_aborts_following_methods(tmp_path, monkeypatch):
    source = tmp_path / "synthetic.ppt"
    source.write_bytes(b"SYNTHETIC")
    monkeypatch.setattr(cli, "_service", lambda app: object())

    def fail(*args):
        raise readers.ProbeCloseFailure(RuntimeError("SECRET_CLOSE_ERROR"))

    monkeypatch.setattr(cli, "opened", fail)
    monkeypatch.setattr(cli, "convert_synthetic", lambda *args: pytest.fail("must abort"))
    args = SimpleNamespace(method="all", synthetic=True, repeat=1)
    with pytest.raises(readers.ProbeCloseFailure):
        cli.benchmark(None, source, args, tmp_path)


def test_open_none_cannot_be_accepted_as_success(tmp_path):
    app = SimpleNamespace(Presentations=SimpleNamespace(Open=lambda *args, **kwargs: None))
    with pytest.raises(readers.com_reader.OfficeComPoisonError):
        readers.opened(app, tmp_path / "deck.ppt", lambda prs: pytest.fail("must not read"))


def test_benchmark_alternates_order_and_includes_conversion_time(tmp_path, monkeypatch, capsys):
    source = tmp_path / "synthetic.ppt"
    source.write_bytes(b"SYNTHETIC")
    calls = []

    def opened(app, path, reader):
        calls.append("baseline")
        return readers.Snapshot([["SECRET"]], seconds={"total": 10.0})

    def convert(app, path, output):
        calls.append("conversion")
        return readers.Snapshot([["SECRET"]], seconds={"total": 5.0, "save": 4.0, "parse": 1.0})

    monkeypatch.setattr(cli, "opened", opened)
    monkeypatch.setattr(cli, "convert_synthetic", convert)
    args = SimpleNamespace(method="conversion", synthetic=True, repeat=2)
    assert cli.benchmark(None, source, args, tmp_path) == 0
    assert calls == ["baseline", "conversion", "conversion", "baseline"]
    output = capsys.readouterr().out
    assert "SECRET" not in output
    records = [json.loads(line) for line in output.splitlines()]
    assert records[0]["speedup"] == 2
    assert records[0]["seconds"]["save"] == 4
    assert records[-1]["source_unchanged"]


def test_real_all_marks_conversion_blocked_and_addin_missing_incomplete(tmp_path, monkeypatch, capsys):
    source = tmp_path / "business.pptx"
    source.write_bytes(b"SECRET")
    monkeypatch.setattr(cli, "convert_synthetic", lambda *args: pytest.fail("no real SaveCopyAs"))
    args = SimpleNamespace(method="all", synthetic=False, repeat=1)
    assert cli.benchmark(SimpleNamespace(), source, args, tmp_path) == 2
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert records[0]["status"] == "blocked"
    assert records[1]["status"] == "unavailable"
    assert not records[-1]["performance_verified"]
    assert list(tmp_path.iterdir()) == [source]


def test_failed_conversion_still_compares_addin_without_leaking_error(tmp_path, monkeypatch, capsys):
    source = tmp_path / "synthetic.ppt"
    source.write_bytes(b"SYNTHETIC")
    monkeypatch.setattr(cli, "_service", lambda app: object())
    monkeypatch.setattr(cli, "opened", lambda *args: readers.Snapshot([["SECRET"]], seconds={"total": 1}))

    def fail(*args):
        raise ValueError("SECRET_IN_EXCEPTION")

    monkeypatch.setattr(cli, "convert_synthetic", fail)
    args = SimpleNamespace(method="all", synthetic=True, repeat=1)
    assert cli.benchmark(None, source, args, tmp_path) == 1
    output = capsys.readouterr().out
    assert "SECRET" not in output
    records = [json.loads(line) for line in output.splitlines()]
    assert records[0]["method"] == "conversion" and records[0]["status"] == "error"
    assert records[1]["method"] == "addin" and records[1]["status"] == "pass"


def test_synthetic_fixture_covers_merged_cells_empty_slides_and_nested_groups(tmp_path):
    path = tmp_path / "synthetic.pptx"
    create_pptx(path, rows=4, columns=3, slides=2)
    result = readers.read_plain(path)
    assert len(result.slides) == 3 and result.slides[-1] == []
    assert result.counts == {"shapes": 12, "groups": 4, "tables": 2, "cells": 24, "errors": 0}
    assert "첫 줄 둘째 줄" in result.text
    assert "그룹 첫 도형\n중첩 그룹 도형" in result.text


def test_main_sanitizes_failure_and_only_releases_reference(tmp_path, monkeypatch, capsys):
    path = tmp_path / "business.ppt"
    path.write_bytes(b"SYNTHETIC")

    def fail():
        raise RuntimeError("SECRET_DOCUMENT_TEXT")

    monkeypatch.setattr(cli.com_reader, "_get_ppt_app", fail)
    assert cli.main(["--file", str(path), "--method", "addin"]) == 1
    assert "SECRET" not in capsys.readouterr().out
    assert cli.com_reader._tls.ppt is None


def test_conversion_uses_savecopyas_and_never_overwrites_source(tmp_path, monkeypatch):
    source = tmp_path / "synthetic.ppt"
    destination = tmp_path / "converted.pptx"
    calls = []
    prs = SimpleNamespace(SaveCopyAs=lambda *args: calls.append(args), Close=lambda: None)
    app = SimpleNamespace(Presentations=SimpleNamespace(Open=lambda *args, **kwargs: prs))
    monkeypatch.setattr(readers, "read_plain", lambda path: readers.Snapshot([["SYNTHETIC"]]))
    result = readers.convert_synthetic(app, source, destination)
    assert calls == [(str(destination.resolve()), 24)]
    assert "save" in result.seconds and result.seconds["total"] >= result.seconds["save"]


def test_csharp_internal_reader_contract_without_office_or_registration(tmp_path):
    compiler = Path(os.environ.get("WINDIR", "")) / "Microsoft.NET/Framework/v4.0.30319/csc.exe"
    if os.name != "nt" or not compiler.is_file():
        pytest.skip("Windows .NET Framework compiler required; no Office needed")
    root = Path(__file__).resolve().parents[2]
    dll = tmp_path / "PptReadProbe.dll"
    harness = tmp_path / "ProbeHarness.exe"
    commands = [
        [str(compiler), "/nologo", "/target:library", f"/out:{dll}",
         "/reference:Microsoft.CSharp.dll", "/reference:System.Core.dll",
         "/reference:System.Web.Extensions.dll", str(root / "scripts/ppt_addin/PptReadProbe.cs")],
        [str(compiler), "/nologo", f"/out:{harness}", str(root / "knowmate/tests/ppt_probe_harness.cs")],
        [str(harness), str(dll)],
    ]
    for command in commands:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "SECRET" not in result.stdout + result.stderr
