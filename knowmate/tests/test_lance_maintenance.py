"""LanceDB 주기 optimize 정책의 경계·실패·실제 fragment 회귀 테스트."""
from __future__ import annotations

import importlib.util

import pytest

from knowmate.rag.lance_maintenance import (
    LanceMaintenanceConfig,
    LanceTableMaintenance,
)


class _Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class _Table:
    def __init__(self, small=0, after_small=1, fail_optimize=0, fail_stats=0):
        self.small = small
        self.after_small = after_small
        self.fail_optimize = fail_optimize
        self.fail_stats = fail_stats
        self.optimize_calls = 0

    def stats(self):
        if self.fail_stats:
            self.fail_stats -= 1
            raise RuntimeError("stats failed")
        return {
            "fragment_stats": {
                "num_fragments": self.small,
                "num_small_fragments": self.small,
            }
        }

    def optimize(self):
        self.optimize_calls += 1
        if self.fail_optimize:
            self.fail_optimize -= 1
            raise RuntimeError("optimize failed")
        self.small = self.after_small


def _maintenance(table=None, clock=None, **overrides):
    cfg = {
        "enabled": True,
        "optimize_every_mutations": 100,
        "startup_optimize_when_small_fragments_reach": 100,
        "cycle_end_min_mutations": 20,
        "failure_cooldown_sec": 300,
        **overrides,
    }
    return LanceTableMaintenance(table or _Table(), "chunks", cfg, now_fn=clock or _Clock())


def test_periodic_runs_at_100_not_99_and_resets_counter():
    table = _Table()
    maintenance = _maintenance(table)
    maintenance.record_mutation(99)
    assert not maintenance.periodic_due()
    maintenance.record_mutation()
    assert maintenance.periodic_due()

    assert maintenance.run_periodic()

    assert table.optimize_calls == 1
    assert maintenance.mutations_since_optimize == 0


def test_startup_fragment_check_runs_once_even_when_optimize_has_no_effect():
    table = _Table(small=120, after_small=120)
    maintenance = _maintenance(table)

    assert maintenance.run_startup_check()
    assert not maintenance.run_startup_check()

    maintenance.record_mutation()
    assert not maintenance.run_startup_check()
    assert table.optimize_calls == 1


def test_optimize_failure_keeps_counter_and_honors_cooldown():
    clock = _Clock()
    table = _Table(fail_optimize=1)
    maintenance = _maintenance(table, clock)
    maintenance.record_mutation(100)

    assert not maintenance.run_periodic()
    assert maintenance.mutations_since_optimize == 100
    clock.value = 299
    assert not maintenance.run_periodic()
    clock.value = 300
    assert maintenance.run_periodic()
    assert table.optimize_calls == 2


def test_startup_optimize_failure_retries_after_cooldown():
    clock = _Clock()
    table = _Table(small=120, fail_optimize=1)
    maintenance = _maintenance(table, clock)

    assert not maintenance.run_startup_check()
    assert not maintenance.startup_checked
    clock.value = 299
    assert not maintenance.run_startup_check()
    clock.value = 300
    assert maintenance.run_startup_check()
    assert maintenance.startup_checked
    assert table.optimize_calls == 2


def test_cycle_end_threshold_and_disabled_zero():
    first = _maintenance(_Table())
    first.record_mutation(19)
    assert not first.run_cycle_end()
    first.record_mutation()
    assert first.run_cycle_end()

    disabled = _maintenance(_Table(), cycle_end_min_mutations=0)
    disabled.record_mutation(100)
    assert not disabled.run_cycle_end()


def test_stats_failure_is_nonfatal_and_does_not_force_startup_optimize():
    clock = _Clock()
    table = _Table(fail_stats=1)
    maintenance = _maintenance(table, clock)

    assert not maintenance.run_startup_check()
    assert table.optimize_calls == 0
    clock.value = 300
    assert not maintenance.run_startup_check()
    assert maintenance.startup_checked


def test_cancelled_checkpoint_never_starts_optimize():
    table = _Table(small=100)
    maintenance = _maintenance(table)
    maintenance.record_mutation(100)

    assert not maintenance.run_startup_check(cancelled=lambda: True)
    assert not maintenance.run_periodic(cancelled=lambda: True)
    assert table.optimize_calls == 0


def test_config_rejects_bool_as_integer_and_bad_values():
    cfg = LanceMaintenanceConfig.from_mapping({
        "optimize_every_mutations": True,
        "startup_optimize_when_small_fragments_reach": -1,
        "cycle_end_min_mutations": "20",
        "failure_cooldown_sec": 0,
    })

    assert cfg.optimize_every_mutations == 100
    assert cfg.startup_optimize_when_small_fragments_reach == 100
    assert cfg.cycle_end_min_mutations == 20
    assert cfg.failure_cooldown_sec == 300


def test_document_and_mail_delete_wrappers_record_mutations():
    from knowmate.rag.email_indexer import EmailIndexer
    from knowmate.rag.indexer import Indexer

    class DeleteTable(_Table):
        def __init__(self):
            super().__init__()
            self.where = []

        def delete(self, where):
            self.where.append(where)

    document_table = DeleteTable()
    document = Indexer.__new__(Indexer)
    document._table = document_table
    document._maintenance = _maintenance(document_table)
    document.delete_file_chunks("C:/O'Brien/report.docx")

    mail_table = DeleteTable()
    mail = EmailIndexer.__new__(EmailIndexer)
    mail.table = mail_table
    mail._maintenance = _maintenance(mail_table)
    assert mail.delete_chunk_ids(["mail-1"]) == ("mail-1",)

    assert document._maintenance.mutations_since_optimize == 1
    assert mail._maintenance.mutations_since_optimize == 1
    assert document_table.where == ["file_path = 'C:/O''Brien/report.docx'"]
    assert mail_table.where == ["chunk_id IN ('mail-1')"]


@pytest.mark.skipif(not importlib.util.find_spec("lancedb"), reason="lancedb 미설치")
def test_real_lancedb_optimize_preserves_rows_and_reduces_small_fragments(tmp_path):
    import lancedb

    table = lancedb.connect(tmp_path).create_table("probe", data=[{"id": 0, "text": "seed"}])
    maintenance = LanceTableMaintenance(
        table,
        "probe",
        {
            "optimize_every_mutations": 20,
            "startup_optimize_when_small_fragments_reach": 100,
            "cycle_end_min_mutations": 0,
        },
    )
    for value in range(1, 21):
        table.add([{"id": value, "text": str(value)}])
        maintenance.record_mutation()

    before = table.stats()["fragment_stats"]["num_small_fragments"]
    assert maintenance.run_periodic()
    after = table.stats()["fragment_stats"]["num_small_fragments"]

    assert before == 21
    assert after < before
    assert table.count_rows() == 21
