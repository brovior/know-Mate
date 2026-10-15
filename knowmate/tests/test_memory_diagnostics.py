"""수집 사이클 메모리 진단 테스트."""
from __future__ import annotations

import logging

import pytest


class _FakeArrowPool:
    backend_name = "mimalloc"

    def bytes_allocated(self) -> int:
        return 30 * 1024 * 1024

    def max_memory(self) -> int:
        return 40 * 1024 * 1024


def test_logs_private_python_and_arrow_in_one_info_line(monkeypatch, caplog):
    """활성화 시 세 메모리 영역과 backend를 한 줄로 기록한다."""
    from knowmate.collector import memory_diagnostics as module

    monkeypatch.setattr(module.tracemalloc, "is_tracing", lambda: True)
    monkeypatch.setattr(module.tracemalloc, "reset_peak", lambda: None)
    monkeypatch.setattr(module.tracemalloc, "get_tracemalloc_memory", lambda: 3 * 1024 * 1024)
    monkeypatch.setattr(
        module.tracemalloc,
        "get_traced_memory",
        lambda: (10 * 1024 * 1024, 20 * 1024 * 1024),
    )
    diagnostics = module.MemoryDiagnostics(
        True, mode="detailed",
        private_bytes_reader=lambda: 50 * 1024 * 1024,
        arrow_pool_getter=_FakeArrowPool,
        pid=1234,
        cycle_id="test-cycle",
    )

    with caplog.at_level(logging.INFO, logger=module.__name__):
        diagnostics.start()
        diagnostics.log("after_mail")

    memory_lines = [record.message for record in caplog.records if " phase=after_mail private_mib=" in record.message]
    assert memory_lines == [
        "[memory] pid=1234 cycle_id=test-cycle phase=after_mail private_mib=50.0 python_current_mib=10.0 "
        "python_peak_mib=20.0 arrow_current_mib=30.0 arrow_peak_mib=40.0 "
        "arrow_backend=mimalloc private_delta_mib=0.0 python_scope=external_start_unknown "
        "trace_started_at_ns=n/a tracemalloc_mib=3.0 mode=detailed"
    ]


def test_mail_sample_includes_cycle_identity_and_counters(monkeypatch, caplog):
    """메일 표본은 PID·사이클과 익명 누적 수치만 함께 남긴다."""
    from knowmate.collector import memory_diagnostics as module

    monkeypatch.setattr(module.tracemalloc, "is_tracing", lambda: True)
    monkeypatch.setattr(module.tracemalloc, "get_traced_memory", lambda: (0, 0))
    diagnostics = module.MemoryDiagnostics(
        True,
        private_bytes_reader=lambda: None,
        arrow_pool_getter=_FakeArrowPool,
        pid=1234,
        cycle_id="test-cycle",
    )

    with caplog.at_level(logging.INFO, logger=module.__name__):
        diagnostics.log("mail_sample", counters={"commits": 4, "attempted": 50})

    assert any(
        "pid=1234 cycle_id=test-cycle phase=mail_sample" in record.message
        and "attempted=50 commits=4" in record.message
        for record in caplog.records
    )


@pytest.mark.parametrize("phase", [
    "mail_sample", "before_mail_discovery", "after_mail_discovery",
    "before_mail_embed", "after_mail_embed", "before_mail_commit", "after_mail_commit",
])
def test_mail_sample_does_not_take_tracemalloc_snapshot(monkeypatch, phase):
    """가벼운 메일 표본은 전체 추적 기록의 스냅샷을 추가 생성하지 않는다."""
    from knowmate.collector import memory_diagnostics as module

    monkeypatch.setattr(module.tracemalloc, "is_tracing", lambda: True)
    monkeypatch.setattr(module.tracemalloc, "get_traced_memory", lambda: (0, 0))
    snapshots = []
    monkeypatch.setattr(module.tracemalloc, "take_snapshot", lambda: snapshots.append(object()))
    diagnostics = module.MemoryDiagnostics(
        True,
        private_bytes_reader=lambda: None,
        arrow_pool_getter=_FakeArrowPool,
    )

    diagnostics.log(phase, counters={"attempted": 50})
    assert snapshots == []


