"""3차(실패 원인 분류 및 기록) — knowmate/collector/failure_state.py 단위 테스트.

사외 Linux에서도 전부 통과해야 한다(순수 파이썬, win32/COM import 없음).
"""
import json
from pathlib import Path

import pytest

from knowmate.collector import failure_state


class _FakeComError(Exception):
    """pywintypes.com_error 흉내 — hresult 속성 형태."""

    def __init__(self, hresult):
        super().__init__(hresult, "message", None, None)
        self.hresult = hresult


class _FakeComErrorArgsOnly(Exception):
    """pywintypes.com_error 흉내 — args[0] 형태(hresult 속성 없음)."""

    def __init__(self, hresult):
        super().__init__(hresult, "message")


class _OfficeBusyError(Exception):
    """실제 office_guard.OfficeBusyError를 import하지 않고 이름만 흉내(순수 파이썬 유지)."""


_OfficeBusyError.__name__ = "OfficeBusyError"


class _UnreadableFormatError(Exception):
    pass


_UnreadableFormatError.__name__ = "UnreadableFormatError"


# ============================================================
# classify
# ============================================================

class TestClassify:
    def test_watchdog_open_stage_is_open_timeout(self):
        kind, code = failure_state.classify(Exception("x"), watchdog_stage="open")
        assert kind == failure_state.KIND_OPEN_TIMEOUT

    def test_watchdog_dispatch_stage_is_open_timeout(self):
        kind, _ = failure_state.classify(Exception("x"), watchdog_stage="dispatch")
        assert kind == failure_state.KIND_OPEN_TIMEOUT

    def test_watchdog_cell_read_stage_is_read_timeout(self):
        kind, _ = failure_state.classify(Exception("x"), watchdog_stage="cell_read")
        assert kind == failure_state.KIND_READ_TIMEOUT

    def test_watchdog_sheets_stage_is_read_timeout(self):
        kind, _ = failure_state.classify(Exception("x"), watchdog_stage="sheets")
        assert kind == failure_state.KIND_READ_TIMEOUT

    def test_watchdog_read_stage_is_read_timeout(self):
        kind, _ = failure_state.classify(Exception("x"), watchdog_stage="read")
        assert kind == failure_state.KIND_READ_TIMEOUT

    def test_office_busy_error_by_name(self):
        kind, _ = failure_state.classify(_OfficeBusyError("busy"))
        assert kind == failure_state.KIND_TEMPORARY_BUSY

    def test_com_busy_hresult_rpc_e_call_rejected(self):
        exc = _FakeComError(-2147418111)  # RPC_E_CALL_REJECTED
        kind, code = failure_state.classify(exc)
        assert kind == failure_state.KIND_TEMPORARY_BUSY
        assert code == "0x80010001"

    def test_com_busy_hresult_servercall_retrylater(self):
        exc = _FakeComError(-2147417846)  # RPC_E_SERVERCALL_RETRYLATER
        kind, _ = failure_state.classify(exc)
        assert kind == failure_state.KIND_TEMPORARY_BUSY

    def test_unreadable_format_error_by_name(self):
        kind, _ = failure_state.classify(_UnreadableFormatError("bad"))
        assert kind == failure_state.KIND_NEEDS_USER_ACTION

    def test_unknown_hresult_falls_back_to_unknown_transient(self):
        exc = _FakeComError(-2147024809)  # E_INVALIDARG — 매핑 테이블에 없는 임의 코드
        kind, code = failure_state.classify(exc)
        assert kind == failure_state.KIND_UNKNOWN_TRANSIENT
        assert code == "0x80070057"

    def test_plain_exception_no_hresult_falls_back(self):
        kind, code = failure_state.classify(RuntimeError("plain"))
        assert kind == failure_state.KIND_UNKNOWN_TRANSIENT
        assert code is None

    def test_hresult_from_args_only_form(self):
        exc = _FakeComErrorArgsOnly(-2147418111)
        kind, code = failure_state.classify(exc)
        assert kind == failure_state.KIND_TEMPORARY_BUSY
        assert code == "0x80010001"

    def test_watchdog_stage_takes_priority_over_busy_name(self):
        # 워치독이 실제로 발화했다면(타임아웃) OfficeBusyError보다 타임아웃 분류가 우선한다
        kind, _ = failure_state.classify(_OfficeBusyError("busy"), watchdog_stage="open")
        assert kind == failure_state.KIND_OPEN_TIMEOUT


# ============================================================
# note_failure / note_success — 연속 실패 누적·리셋
# ============================================================

