"""수집 사이클 메모리 진단 테스트."""
from __future__ import annotations

import logging


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
    monkeypatch.setattr(
        module.tracemalloc,
        "get_traced_memory",
        lambda: (10 * 1024 * 1024, 20 * 1024 * 1024),
    )
    diagnostics = module.MemoryDiagnostics(
        True,
        private_bytes_reader=lambda: 50 * 1024 * 1024,
        arrow_pool_getter=_FakeArrowPool,
        pid=1234,
        cycle_id="test-cycle",
    )

    with caplog.at_level(logging.INFO, logger=module.__name__):
        diagnostics.start()
        diagnostics.log("after_mail")

    memory_lines = [record.message for record in caplog.records if " phase=after_mail " in record.message]
    assert memory_lines == [
        "[memory] pid=1234 cycle_id=test-cycle phase=after_mail private_mib=50.0 python_current_mib=10.0 "
        "python_peak_mib=20.0 arrow_current_mib=30.0 arrow_peak_mib=40.0 "
        "arrow_backend=mimalloc"
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


def test_mail_sample_does_not_take_tracemalloc_snapshot(monkeypatch):
    """50건 표본은 snapshot diff 대상이 아니어서 추가 할당을 만들지 않는다."""
    from knowmate.collector import memory_diagnostics as module

    monkeypatch.setattr(module.tracemalloc, "is_tracing", lambda: True)
    monkeypatch.setattr(module.tracemalloc, "get_traced_memory", lambda: (0, 0))
    monkeypatch.setattr(
        module.tracemalloc,
        "take_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("mail sample took a snapshot")),
    )
    diagnostics = module.MemoryDiagnostics(
        True,
        private_bytes_reader=lambda: None,
        arrow_pool_getter=_FakeArrowPool,
    )

    diagnostics.log("mail_sample", counters={"attempted": 50})


def test_disabled_diagnostics_do_not_trace_collect_or_measure(monkeypatch, caplog):
    """기본 비활성 상태는 tracemalloc·GC·메모리 조회를 전혀 실행하지 않는다."""
    from knowmate.collector import memory_diagnostics as module

    def fail(*_args, **_kwargs):
        raise AssertionError("비활성 진단이 계측 함수를 호출함")

    monkeypatch.setattr(module.tracemalloc, "is_tracing", fail)
    monkeypatch.setattr(module.tracemalloc, "start", fail)
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
    diagnostics = module.MemoryDiagnostics(True)
    monkeypatch.setattr(diagnostics, "log", lambda phase: events.append(phase))

    diagnostics.collect_and_log()

    assert events == ["gc", "after_gc_collect"]