def test_disabled_diagnostics_do_not_trace_collect_or_measure(monkeypatch, caplog):
    """기본 비활성 상태는 tracemalloc·GC·메모리 조회를 전혀 실행하지 않는다."""
    from knowmate.collector import memory_diagnostics as module

    def fail(*_args, **_kwargs):
        raise AssertionError("비활성 진단이 계측 함수를 호출함")

    monkeypatch.setattr(module.tracemalloc, "is_tracing", fail)
    monkeypatch.setattr(module.tracemalloc, "start", fail)
    monkeypatch.setattr(module.tracemalloc, "get_tracemalloc_memory", fail)
    monkeypatch.setattr(module.gc, "collect", fail)
    diagnostics = module.MemoryDiagnostics(
        False,
        private_bytes_reader=fail,
        arrow_pool_getter=fail,
    )

    with caplog.at_level(logging.INFO, logger=module.__name__):
        diagnostics.start()
        diagnostics.log("cycle_start")
        diagnostics.collect_and_log()
        diagnostics.stop()

    assert not caplog.records


def test_collect_and_log_runs_gc_before_final_sample(monkeypatch):
    """마지막 표본은 진단 모드의 gc.collect 이후에 남긴다."""
    from knowmate.collector import memory_diagnostics as module

    events: list[str] = []
    monkeypatch.setattr(module.gc, "collect", lambda: events.append("gc"))
    diagnostics = module.MemoryDiagnostics(True, mode="detailed")
    monkeypatch.setattr(diagnostics, "log", lambda phase: events.append(phase))

    diagnostics.collect_and_log()

    assert events == ["before_gc_collect", "gc", "after_gc_collect"]


def test_owned_trace_records_scope_overhead_and_release_order(monkeypatch, caplog):
    """Trace restarts remain explicit; retained allocations are not total Python memory."""
    from knowmate.collector import memory_diagnostics as module

    active = [False]
    actions = []
    monkeypatch.setattr(module.tracemalloc, "is_tracing", lambda: active[0])
    monkeypatch.setattr(module.tracemalloc, "start", lambda depth: (active.__setitem__(0, True), actions.append(("start", depth))))
    monkeypatch.setattr(module.tracemalloc, "stop", lambda: (active.__setitem__(0, False), actions.append(("stop",))))
    monkeypatch.setattr(module.tracemalloc, "reset_peak", lambda: None)
    monkeypatch.setattr(module.tracemalloc, "take_snapshot", object)
    monkeypatch.setattr(module.tracemalloc, "get_traced_memory", lambda: (1024, 2048))
    monkeypatch.setattr(module.tracemalloc, "get_tracemalloc_memory", lambda: 1024 * 1024)
    monkeypatch.setattr(module.time, "time_ns", lambda: 42)
    readings = iter([100, 101, 105, 106, 150, 151, 147, 147])
    diagnostics = module.MemoryDiagnostics(True, mode="detailed", private_bytes_reader=lambda: next(readings) * 1024 * 1024, arrow_pool_getter=_FakeArrowPool)

    with caplog.at_level(logging.INFO, logger=module.__name__):
        diagnostics.start()
        diagnostics.log("mail_sample")
        diagnostics.stop()

    assert actions == [("start", 10), ("stop",)]
    messages = [record.message for record in caplog.records]
    assert any("phase=mail_sample " in line and "python_scope=cycle_since_start" in line and "trace_started_at_ns=42" in line and "tracemalloc_mib=1.0" in line for line in messages)
    assert any("phase=after_tracemalloc_stop " in line and "private_delta_mib=-4.0" in line and "python_current_mib=n/a" in line and "python_scope=inactive" in line for line in messages)
    assert "phase=cycle_diagnostics_released " in messages[-1]
    assert diagnostics._cycle_snapshot is None


def test_external_trace_is_preserved_and_start_time_is_unknown(monkeypatch, caplog):
    """A diagnostic must never reset or stop somebody else's tracer."""
    from knowmate.collector import memory_diagnostics as module

    monkeypatch.setattr(module.tracemalloc, "is_tracing", lambda: True)
    monkeypatch.setattr(module.tracemalloc, "start", lambda *_: (_ for _ in ()).throw(AssertionError("started external tracer")))
    monkeypatch.setattr(module.tracemalloc, "stop", lambda: (_ for _ in ()).throw(AssertionError("stopped external tracer")))
    monkeypatch.setattr(module.tracemalloc, "reset_peak", lambda: (_ for _ in ()).throw(AssertionError("reset external peak")))
    monkeypatch.setattr(module.tracemalloc, "take_snapshot", object)
    diagnostics = module.MemoryDiagnostics(True, mode="detailed", private_bytes_reader=lambda: None, arrow_pool_getter=_FakeArrowPool)
    with caplog.at_level(logging.INFO, logger=module.__name__):
        diagnostics.start()
        diagnostics.stop()
    assert any("phase=tracemalloc_preserved_external " in record.message and "python_scope=external_start_unknown" in record.message and "trace_started_at_ns=n/a" in record.message for record in caplog.records)


