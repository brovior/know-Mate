"""Trace transport loss must never be confused with successful host execution."""
import importlib.util
from pathlib import Path
import pytest

PATH = Path(__file__).resolve().parents[2] / "scripts/ppt_reader/ppt_trace_report.py"
SPEC = importlib.util.spec_from_file_location("ppt_trace_report", PATH)
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)
RUN = "1234567890abcdef1234567890abcdef"


def frame(slot=0, incomplete=0, dropped=0):
    return (f"schema=2 run={RUN} pid=42 creation=123 frequency=10000000 snapshot={slot} "
            f"incomplete={incomplete} dropped={dropped} first_stage=3 first_reason=2\n"
            "0 100 Identity Refused 2 0 0\nEND count=1\n")


@pytest.mark.parametrize("case", ["processloss", "writefail", "partial", "wrongidentity", "unknown", "overflow", "terminal", "latecleanup"])
def test_terminal_evidence_loss_is_explicit(tmp_path, case):
    if case not in ("processloss", "writefail"):
        text = frame(incomplete=int(case in ("unknown", "latecleanup")), dropped=int(case == "overflow"))
        if case == "partial":
            text = text.split("END")[0]
        if case == "wrongidentity":
            text = text.replace("creation=123", "creation=999")
        (tmp_path / "host-42-external-0.log").write_text(text)
        if case == "latecleanup":
            (tmp_path / "host-42-external-1.log").write_text(frame(slot=1))
    result = REPORT.read_terminal_trace(tmp_path, RUN, 42, 123)
    assert result["evidence_complete"] == (case in ("terminal", "latecleanup"))
    assert (result["host_trace"] == "recovered") == (case in ("terminal", "latecleanup", "overflow"))
    assert result["stages"]["Ack"]["reached"] == (
        "not_reached" if case in ("terminal", "latecleanup") else "unknown")


@pytest.mark.parametrize("earlier", ["missing", "arrives_last", "dropped"])
def test_cumulative_final_slot_is_selected_independent_of_arrival(tmp_path, earlier):
    """A complete cumulative slot 1 stands alone; loss never proves non-reach."""
    final = frame(slot=1, dropped=int(earlier == "dropped")).replace(
        "END count=1", "1 200 Completion Success 0 0 0\nEND count=2")
    (tmp_path / "host-42-external-1.log").write_text(final)
    if earlier != "missing":
        (tmp_path / "host-42-external-0.log").write_text(
            frame(incomplete=1, dropped=int(earlier == "dropped")))
    result = REPORT.read_terminal_trace(tmp_path, RUN, 42, 123)
    assert result["frames"][-1]["slot"] == 1
    assert result["stages"]["Completion"]["reached"] == "yes"
    assert result["evidence_complete"] == (earlier != "dropped")
    assert result["stages"]["Ack"]["reached"] == (
        "unknown" if earlier == "dropped" else "not_reached")
