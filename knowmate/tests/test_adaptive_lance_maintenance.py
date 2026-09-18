"""Focused state-transition coverage for adaptive Lance maintenance."""
from __future__ import annotations

from pathlib import Path
import types

import pytest

from knowmate.collector.mail_scanner import MailScanCompletion
from knowmate.rag.lance_maintenance import LanceMaintenanceConfig, LanceTableMaintenance


class _Clock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class _Table:
    def __init__(self, small: int, *, no_effect: bool = False, fail: bool = False) -> None:
        self.small = small
        self.no_effect = no_effect
        self.fail = fail
        self.optimize_calls = 0

    def stats(self) -> dict:
        return {"fragment_stats": {"num_fragments": self.small, "num_small_fragments": self.small}}

    def optimize(self) -> None:
        self.optimize_calls += 1
        if self.fail:
            raise RuntimeError("optimize failed")
        if not self.no_effect:
            self.small = 1


def _maintenance(tmp_path: Path, table: _Table, mono: _Clock, wall: _Clock) -> LanceTableMaintenance:
    return LanceTableMaintenance(
        table, "emails", {}, db_path=tmp_path / "index", state_dir=tmp_path / "maintenance",
        now_fn=mono, wall_time_fn=wall,
    )


def test_3500_backlog_uses_three_hard_checks_and_one_final(tmp_path: Path) -> None:
    mono, wall, table = _Clock(), _Clock(1000), _Table(0)
    maintenance = _maintenance(tmp_path, table, mono, wall)
    assert maintenance.mark_backlog_active()
    for _ in range(3):
        table.small = 1000
        maintenance.record_mutation(1000)
        assert maintenance.checkpoint_hard_limit()
    table.small = 350
    assert maintenance.finish_backlog(completion="EXHAUSTED", checkpoint_succeeded=True)
    assert table.optimize_calls == 4


def test_remaining_unknown_cancel_and_checkpoint_failure_do_not_finalize(tmp_path: Path) -> None:
    mono, wall, table = _Clock(), _Clock(1000), _Table(350)
    maintenance = _maintenance(tmp_path, table, mono, wall)
    assert maintenance.mark_backlog_active()
    assert not maintenance.finish_backlog(completion="REMAINING", checkpoint_succeeded=True)
    assert not maintenance.finish_backlog(completion="UNKNOWN", checkpoint_succeeded=True)
    assert not maintenance.finish_backlog(completion="EXHAUSTED", checkpoint_succeeded=False)
    assert not maintenance.finish_backlog(completion="EXHAUSTED", checkpoint_succeeded=True, cancelled=lambda: True)
    assert table.optimize_calls == 0 and maintenance.backlog_active


def test_no_effect_is_suppressed_until_300_more_fragments(tmp_path: Path) -> None:
    mono, wall, table = _Clock(), _Clock(1000), _Table(350, no_effect=True)
    maintenance = _maintenance(tmp_path, table, mono, wall)
    assert maintenance.finish_backlog(completion="EXHAUSTED", checkpoint_succeeded=True)
    mono.value += 86400
    wall.value += 86400
    assert not maintenance.checkpoint_steady()
    table.small = 650
    assert maintenance.checkpoint_steady()
    assert table.optimize_calls == 2


def test_failure_cooldown_and_wall_clock_state_survive_restart(tmp_path: Path) -> None:
    mono, wall, table = _Clock(), _Clock(1000), _Table(1000, fail=True)
    maintenance = _maintenance(tmp_path, table, mono, wall)
    assert maintenance.mark_backlog_active()
    assert not maintenance.checkpoint_hard_limit()
    restarted = _maintenance(tmp_path, table, mono, wall)
    assert not restarted.checkpoint_hard_limit()
    mono.value += 300
    wall.value += 300
    table.fail = False
    assert restarted.checkpoint_hard_limit()


def test_recreation_resets_a_previous_steady_gate(tmp_path: Path) -> None:
    mono, wall, table = _Clock(), _Clock(1000), _Table(100)
    maintenance = _maintenance(tmp_path, table, mono, wall)
    assert maintenance.checkpoint_steady()
    reset = LanceTableMaintenance(
        table, "emails", {}, db_path=tmp_path / "index", state_dir=tmp_path / "maintenance",
        recreated=True, now_fn=mono, wall_time_fn=wall,
    )
    table.small = 100
    assert reset.checkpoint_steady()