def test_snapshot_failure_has_end_marker_and_does_not_escape(monkeypatch, caplog):
    """A missing end marker isolates native failures; Python errors still log an end."""
    from knowmate.collector import memory_diagnostics as module

    monkeypatch.setattr(module.tracemalloc, "is_tracing", lambda: True)
    monkeypatch.setattr(module.tracemalloc, "take_snapshot", lambda: (_ for _ in ()).throw(RuntimeError("snapshot failed")))
    diagnostics = module.MemoryDiagnostics(True, mode="detailed", private_bytes_reader=lambda: None, arrow_pool_getter=_FakeArrowPool)
    with caplog.at_level(logging.INFO, logger=module.__name__):
        diagnostics.log("after_mail")
    markers = [record.message for record in caplog.records if "snapshot_diff phase=after_mail action=" in record.message]
    assert len(markers) == 2
    assert markers[0].endswith("action=start")
    assert markers[1].endswith("action=end status=failed")


@pytest.mark.parametrize("enabled", [False, True])
def test_off_and_basic_never_call_tracer_or_gc_in_any_phase(monkeypatch, caplog, enabled):
    """Even an active external tracer must remain completely uninspected in basic."""
    from knowmate.collector import memory_diagnostics as module

    calls = []

    def forbidden(name):
        def fail(*_args, **_kwargs):
            calls.append(name)
            raise AssertionError(name)
        return fail

    for name in ("is_tracing", "start", "stop", "reset_peak", "get_traced_memory",
                 "get_tracemalloc_memory", "get_traceback_limit", "take_snapshot"):
        monkeypatch.setattr(module.tracemalloc, name, forbidden(name))
    monkeypatch.setattr(module.gc, "collect", forbidden("gc"))
    measurements = []
    diagnostic = module.MemoryDiagnostics(
        enabled,
        private_bytes_reader=lambda: measurements.append("private"),
        arrow_pool_getter=lambda: measurements.append("arrow"),
    )
    with caplog.at_level(logging.INFO, logger=module.__name__):
        diagnostic.start()
        for phase in ("cycle_start", "before_mail_discovery", "after_mail_discovery", "mail_sample",
                      "before_mail_embed", "after_mail_embed", "before_mail_commit", "after_mail_commit",
                      "after_document_indexing", "after_documents", "before_optimize_chunks",
                      "after_optimize_chunks", "after_mail", "before_com_cleanup", "after_com_cleanup"):
            diagnostic.log(phase, counters={"attempted": 50})
        diagnostic.collect_and_log()
        diagnostic.stop()
    assert calls == []
    if enabled:
        assert measurements
        assert all("python_scope=not_measured" in r.message for r in caplog.records)
        assert any("phase=cycle_resources_released " in r.message for r in caplog.records)
        assert not any(token in r.message for r in caplog.records for token in ("gc_collect", "tracemalloc_start", "tracemalloc_stop", "snapshot"))
    else:
        assert measurements == []
        assert not caplog.records


def test_basic_missing_arrow_does_not_import_it(monkeypatch, caplog):
    """A diagnostic-only process does not load PyArrow just to measure it."""
    import builtins
    import sys
    from knowmate.collector import memory_diagnostics as module

    monkeypatch.delitem(sys.modules, "pyarrow", raising=False)
    original_import = builtins.__import__
    calls = []

    def observed_import(name, *args, **kwargs):
        if name == "pyarrow" or name.startswith("pyarrow."):
            calls.append(name)
            raise AssertionError("unexpected PyArrow import")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", observed_import)
    diagnostic = module.MemoryDiagnostics(True, private_bytes_reader=lambda: None)
    with caplog.at_level(logging.INFO, logger=module.__name__):
        diagnostic.start()
        diagnostic.log("cycle_start")
        diagnostic.collect_and_log()
        diagnostic.stop()
    assert calls == []
    assert all("arrow_current_mib=n/a arrow_peak_mib=n/a arrow_backend=n/a" in r.message for r in caplog.records)