class TestNoteFailure:
    def test_first_failure_is_consecutive_one(self):
        records = {}
        failure_state.note_failure(
            records, "a.xlsx", failure_state.KIND_UNKNOWN_TRANSIENT, None, None,
            mtime=100.0, size=10, now=1000.0,
        )
        assert records["a.xlsx"].consecutive_failures == 1

    def test_same_mtime_size_accumulates(self):
        records = {}
        for i in range(3):
            failure_state.note_failure(
                records, "a.xlsx", failure_state.KIND_UNKNOWN_TRANSIENT, None, None,
                mtime=100.0, size=10, now=1000.0 + i,
            )
        assert records["a.xlsx"].consecutive_failures == 3
        assert records["a.xlsx"].last_failed_ts == 1002.0

    def test_size_change_resets_to_one(self):
        records = {}
        failure_state.note_failure(
            records, "a.xlsx", failure_state.KIND_UNKNOWN_TRANSIENT, None, None,
            mtime=100.0, size=10, now=1000.0,
        )
        failure_state.note_failure(
            records, "a.xlsx", failure_state.KIND_UNKNOWN_TRANSIENT, None, None,
            mtime=100.0, size=999, now=1001.0,
        )
        assert records["a.xlsx"].consecutive_failures == 1

    def test_mtime_change_resets_to_one(self):
        records = {}
        failure_state.note_failure(
            records, "a.xlsx", failure_state.KIND_UNKNOWN_TRANSIENT, None, None,
            mtime=100.0, size=10, now=1000.0,
        )
        failure_state.note_failure(
            records, "a.xlsx", failure_state.KIND_UNKNOWN_TRANSIENT, None, None,
            mtime=200.0, size=10, now=1001.0,
        )
        assert records["a.xlsx"].consecutive_failures == 1

    def test_note_success_removes_record(self):
        records = {}
        failure_state.note_failure(
            records, "a.xlsx", failure_state.KIND_UNKNOWN_TRANSIENT, None, None,
            mtime=100.0, size=10, now=1000.0,
        )
        failure_state.note_success(records, "a.xlsx")
        assert "a.xlsx" not in records

    def test_note_success_on_unknown_path_is_noop(self):
        records = {}
        failure_state.note_success(records, "nope.xlsx")  # should not raise
        assert records == {}

    def test_stage_and_error_code_recorded(self):
        records = {}
        failure_state.note_failure(
            records, "a.xlsx", failure_state.KIND_OPEN_TIMEOUT, "open", "0x80010001",
            mtime=100.0, size=10, now=1000.0,
        )
        rec = records["a.xlsx"]
        assert rec.stage == "open"
        assert rec.last_error_code == "0x80010001"

    def test_no_exception_message_ever_stored(self):
        """CLAUDE.md 원칙7 회귀 방어 — 기록에 예외 메시지 내용이 절대 섞이지 않는다."""
        records = {}
        secret_message = "SENSITIVE_CELL_CONTENT_1234"
        exc = RuntimeError(secret_message)
        kind, code = failure_state.classify(exc)
        failure_state.note_failure(
            records, "a.xlsx", kind, None, code, mtime=100.0, size=10, now=1000.0,
        )
        serialized = json.dumps({p: r.__dict__ for p, r in records.items()})
        assert secret_message not in serialized


# ============================================================
# prune
# ============================================================

class TestPrune:
    def test_removes_only_nonexistent_paths(self):
        records = {}
        failure_state.note_failure(
            records, "exists.xlsx", failure_state.KIND_UNKNOWN_TRANSIENT, None, None,
            mtime=100.0, size=10, now=1000.0,
        )
        failure_state.note_failure(
            records, "gone.xlsx", failure_state.KIND_UNKNOWN_TRANSIENT, None, None,
            mtime=100.0, size=10, now=1000.0,
        )
        removed = failure_state.prune(records, exists_fn=lambda p: p == "exists.xlsx")
        assert removed == 1
        assert "exists.xlsx" in records
        assert "gone.xlsx" not in records

    def test_no_removal_when_all_exist(self):
        records = {}
        failure_state.note_failure(
            records, "a.xlsx", failure_state.KIND_UNKNOWN_TRANSIENT, None, None,
            mtime=100.0, size=10, now=1000.0,
        )
        removed = failure_state.prune(records, exists_fn=lambda p: True)
        assert removed == 0
        assert "a.xlsx" in records


# ============================================================
# load_failures / save_failures — 로드 견고성 + 원자적 저장 + 전략 버전 초기화
# ============================================================