def test_steady_interval_survives_restart(tmp_path: Path) -> None:
    """정상적인 미래 steady_not_before를 재시작 시 손상 시각으로 오인하지 않는다."""
    mono, wall, table = _Clock(), _Clock(1000), _Table(100)
    maintenance = _maintenance(tmp_path, table, mono, wall)
    assert maintenance.checkpoint_steady()
    table.small = 100

    restarted = _maintenance(tmp_path, table, mono, wall)

    assert not restarted.checkpoint_steady()
    mono.value += 86400
    wall.value += 86400
    assert restarted.checkpoint_steady()


def test_hard_limit_stats_are_sampled_not_read_for_every_write(tmp_path: Path) -> None:
    """실제 fragment 기준을 유지하되 기본 100 write 사이에는 stats I/O를 하지 않는다."""
    class CountingTable(_Table):
        def __init__(self) -> None:
            super().__init__(small=0)
            self.stats_calls = 0

        def stats(self) -> dict:
            self.stats_calls += 1
            return super().stats()

    mono, wall, table = _Clock(), _Clock(1000), CountingTable()
    maintenance = _maintenance(tmp_path, table, mono, wall)
    assert maintenance.mark_backlog_active()
    # marker 직후 첫 checkpoint는 기존 fragment 복구를 위해 한 번 확인한다.
    assert not maintenance.checkpoint_hard_limit()
    initial_calls = table.stats_calls
    for _ in range(99):
        maintenance.record_mutation()
        assert not maintenance.checkpoint_hard_limit()
    assert table.stats_calls == initial_calls
    maintenance.record_mutation()
    assert not maintenance.checkpoint_hard_limit()
    assert table.stats_calls == initial_calls + 1


def test_invalid_config_values_fall_back_to_safe_defaults() -> None:
    cfg = LanceMaintenanceConfig.from_mapping({
        "optimize_when_small_fragments_reach": True,
        "backlog_hard_limit_small_fragments": -1,
        "backlog_finalize_small_fragments_reach": "300",
        "min_optimize_interval_sec": float("nan"),
        "failure_cooldown_sec": 0,
    })

    assert cfg.optimize_when_small_fragments_reach == 100
    assert cfg.backlog_hard_limit_small_fragments == 1000
    assert cfg.backlog_finalize_small_fragments_reach == 300
    assert cfg.min_optimize_interval_sec == 86400
    assert cfg.failure_cooldown_sec == 300


def test_cancelled_checkpoint_never_starts_optimize(tmp_path: Path) -> None:
    mono, wall, table = _Clock(), _Clock(1000), _Table(1000)
    maintenance = _maintenance(tmp_path, table, mono, wall)
    assert maintenance.mark_backlog_active()
    maintenance.record_mutation(1000)

    assert not maintenance.checkpoint_hard_limit(cancelled=lambda: True)
    assert table.optimize_calls == 0


def test_optimize_success_is_not_retried_when_only_after_stats_fails(tmp_path: Path) -> None:
    """native optimize 성공 뒤 stats만 실패해도 전체 재작성을 실패로 오인하지 않는다."""
    class AfterStatsFailure(_Table):
        def __init__(self) -> None:
            super().__init__(small=100)
            self.stats_calls = 0

        def stats(self) -> dict:
            self.stats_calls += 1
            if self.stats_calls == 2:
                raise RuntimeError("after stats failed")
            return super().stats()

    mono, wall, table = _Clock(), _Clock(1000), AfterStatsFailure()
    maintenance = _maintenance(tmp_path, table, mono, wall)

    assert maintenance.checkpoint_steady()
    assert maintenance._state["last_result"] == "success_stats_unknown"
    assert table.optimize_calls == 1
    table.small = 100
    assert not maintenance.checkpoint_steady()
    assert table.optimize_calls == 1


