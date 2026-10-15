"""LanceDB integration coverage for the adaptive maintenance policy."""
from __future__ import annotations

import importlib.util

import pytest

from knowmate.rag.lance_maintenance import LanceMaintenanceConfig, LanceTableMaintenance


def test_config_uses_fragment_policy_and_ignores_mutation_keys() -> None:
    cfg = LanceMaintenanceConfig.from_mapping({
        "optimize_every_mutations": 1,
        "cycle_end_min_mutations": 1,
        "startup_optimize_when_small_fragments_reach": 77,
        "optimize_when_small_fragments_reach": 123,
    })
    assert cfg.optimize_when_small_fragments_reach == 123
    assert cfg.backlog_hard_limit_small_fragments == 1000
    assert cfg.backlog_finalize_small_fragments_reach == 300


@pytest.mark.skipif(not importlib.util.find_spec("lancedb"), reason="lancedb 미설치")
def test_real_lancedb_optimize_preserves_rows_and_reduces_small_fragments(tmp_path) -> None:
    import lancedb

    table = lancedb.connect(tmp_path).create_table("probe", data=[{"id": 0, "text": "seed"}])
    maintenance = LanceTableMaintenance(
        table, "probe", {"backlog_hard_limit_small_fragments": 20},
        db_path=tmp_path, state_dir=tmp_path / "maintenance",
    )
    assert maintenance.mark_backlog_active()
    for value in range(1, 21):
        table.add([{"id": value, "text": str(value)}])

    before = table.stats()["fragment_stats"]["num_small_fragments"]
    assert maintenance.checkpoint_hard_limit(ensure_durable=lambda: True)
    after = table.stats()["fragment_stats"]["num_small_fragments"]
    assert after < before
    assert table.count_rows() == 21


@pytest.mark.parametrize("failed_phase", ["before", "after"])
def test_memory_callback_failure_preserves_optimize_success_and_cooldown(tmp_path, failed_phase, caplog):
    """Observability must neither skip optimize nor overwrite its durable success."""
    class Table:
        def __init__(self):
            self.optimizations = 0

        def optimize(self):
            self.optimizations += 1

        def stats(self):
            return {"fragment_stats": {"num_small_fragments": 0}}

    table = Table()
    maintenance = LanceTableMaintenance(table, "probe", {}, db_path=tmp_path, state_dir=tmp_path / "state", now_fn=lambda: 100, wall_time_fn=lambda: 1000)
    callbacks = []

    def broken_callback(phase):
        callbacks.append(phase)
        if phase.startswith(failed_phase):
            raise ValueError("secret body /private/path.pdf")

    assert maintenance._run_optimize("test", 1000, None, broken_callback) is True
    assert callbacks == ["before_optimize_probe", "after_optimize_probe"]
    assert table.optimizations == 1
    assert maintenance._state["last_result"] == "success"
    assert maintenance._state["last_failure_wall_time"] is None
    assert maintenance._state["last_success_wall_time"] == 1000
    assert maintenance._failure_deadline == 100
    assert maintenance._steady_deadline == 100 + maintenance.config.min_optimize_interval_sec
    assert not maintenance.in_progress
    assert "error_type=ValueError" in caplog.text
    assert "secret body" not in caplog.text and "/private/path" not in caplog.text


def test_memory_callback_error_report_failure_still_preserves_optimize(tmp_path, monkeypatch):
    """Even a diagnostic warning handler failure cannot change optimize state."""
    from knowmate.rag import lance_maintenance as module
    from types import SimpleNamespace

    calls = []
    table = SimpleNamespace(optimize=lambda: calls.append("optimize"), stats=lambda: {"fragment_stats": {"num_small_fragments": 0}})
    maintenance = LanceTableMaintenance(table, "probe", {}, db_path=tmp_path, state_dir=tmp_path / "state")

    def fail(*_args):
        raise OSError("diagnostic failed")

    monkeypatch.setattr(module.logger, "warning", fail)
    assert maintenance._run_optimize("test", 1000, None, fail)
    assert calls == ["optimize"]
    assert maintenance._state["last_result"] == "success"
    assert maintenance._state["last_failure_wall_time"] is None
