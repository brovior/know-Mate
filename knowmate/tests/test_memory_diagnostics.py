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
    )

    with caplog.at_level(logging.INFO, logger=module.__name__):
        diagnostics.start()
        diagnostics.log("after_mail")

    memory_lines = [record.message for record in caplog.records if "[memory]" in record.message]
    assert memory_lines == [
        "[memory] phase=after_mail private_mib=50.0 python_current_mib=10.0 "
        "python_peak_mib=20.0 arrow_current_mib=30.0 arrow_peak_mib=40.0 "
        "arrow_backend=mimalloc"
    ]


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