def test_document_and_mail_delete_wrappers_still_record_mutations(tmp_path: Path) -> None:
    from knowmate.rag.email_indexer import EmailIndexer
    from knowmate.rag.indexer import Indexer

    class DeleteTable(_Table):
        def __init__(self) -> None:
            super().__init__(small=0)
            self.where: list[str] = []

        def delete(self, where: str) -> None:
            self.where.append(where)

    mono, wall = _Clock(), _Clock(1000)
    document_table = DeleteTable()
    document = Indexer.__new__(Indexer)
    document._table = document_table
    document._maintenance = _maintenance(tmp_path / "doc", document_table, mono, wall)
    document.delete_file_chunks("C:/O'Brien/report.docx")

    mail_table = DeleteTable()
    mail = EmailIndexer.__new__(EmailIndexer)
    mail.table = mail_table
    mail._maintenance = _maintenance(tmp_path / "mail", mail_table, mono, wall)
    assert mail.delete_chunk_ids(["mail-1"]) == ("mail-1",)

    assert document._maintenance.mutations_since_optimize == 1
    assert mail._maintenance.mutations_since_optimize == 1
    assert document_table.where == ["file_path = 'C:/O''Brien/report.docx'"]
    assert mail_table.where == ["chunk_id IN ('mail-1')"]


class _CompletionIndexer:
    table_was_recreated = False
    table_is_empty = False

    def __init__(self) -> None:
        self.finishes: list[tuple[str, bool]] = []
        self.marker_calls = 0

    def mark_maintenance_backlog(self) -> bool:
        self.marker_calls += 1
        return True

    def finish_maintenance_backlog(self, **kwargs) -> bool:
        self.finishes.append((kwargs["completion"], kwargs["checkpoint_succeeded"]))
        return False

    def get_index_state(self, _mail_uid, _mtime):
        return types.SimpleNamespace(
            state=types.SimpleNamespace(name="CURRENT"), old_chunk_ids=(),
        )


def _mail_item(index: int) -> dict:
    return {
        "path": f"C:/mail/{index}.mysingle",
        "path_key": f"c:/mail/{index}.mysingle",
        "mtime": float(index),
        "size": 1,
        "cache_entry": None,
    }


def _parsed_mail(path: str) -> dict:
    return {
        "mail_uid": f"knox:{path}", "source_file": path, "source_type": "knox",
        "message_id": "", "subject": "", "sender": "", "recipients": "",
        "mail_date": "", "thread_ref": "", "body_text": "", "source_meta": "{}",
    }


@pytest.mark.parametrize(
    ("limit", "cancelled", "save_ok", "expected"),
    [
        (1, False, True, MailScanCompletion.REMAINING),
        (2, False, True, MailScanCompletion.EXHAUSTED),
        (2, True, True, MailScanCompletion.UNKNOWN),
        (2, False, False, MailScanCompletion.UNKNOWN),
    ],
)
def test_mail_scan_reports_only_proven_completion(
    tmp_path: Path, monkeypatch, limit: int, cancelled: bool, save_ok: bool,
    expected: MailScanCompletion,
) -> None:
    """처리 제한·취소·상태 저장 실패를 EXHAUSTED로 잘못 보고하지 않는다."""
    from knowmate.collector import mail_scanner
    from knowmate.secure import mysingle_reader

    items = [_mail_item(1), _mail_item(2)]

    def collect(*_args, **_kwargs):
        scan_status = _args[-1]
        scan_status["complete"] = True
        return list(items), {item["path_key"] for item in items}, [], {}, 0

    monkeypatch.setattr(mail_scanner, "_collect_actionable_candidates", collect)
    monkeypatch.setattr(mysingle_reader, "parse_mail_file", _parsed_mail)
    if not save_ok:
        monkeypatch.setattr(mail_scanner, "save_mail_scan_state", lambda *_args, **_kwargs: False)
    indexer = _CompletionIndexer()

    mail_scanner.run_mail_scan(
        ["C:/mail"], indexer, {"mail": {"max_mails_per_scan": limit}},
        state_file=tmp_path / "state.json", failure_file=tmp_path / "failures.json",
        preloaded_state={
            "schema_version": 3, "cursor": None, "files": {}, "pending_deletes": [],
        },
        cancel_check=(lambda: cancelled),
    )

    assert indexer.last_mail_scan_completion is expected
    assert indexer.finishes == [(expected.value, save_ok if not cancelled else True)]
    assert indexer.marker_calls == 1