def test_basic_uses_already_loaded_arrow_pool(monkeypatch, caplog):
    """Loaded Arrow current/peak remain available without tracing."""
    import sys
    from types import SimpleNamespace
    from knowmate.collector import memory_diagnostics as module

    monkeypatch.setitem(sys.modules, "pyarrow", SimpleNamespace(default_memory_pool=_FakeArrowPool))
    diagnostic = module.MemoryDiagnostics(True, private_bytes_reader=lambda: None)
    with caplog.at_level(logging.INFO, logger=module.__name__):
        diagnostic.log("cycle_start")
    assert "arrow_current_mib=30.0 arrow_peak_mib=40.0 arrow_backend=mimalloc" in caplog.text


def test_failed_private_sample_breaks_delta_chain_and_redacts_exception(caplog):
    """A failed reading and the next valid reading both have unknown delta."""
    from knowmate.collector import memory_diagnostics as module

    readings = iter([100, OSError("secret body C:/Users/private/file.pdf"), 110, 115])

    def read():
        value = next(readings)
        if isinstance(value, Exception):
            raise value
        return value * 1024 * 1024

    diagnostic = module.MemoryDiagnostics(True, private_bytes_reader=read, arrow_pool_getter=lambda: None)
    with caplog.at_level(logging.INFO, logger=module.__name__):
        for _ in range(4):
            diagnostic.log("mail_sample")
    messages = [r.message for r in caplog.records if "phase=mail_sample " in r.message]
    assert ["private_delta_mib=n/a" in message for message in messages] == [True, True, True, False]
    assert "private_delta_mib=5.0" in messages[-1]
    assert "error_type=OSError" in caplog.text
    assert "secret" not in caplog.text and "C:/Users" not in caplog.text


def test_sample_failure_does_not_escape_and_owned_stop_releases_snapshots(monkeypatch, caplog):
    """Formatting/snapshot failure must not retain state or interrupt owned stop."""
    from knowmate.collector import memory_diagnostics as module

    active = [False]
    actions = []
    monkeypatch.setattr(module.tracemalloc, "is_tracing", lambda: active[0])
    monkeypatch.setattr(module.tracemalloc, "start", lambda depth: active.__setitem__(0, True))
    monkeypatch.setattr(module.tracemalloc, "stop", lambda: (actions.append("stop"), active.__setitem__(0, False)))
    monkeypatch.setattr(module.tracemalloc, "reset_peak", lambda: None)
    monkeypatch.setattr(module.tracemalloc, "take_snapshot", object)
    diagnostic = module.MemoryDiagnostics(True, mode="detailed", private_bytes_reader=lambda: None, arrow_pool_getter=lambda: None)
    diagnostic.start()
    monkeypatch.setattr(diagnostic, "_log", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("secret body")))
    with caplog.at_level(logging.INFO, logger=module.__name__):
        diagnostic.log("after_mail")
        diagnostic.stop()
    assert actions == ["stop"]
    assert diagnostic._cycle_snapshot is None
    assert diagnostic._before_optimize_snapshot is None
    assert not diagnostic._started and not diagnostic._owns_tracing
    assert "error_type=ValueError" in caplog.text and "secret body" not in caplog.text


def test_diagnostic_failure_report_cannot_escape_a_broken_log_handler(monkeypatch):
    """The optional diagnostic's own error report is also best-effort."""
    from knowmate.collector import memory_diagnostics as module

    def fail(*_args, **_kwargs):
        raise OSError("broken diagnostic handler")

    monkeypatch.setattr(module.logger, "warning", fail)
    diagnostic = module.MemoryDiagnostics(True, private_bytes_reader=fail, arrow_pool_getter=fail)
    diagnostic.start()
    diagnostic.log("after_mail")
    diagnostic.collect_and_log()
    diagnostic.stop()
    assert not diagnostic._started


def test_partial_arrow_failure_reports_all_arrow_values_unknown(caplog):
    """A pool read failure cannot mix stale partial numbers into a valid sample."""
    from knowmate.collector import memory_diagnostics as module

    class BrokenPool(_FakeArrowPool):
        def max_memory(self):
            raise OSError("private body in arrow exception")

    diagnostic = module.MemoryDiagnostics(True, private_bytes_reader=lambda: 1024, arrow_pool_getter=BrokenPool)
    with caplog.at_level(logging.INFO, logger=module.__name__):
        diagnostic.log("before_optimize_chunks")
    assert "arrow_current_mib=n/a arrow_peak_mib=n/a arrow_backend=n/a" in caplog.text
    assert "error_type=OSError" in caplog.text
    assert "private body" not in caplog.text
