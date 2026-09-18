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
    assert maintenance.checkpoint_hard_limit()
    after = table.stats()["fragment_stats"]["num_small_fragments"]
    assert after < before
    assert table.count_rows() == 21