class TestLoadSaveFailures:
    def test_load_missing_file_returns_empty(self, tmp_path: Path):
        assert failure_state.load_failures(tmp_path / "nope.json") == {}

    def test_load_corrupted_json_returns_empty(self, tmp_path: Path):
        f = tmp_path / "index_failure.json"
        f.write_text("NOT_JSON", encoding="utf-8")
        assert failure_state.load_failures(f) == {}

    def test_load_wrong_top_level_type_returns_empty(self, tmp_path: Path):
        f = tmp_path / "index_failure.json"
        f.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        assert failure_state.load_failures(f) == {}

    def test_load_missing_files_key_returns_empty(self, tmp_path: Path):
        f = tmp_path / "index_failure.json"
        f.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
        assert failure_state.load_failures(f) == {}

    def test_save_load_roundtrip(self, tmp_path: Path):
        f = tmp_path / "index_failure.json"
        records = {}
        failure_state.note_failure(
            records, "a.xlsx", failure_state.KIND_OPEN_TIMEOUT, "open", "0x80010001",
            mtime=100.0, size=10, now=1000.0,
        )
        assert failure_state.save_failures(f, records) is True
        loaded = failure_state.load_failures(f)
        assert loaded["a.xlsx"].kind == failure_state.KIND_OPEN_TIMEOUT
        assert loaded["a.xlsx"].stage == "open"
        assert loaded["a.xlsx"].consecutive_failures == 1

    def test_save_leaves_no_tmp_file(self, tmp_path: Path):
        f = tmp_path / "index_failure.json"
        failure_state.save_failures(f, {})
        assert not f.with_suffix(".tmp").exists()
        assert f.exists()

    def test_load_discards_record_with_stale_strategy_version(self, tmp_path: Path):
        f = tmp_path / "index_failure.json"
        payload = {
            "schema_version": 1,
            "files": {
                "old.xlsx": {
                    "mtime": 100.0, "size": 10, "kind": "UNKNOWN_TRANSIENT",
                    "stage": None, "consecutive_failures": 5, "last_failed_ts": 1000.0,
                    "last_error_code": None,
                    "strategy_version": failure_state.READ_STRATEGY_VERSION - 1,
                },
            },
        }
        f.write_text(json.dumps(payload), encoding="utf-8")
        loaded = failure_state.load_failures(f)
        assert "old.xlsx" not in loaded

    def test_load_discards_record_with_unknown_kind(self, tmp_path: Path):
        f = tmp_path / "index_failure.json"
        payload = {
            "schema_version": 1,
            "files": {
                "bad.xlsx": {
                    "mtime": 100.0, "size": 10, "kind": "NOT_A_REAL_KIND",
                    "stage": None, "consecutive_failures": 1, "last_failed_ts": 1000.0,
                    "last_error_code": None,
                    "strategy_version": failure_state.READ_STRATEGY_VERSION,
                },
            },
        }
        f.write_text(json.dumps(payload), encoding="utf-8")
        assert failure_state.load_failures(f) == {}

    def test_load_discards_record_with_bad_field_type(self, tmp_path: Path):
        f = tmp_path / "index_failure.json"
        payload = {
            "schema_version": 1,
            "files": {
                "bad.xlsx": {
                    "mtime": "not-a-number", "size": 10, "kind": "UNKNOWN_TRANSIENT",
                    "stage": None, "consecutive_failures": 1, "last_failed_ts": 1000.0,
                    "last_error_code": None,
                    "strategy_version": failure_state.READ_STRATEGY_VERSION,
                },
            },
        }
        f.write_text(json.dumps(payload), encoding="utf-8")
        assert failure_state.load_failures(f) == {}

    def test_good_and_bad_records_mixed_only_bad_discarded(self, tmp_path: Path):
        f = tmp_path / "index_failure.json"
        payload = {
            "schema_version": 1,
            "files": {
                "good.xlsx": {
                    "mtime": 100.0, "size": 10, "kind": "UNKNOWN_TRANSIENT",
                    "stage": None, "consecutive_failures": 1, "last_failed_ts": 1000.0,
                    "last_error_code": None,
                    "strategy_version": failure_state.READ_STRATEGY_VERSION,
                },
                "bad.xlsx": {"kind": "NOT_A_REAL_KIND"},
            },
        }
        f.write_text(json.dumps(payload), encoding="utf-8")
        loaded = failure_state.load_failures(f)
        assert "good.xlsx" in loaded
        assert "bad.xlsx" not in loaded


# ============================================================
# summarize
# ============================================================

class TestSummarize:
    def test_empty_records(self):
        assert failure_state.summarize({}) == {}

    def test_counts_by_kind(self):
        records = {}
        failure_state.note_failure(
            records, "a.xlsx", failure_state.KIND_OPEN_TIMEOUT, "open", None,
            mtime=1.0, size=1, now=1.0,
        )
        failure_state.note_failure(
            records, "b.xlsx", failure_state.KIND_OPEN_TIMEOUT, "open", None,
            mtime=1.0, size=1, now=1.0,
        )
        failure_state.note_failure(
            records, "c.doc", failure_state.KIND_NEEDS_USER_ACTION, None, None,
            mtime=1.0, size=1, now=1.0,
        )
        summary = failure_state.summarize(records)
        assert summary == {
            failure_state.KIND_OPEN_TIMEOUT: 2,
            failure_state.KIND_NEEDS_USER_ACTION: 1,
        }
