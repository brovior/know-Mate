"""3~4차(실패 원인 분류·기록 + 유형별 백오프) — knowmate/collector/failure_state.py 단위 테스트.

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

    # -- 6a: failed_stage 사용 (이전엔 인자로 받고도 본문에서 무시됨) --

    def test_failed_stage_open_without_watchdog_is_open_error(self):
        kind, _ = failure_state.classify(RuntimeError("x"), failed_stage="open")
        assert kind == failure_state.KIND_OPEN_ERROR

    def test_failed_stage_dispatch_without_watchdog_is_open_error(self):
        kind, _ = failure_state.classify(RuntimeError("x"), failed_stage="dispatch")
        assert kind == failure_state.KIND_OPEN_ERROR

    def test_failed_stage_read_without_watchdog_is_read_error(self):
        kind, _ = failure_state.classify(RuntimeError("x"), failed_stage="read")
        assert kind == failure_state.KIND_READ_ERROR

    def test_failed_stage_cell_read_without_watchdog_is_read_error(self):
        kind, _ = failure_state.classify(RuntimeError("x"), failed_stage="cell_read")
        assert kind == failure_state.KIND_READ_ERROR

    def test_no_failed_stage_still_falls_back_to_unknown(self):
        """failed_stage가 아예 없으면(예: COM 아닌 다른 실패) 기존처럼 폴백 버킷으로 간다."""
        kind, _ = failure_state.classify(RuntimeError("x"))
        assert kind == failure_state.KIND_UNKNOWN_TRANSIENT

    def test_unmapped_failed_stage_falls_back_to_unknown(self):
        """정규화 테이블에 없는 단계 이름은 추측하지 않고 폴백한다."""
        kind, _ = failure_state.classify(RuntimeError("x"), failed_stage="close")
        assert kind == failure_state.KIND_UNKNOWN_TRANSIENT

    def test_watchdog_stage_takes_priority_over_failed_stage(self):
        """워치독이 발화했으면 failed_stage와 무관하게 타임아웃 분류가 우선한다."""
        kind, _ = failure_state.classify(
            RuntimeError("x"), watchdog_stage="open", failed_stage="read"
        )
        assert kind == failure_state.KIND_OPEN_TIMEOUT

    def test_office_busy_takes_priority_over_failed_stage(self):
        kind, _ = failure_state.classify(_OfficeBusyError("busy"), failed_stage="open")
        assert kind == failure_state.KIND_TEMPORARY_BUSY

    def test_unreadable_format_takes_priority_over_failed_stage(self):
        kind, _ = failure_state.classify(_UnreadableFormatError("bad"), failed_stage="open")
        assert kind == failure_state.KIND_NEEDS_USER_ACTION


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

    def test_kind_change_resets_to_one(self):
        """4차: 분류가 바뀌면 연속 횟수를 리셋한다 — 그렇지 않으면 사용자가 파일을
        하루 종일 열어둬 TEMPORARY_BUSY가 여러 번 쌓인 뒤 우연히 시간초과가 1번
        나면 consecutive가 커서 곧장 긴 백오프로 튄다."""
        records = {}
        for _ in range(10):
            failure_state.note_failure(
                records, "a.xlsx", failure_state.KIND_TEMPORARY_BUSY, None, None,
                mtime=100.0, size=10, now=1000.0,
            )
        assert records["a.xlsx"].consecutive_failures == 10
        failure_state.note_failure(
            records, "a.xlsx", failure_state.KIND_OPEN_TIMEOUT, "open", None,
            mtime=100.0, size=10, now=2000.0,
        )
        assert records["a.xlsx"].consecutive_failures == 1
        assert records["a.xlsx"].kind == failure_state.KIND_OPEN_TIMEOUT

    def test_same_kind_different_normalized_stage_resets_to_one(self):
        """6a(A-0003 §4): 연속성 키는 (kind, normalized_stage)다. dispatch와 open은 둘 다
        "open"으로 정규화되므로 이 둘 사이는 리셋되지 않지만(다음 테스트 참고), 서로 다른
        normalized_stage(open vs read)로 바뀌면 kind가 같아도 리셋돼야 한다."""
        records = {}
        for _ in range(3):
            failure_state.note_failure(
                records, "a.xlsx", failure_state.KIND_UNKNOWN_TRANSIENT, "open", None,
                mtime=100.0, size=10, now=1000.0,
            )
        assert records["a.xlsx"].consecutive_failures == 3
        failure_state.note_failure(
            records, "a.xlsx", failure_state.KIND_UNKNOWN_TRANSIENT, "read", None,
            mtime=100.0, size=10, now=2000.0,
        )
        assert records["a.xlsx"].consecutive_failures == 1

    def test_dispatch_and_open_stage_normalize_to_same_key(self):
        """dispatch → open은 같은 normalized_stage("open")이므로 리셋되지 않고 누적된다."""
        records = {}
        failure_state.note_failure(
            records, "a.xlsx", failure_state.KIND_OPEN_ERROR, "dispatch", None,
            mtime=100.0, size=10, now=1000.0,
        )
        failure_state.note_failure(
            records, "a.xlsx", failure_state.KIND_OPEN_ERROR, "open", None,
            mtime=100.0, size=10, now=1001.0,
        )
        assert records["a.xlsx"].consecutive_failures == 2

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
# request_retry_one (5차: 파일 단위 「지금 다시 시도」)
# ============================================================

class TestRequestRetryOne:
    def test_sets_force_retry_on_target_only(self):
        records = {}
        failure_state.note_failure(
            records, "a.xlsx", failure_state.KIND_UNKNOWN_TRANSIENT, None, None,
            mtime=100.0, size=10, now=1000.0,
        )
        failure_state.note_failure(
            records, "b.xlsx", failure_state.KIND_UNKNOWN_TRANSIENT, None, None,
            mtime=100.0, size=10, now=1000.0,
        )
        found = failure_state.request_retry_one(records, "a.xlsx")
        assert found is True
        assert records["a.xlsx"].force_retry is True
        assert records["b.xlsx"].force_retry is False

    def test_preserves_consecutive_failures(self):
        records = {}
        for i in range(5):
            failure_state.note_failure(
                records, "a.xlsx", failure_state.KIND_UNKNOWN_TRANSIENT, None, None,
                mtime=100.0, size=10, now=1000.0 + i,
            )
        failure_state.request_retry_one(records, "a.xlsx")
        assert records["a.xlsx"].consecutive_failures == 5

    def test_unknown_path_returns_false(self):
        records = {}
        found = failure_state.request_retry_one(records, "nope.xlsx")
        assert found is False
        assert records == {}

    def test_bypasses_backoff_in_should_defer(self):
        records = {}
        failure_state.note_failure(
            records, "a.xlsx", failure_state.KIND_NEEDS_USER_ACTION, "open", None,
            mtime=100.0, size=10, now=1000.0,
        )
        policy = failure_state.BackoffPolicy()
        # 백오프 창 안(같은 시각)이면 원래는 건너뛰어야 한다
        assert failure_state.should_defer(
            records["a.xlsx"], "a.xlsx", 100.0, 10, 1000.0, policy
        ) is True
        failure_state.request_retry_one(records, "a.xlsx")
        assert failure_state.should_defer(
            records["a.xlsx"], "a.xlsx", 100.0, 10, 1000.0, policy
        ) is False


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

    def test_source_changed_alias_normalizes_to_file_changed(self, tmp_path: Path):
        """A-0003 §6: SOURCE_CHANGED로 수동 편집된 sidecar도 폐기하지 않고
        FILE_CHANGED로 정규화한다(개명하지 않기로 확정했지만 하위호환은 유지)."""
        f = tmp_path / "index_failure.json"
        f.write_text(json.dumps({
            "schema_version": 1,
            "files": {
                "a.xlsx": {
                    "mtime": 100.0, "size": 10, "kind": "SOURCE_CHANGED", "stage": None,
                    "consecutive_failures": 1, "last_failed_ts": 1000.0,
                    "last_error_code": None, "strategy_version": failure_state.READ_STRATEGY_VERSION,
                    "force_retry": False,
                }
            },
        }), encoding="utf-8")
        loaded = failure_state.load_failures(f)
        assert loaded["a.xlsx"].kind == failure_state.KIND_FILE_CHANGED

    def test_new_6a_kinds_survive_roundtrip(self, tmp_path: Path):
        f = tmp_path / "index_failure.json"
        records = {}
        failure_state.note_failure(
            records, "a.xlsx", failure_state.KIND_OPEN_ERROR, "dispatch", None,
            mtime=100.0, size=10, now=1000.0,
        )
        failure_state.note_failure(
            records, "b.xlsx", failure_state.KIND_READ_ERROR, "read", None,
            mtime=100.0, size=10, now=1000.0,
        )
        failure_state.save_failures(f, records)
        loaded = failure_state.load_failures(f)
        assert loaded["a.xlsx"].kind == failure_state.KIND_OPEN_ERROR
        assert loaded["b.xlsx"].kind == failure_state.KIND_READ_ERROR

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


# ============================================================
# BackoffPolicy.from_config — fail-open 검증
# ============================================================

class TestBackoffPolicyFromConfig:
    def test_defaults_when_key_missing(self):
        policy = failure_state.BackoffPolicy.from_config({})
        assert policy.enabled is True
        assert policy.temporary_busy_sec == 300.0
        assert policy.timeout_ladder_sec == (1800.0, 21600.0)
        assert policy.needs_user_action_max_sec == 7 * 24 * 3600.0

    def test_defaults_when_section_wrong_type(self):
        policy = failure_state.BackoffPolicy.from_config({"failure_backoff": "not-a-dict"})
        assert policy == failure_state.BackoffPolicy()

    def test_custom_values_applied(self):
        policy = failure_state.BackoffPolicy.from_config({
            "failure_backoff": {
                "enabled": True, "temporary_busy_sec": 120.0,
                "timeout_ladder_sec": [60.0, 120.0], "file_changed_sec": 5.0,
            }
        })
        assert policy.temporary_busy_sec == 120.0
        assert policy.timeout_ladder_sec == (60.0, 120.0)
        assert policy.file_changed_sec == 5.0

    def test_enabled_false_respected(self):
        policy = failure_state.BackoffPolicy.from_config({"failure_backoff": {"enabled": False}})
        assert policy.enabled is False

    def test_invalid_enabled_type_falls_back_to_true(self):
        policy = failure_state.BackoffPolicy.from_config({"failure_backoff": {"enabled": "nope"}})
        assert policy.enabled is True

    def test_nan_seconds_falls_back_to_default(self):
        policy = failure_state.BackoffPolicy.from_config({
            "failure_backoff": {"temporary_busy_sec": float("nan")}
        })
        assert policy.temporary_busy_sec == 300.0

    def test_negative_seconds_falls_back_to_default(self):
        policy = failure_state.BackoffPolicy.from_config({
            "failure_backoff": {"temporary_busy_sec": -5.0}
        })
        assert policy.temporary_busy_sec == 300.0

    def test_non_numeric_seconds_falls_back_to_default(self):
        policy = failure_state.BackoffPolicy.from_config({
            "failure_backoff": {"file_changed_sec": "soon"}
        })
        assert policy.file_changed_sec == 0.0

    def test_empty_ladder_falls_back_to_default(self):
        policy = failure_state.BackoffPolicy.from_config({
            "failure_backoff": {"timeout_ladder_sec": []}
        })
        assert policy.timeout_ladder_sec == (1800.0, 21600.0)

    def test_ladder_with_invalid_item_falls_back_entirely(self):
        policy = failure_state.BackoffPolicy.from_config({
            "failure_backoff": {"timeout_ladder_sec": [60.0, "bad", 120.0]}
        })
        assert policy.timeout_ladder_sec == (1800.0, 21600.0)

    def test_ladder_not_a_list_falls_back(self):
        policy = failure_state.BackoffPolicy.from_config({
            "failure_backoff": {"timeout_ladder_sec": "1800,21600"}
        })
        assert policy.timeout_ladder_sec == (1800.0, 21600.0)

    def test_zero_seconds_is_valid_not_a_fallback(self):
        """file_changed_sec는 0이 정상값이다 — 0이라고 기본값으로 폴백하면 안 된다."""
        policy = failure_state.BackoffPolicy.from_config({
            "failure_backoff": {"file_changed_sec": 0.0}
        })
        assert policy.file_changed_sec == 0.0


# ============================================================
# backoff_seconds — 정책 매트릭스
# ============================================================

def _rec(kind, consecutive=1, last_failed_ts=1000.0, mtime=100.0, size=10, stage=None):
    return failure_state.FailureRecord(
        mtime=mtime, size=size, kind=kind, stage=stage,
        consecutive_failures=consecutive, last_failed_ts=last_failed_ts,
        last_error_code=None,
    )


class TestEscalationState:
    """6a: 단일 진실원(A-0003 §5 — 2차 리뷰 M-2). UI(bridge)와 백오프가 이 함수만
    호출해야 같은 레코드에 다른 판정이 나오는 사고를 막는다."""

    def test_below_repeat_threshold_is_normal(self):
        policy = failure_state.BackoffPolicy()
        rec = _rec(failure_state.KIND_OPEN_ERROR, consecutive=1)
        assert failure_state.escalation_state(rec, policy) == failure_state.ESCALATION_NORMAL

    def test_at_repeat_threshold_is_repeated(self):
        policy = failure_state.BackoffPolicy()
        rec = _rec(failure_state.KIND_OPEN_ERROR, consecutive=2)
        assert failure_state.escalation_state(rec, policy) == failure_state.ESCALATION_REPEATED

    def test_at_action_threshold_is_needs_action(self):
        policy = failure_state.BackoffPolicy()
        rec = _rec(failure_state.KIND_OPEN_ERROR, consecutive=3)
        assert failure_state.escalation_state(rec, policy) == failure_state.ESCALATION_NEEDS_ACTION

    def test_far_past_action_threshold_stays_needs_action(self):
        policy = failure_state.BackoffPolicy()
        rec = _rec(failure_state.KIND_OPEN_ERROR, consecutive=1000)
        assert failure_state.escalation_state(rec, policy) == failure_state.ESCALATION_NEEDS_ACTION

    def test_kind_independent(self):
        """승격은 kind와 무관하게 순수 카운트만 본다 — TEMPORARY_BUSY도 예외 없이 표시는 승격."""
        policy = failure_state.BackoffPolicy()
        for kind in (
            failure_state.KIND_TEMPORARY_BUSY, failure_state.KIND_OPEN_ERROR,
            failure_state.KIND_UNKNOWN_TRANSIENT, failure_state.KIND_NEEDS_USER_ACTION,
        ):
            rec = _rec(kind, consecutive=3)
            assert failure_state.escalation_state(rec, policy) == failure_state.ESCALATION_NEEDS_ACTION

    def test_custom_thresholds_from_config(self):
        policy = failure_state.BackoffPolicy.from_config({
            "failure_backoff": {"escalation_repeat_at": 5, "escalation_action_at": 10}
        })
        assert failure_state.escalation_state(_rec("X", consecutive=4), policy) == failure_state.ESCALATION_NORMAL
        assert failure_state.escalation_state(_rec("X", consecutive=5), policy) == failure_state.ESCALATION_REPEATED
        assert failure_state.escalation_state(_rec("X", consecutive=10), policy) == failure_state.ESCALATION_NEEDS_ACTION

    def test_invalid_pair_reversed_falls_back_to_default(self):
        """repeat_at >= action_at는 무효 — 기본값(2, 3)으로 폴백한다(2차 리뷰 M-2)."""
        policy = failure_state.BackoffPolicy.from_config({
            "failure_backoff": {"escalation_repeat_at": 5, "escalation_action_at": 3}
        })
        assert policy.escalation_repeat_at == 2
        assert policy.escalation_action_at == 3

    def test_invalid_pair_equal_falls_back_to_default(self):
        policy = failure_state.BackoffPolicy.from_config({
            "failure_backoff": {"escalation_repeat_at": 3, "escalation_action_at": 3}
        })
        assert policy.escalation_repeat_at == 2
        assert policy.escalation_action_at == 3

    def test_non_positive_or_non_int_falls_back_to_default(self):
        for bad in (0, -1, 1.5, True, "3"):
            policy = failure_state.BackoffPolicy.from_config({
                "failure_backoff": {"escalation_repeat_at": bad, "escalation_action_at": 3}
            })
            assert policy.escalation_repeat_at == 2
            assert policy.escalation_action_at == 3


class TestBackoffSeconds:
    def test_temporary_busy_within_jitter_range(self):
        policy = failure_state.BackoffPolicy()
        rec = _rec(failure_state.KIND_TEMPORARY_BUSY)
        wait = failure_state.backoff_seconds(rec, "/x/a.xlsx", policy)
        assert 300.0 <= wait <= 600.0

    def test_temporary_busy_deterministic_for_same_path(self):
        policy = failure_state.BackoffPolicy()
        rec = _rec(failure_state.KIND_TEMPORARY_BUSY)
        w1 = failure_state.backoff_seconds(rec, "/x/a.xlsx", policy)
        w2 = failure_state.backoff_seconds(rec, "/x/a.xlsx", policy)
        assert w1 == w2

    def test_temporary_busy_no_escalation_with_consecutive_failures(self):
        """같은 경로면 연속 횟수가 늘어도 대기 시간이 늘지 않는다(escalation 금지)."""
        policy = failure_state.BackoffPolicy()
        w_low = failure_state.backoff_seconds(
            _rec(failure_state.KIND_TEMPORARY_BUSY, consecutive=1), "/x/a.xlsx", policy,
        )
        w_high = failure_state.backoff_seconds(
            _rec(failure_state.KIND_TEMPORARY_BUSY, consecutive=50), "/x/a.xlsx", policy,
        )
        assert w_low == w_high

    def test_temporary_busy_stays_5_to_10min_even_past_escalation_action_at(self):
        """6a 3차 리뷰 M-2 회귀 방어 — 가장 중요한 케이스.

        사용자가 파일을 3번 연속 열어둔 상태로 실패하면 escalation_state는
        NEEDS_ACTION(3회)이 되어 화면엔 "조치 필요"가 뜨지만, 대기 시간은
        여전히 5~10분이어야 한다. 원인별 고정 정책 우선순위가 없으면 이
        경우 7일 사다리를 타 버려 "파일을 닫아도 최대 7일 대기"라는, 애초에
        막으려던 회귀보다 훨씬 나쁜 상황이 된다."""
        policy = failure_state.BackoffPolicy()
        rec = _rec(failure_state.KIND_TEMPORARY_BUSY, consecutive=5)
        assert failure_state.escalation_state(rec, policy) == failure_state.ESCALATION_NEEDS_ACTION
        wait = failure_state.backoff_seconds(rec, "/x/a.xlsx", policy)
        assert 300.0 <= wait <= 600.0

    def test_file_changed_stays_immediate_past_escalation_action_at(self):
        policy = failure_state.BackoffPolicy()
        rec = _rec(failure_state.KIND_FILE_CHANGED, consecutive=10)
        assert failure_state.backoff_seconds(rec, "/x.xlsx", policy) == 0.0

    def test_needs_user_action_unaffected_by_escalation(self):
        """이미 7일 고정값을 쓰므로 승격 임계와 상관없이 항상 동일해야 한다."""
        policy = failure_state.BackoffPolicy()
        w1 = failure_state.backoff_seconds(
            _rec(failure_state.KIND_NEEDS_USER_ACTION, consecutive=1), "/x.xlsx", policy,
        )
        w5 = failure_state.backoff_seconds(
            _rec(failure_state.KIND_NEEDS_USER_ACTION, consecutive=5), "/x.xlsx", policy,
        )
        assert w1 == w5 == 7 * 24 * 3600.0

    def test_open_timeout_ladder_first_failure(self):
        policy = failure_state.BackoffPolicy()
        rec = _rec(failure_state.KIND_OPEN_TIMEOUT, consecutive=1)
        assert failure_state.backoff_seconds(rec, "/x.xlsx", policy) == 1800.0

    def test_open_timeout_ladder_second_failure(self):
        policy = failure_state.BackoffPolicy()
        rec = _rec(failure_state.KIND_OPEN_TIMEOUT, consecutive=2)
        assert failure_state.backoff_seconds(rec, "/x.xlsx", policy) == 21600.0

    def test_open_timeout_ladder_third_and_beyond_escalates_to_7_days(self):
        """6a·사용자 결정(안 B): 3회째부터 escalation_action_at에 걸려 사다리가 아니라
        needs_user_action_max_sec(기본 7일)로 승격된다 — 옛 24시간 3번째 칸은 제거됐다."""
        policy = failure_state.BackoffPolicy()
        for consecutive in (3, 4, 10, 1000):
            rec = _rec(failure_state.KIND_OPEN_TIMEOUT, consecutive=consecutive)
            assert failure_state.backoff_seconds(rec, "/x.xlsx", policy) == 7 * 24 * 3600.0

    def test_open_error_kind_uses_same_ladder_and_escalation(self):
        """6a 신규 kind(OPEN_ERROR)도 OPEN_TIMEOUT과 동일한 사다리·승격을 탄다."""
        policy = failure_state.BackoffPolicy()
        assert failure_state.backoff_seconds(
            _rec(failure_state.KIND_OPEN_ERROR, consecutive=1), "/x.xlsx", policy,
        ) == 1800.0
        assert failure_state.backoff_seconds(
            _rec(failure_state.KIND_OPEN_ERROR, consecutive=3), "/x.xlsx", policy,
        ) == 7 * 24 * 3600.0

    def test_read_error_kind_uses_same_ladder(self):
        policy = failure_state.BackoffPolicy()
        assert failure_state.backoff_seconds(
            _rec(failure_state.KIND_READ_ERROR, consecutive=1), "/x.xlsx", policy,
        ) == 1800.0

    def test_read_timeout_uses_same_ladder(self):
        policy = failure_state.BackoffPolicy()
        rec = _rec(failure_state.KIND_READ_TIMEOUT, consecutive=1)
        assert failure_state.backoff_seconds(rec, "/x.xlsx", policy) == 1800.0

    def test_unknown_transient_ladder(self):
        policy = failure_state.BackoffPolicy()
        assert failure_state.backoff_seconds(
            _rec(failure_state.KIND_UNKNOWN_TRANSIENT, consecutive=1), "/x.xlsx", policy,
        ) == 1800.0
        assert failure_state.backoff_seconds(
            _rec(failure_state.KIND_UNKNOWN_TRANSIENT, consecutive=2), "/x.xlsx", policy,
        ) == 21600.0
        assert failure_state.backoff_seconds(
            _rec(failure_state.KIND_UNKNOWN_TRANSIENT, consecutive=3), "/x.xlsx", policy,
        ) == 7 * 24 * 3600.0

    def test_file_changed_is_zero(self):
        policy = failure_state.BackoffPolicy()
        rec = _rec(failure_state.KIND_FILE_CHANGED)
        assert failure_state.backoff_seconds(rec, "/x.xlsx", policy) == 0.0

    def test_needs_user_action_uses_max_sec(self):
        policy = failure_state.BackoffPolicy()
        rec = _rec(failure_state.KIND_NEEDS_USER_ACTION)
        assert failure_state.backoff_seconds(rec, "/x.xlsx", policy) == 7 * 24 * 3600.0

    def test_custom_ladder_from_policy(self):
        """승격 임계(기본 action_at=3) 전 회차에서는 커스텀 사다리 값이 그대로 쓰인다."""
        policy = failure_state.BackoffPolicy(timeout_ladder_sec=(60.0, 120.0))
        rec = _rec(failure_state.KIND_OPEN_TIMEOUT, consecutive=2)
        assert failure_state.backoff_seconds(rec, "/x.xlsx", policy) == 120.0

    def test_custom_ladder_still_escalates_past_action_at(self):
        """사다리를 아무리 늘려도(길이 5) escalation_action_at(기본 3)에서 승격이 우선한다."""
        policy = failure_state.BackoffPolicy(timeout_ladder_sec=(60.0, 120.0, 180.0, 240.0, 300.0))
        rec = _rec(failure_state.KIND_OPEN_TIMEOUT, consecutive=5)
        assert failure_state.backoff_seconds(rec, "/x.xlsx", policy) == 7 * 24 * 3600.0


# ============================================================
# should_defer — 완료 기준 검증
# ============================================================

class TestShouldDefer:
    def test_no_record_never_defers(self):
        policy = failure_state.BackoffPolicy()
        assert failure_state.should_defer(None, "/x.xlsx", 100.0, 10, 1000.0, policy) is False

    def test_disabled_policy_never_defers(self):
        policy = failure_state.BackoffPolicy(enabled=False)
        rec = _rec(failure_state.KIND_NEEDS_USER_ACTION, last_failed_ts=1000.0)
        assert failure_state.should_defer(rec, "/x.xlsx", 100.0, 10, 1000.1, policy) is False

    def test_file_changed_mtime_always_retries_immediately(self):
        """완료 기준: 파일이 변경되면 즉시 다시 시도 가능 — 분류·횟수 무관."""
        policy = failure_state.BackoffPolicy()
        rec = _rec(failure_state.KIND_NEEDS_USER_ACTION, mtime=100.0, size=10, last_failed_ts=1000.0)
        assert failure_state.should_defer(rec, "/x.xlsx", 999.0, 10, 1000.1, policy) is False

    def test_file_changed_size_always_retries_immediately(self):
        policy = failure_state.BackoffPolicy()
        rec = _rec(failure_state.KIND_OPEN_TIMEOUT, mtime=100.0, size=10, consecutive=3, last_failed_ts=1000.0)
        assert failure_state.should_defer(rec, "/x.xlsx", 100.0, 99, 1000.1, policy) is False

    def test_within_backoff_window_defers(self):
        policy = failure_state.BackoffPolicy()
        rec = _rec(failure_state.KIND_OPEN_TIMEOUT, mtime=100.0, size=10, consecutive=1, last_failed_ts=1000.0)
        # 1800초 백오프, 아직 100초밖에 안 지남
        assert failure_state.should_defer(rec, "/x.xlsx", 100.0, 10, 1100.0, policy) is True

    def test_after_backoff_window_retries(self):
        policy = failure_state.BackoffPolicy()
        rec = _rec(failure_state.KIND_OPEN_TIMEOUT, mtime=100.0, size=10, consecutive=1, last_failed_ts=1000.0)
        assert failure_state.should_defer(rec, "/x.xlsx", 100.0, 10, 1000.0 + 1800.1, policy) is False

    def test_needs_user_action_retries_after_7_days(self):
        """완료 기준: 영구적인 무시가 아니라 재시도 시점만 조정 — 7일 안전밸브."""
        policy = failure_state.BackoffPolicy()
        rec = _rec(failure_state.KIND_NEEDS_USER_ACTION, mtime=100.0, size=10, last_failed_ts=1000.0)
        just_before = 1000.0 + 7 * 24 * 3600.0 - 1.0
        just_after = 1000.0 + 7 * 24 * 3600.0 + 1.0
        assert failure_state.should_defer(rec, "/x.xlsx", 100.0, 10, just_before, policy) is True
        assert failure_state.should_defer(rec, "/x.xlsx", 100.0, 10, just_after, policy) is False

    def test_temporary_busy_retries_within_10_minutes(self):
        policy = failure_state.BackoffPolicy()
        rec = _rec(failure_state.KIND_TEMPORARY_BUSY, mtime=100.0, size=10, last_failed_ts=1000.0)
        assert failure_state.should_defer(rec, "/x.xlsx", 100.0, 10, 1000.0 + 601.0, policy) is False


class TestDescribeWait:
    def test_hours_and_minutes_format(self):
        policy = failure_state.BackoffPolicy()
        rec = _rec(failure_state.KIND_OPEN_TIMEOUT, consecutive=3, last_failed_ts=1000.0)
        desc = failure_state.describe_wait(rec, "/x.xlsx", 1000.0, policy)
        assert "시간" in desc and "분" in desc and "후 재시도" in desc

    def test_minutes_only_when_under_an_hour(self):
        policy = failure_state.BackoffPolicy()
        rec = _rec(failure_state.KIND_TEMPORARY_BUSY, last_failed_ts=1000.0)
        desc = failure_state.describe_wait(rec, "/x/a.xlsx", 1000.0, policy)
        assert "시간" not in desc
        assert "분 후 재시도" in desc

    def test_no_path_or_content_in_description(self):
        """로그용 — 경로·내용이 절대 안 섞인다(원칙7)."""
        policy = failure_state.BackoffPolicy()
        rec = _rec(failure_state.KIND_OPEN_TIMEOUT, last_failed_ts=1000.0)
        desc = failure_state.describe_wait(rec, "/secret/path/a.xlsx", 1000.0, policy)
        assert "secret" not in desc and "a.xlsx" not in desc


class TestRetryReason:
    """실패 기록이 있는 파일을 왜 지금 처리하는지 — 실기 검증에서 [지금 다시 시도]
    버튼을 눌러 재처리된 것과, 첫 실패 후 2시간 12분이 지나 백오프가 자연 만료된
    것을 구분할 수 없었다. 두 경로는 코드가 달라 반드시 구분돼야 한다.

    판정 순서는 should_defer와 정확히 일치해야 한다 — 갈라지면 로그가 거짓말을 한다.
    """

    def test_no_record_returns_none(self):
        assert failure_state.retry_reason(
            None, "/x.ppt", 100.0, 10, 2000.0, failure_state.BackoffPolicy(),
        ) is None

    def test_force_retry_is_user_request(self):
        rec = _rec(failure_state.KIND_OPEN_ERROR)
        rec = failure_state.FailureRecord(**{**rec.__dict__, "force_retry": True})
        assert failure_state.retry_reason(
            rec, "/x.ppt", 100.0, 10, 1001.0, failure_state.BackoffPolicy(),
        ) == "사용자 요청"

    def test_expired_backoff_is_wait_expiry(self):
        rec = _rec(failure_state.KIND_OPEN_ERROR)
        assert failure_state.retry_reason(
            rec, "/x.ppt", 100.0, 10, 1_000_000.0, failure_state.BackoffPolicy(),
        ) == "대기 만료"

    def test_changed_file_wins_over_force_retry(self):
        """파일 변경이 force_retry보다 먼저 판정돼야 한다(should_defer와 같은 순서)."""
        rec = _rec(failure_state.KIND_OPEN_ERROR)
        rec = failure_state.FailureRecord(**{**rec.__dict__, "force_retry": True})
        assert failure_state.retry_reason(
            rec, "/x.ppt", 999.0, 10, 1001.0, failure_state.BackoffPolicy(),
        ) == "파일 변경"

    def test_size_change_also_detected(self):
        rec = _rec(failure_state.KIND_OPEN_ERROR)
        assert failure_state.retry_reason(
            rec, "/x.ppt", 100.0, 99, 1001.0, failure_state.BackoffPolicy(),
        ) == "파일 변경"

    def test_disabled_policy_reported_as_such(self):
        rec = _rec(failure_state.KIND_OPEN_ERROR)
        assert failure_state.retry_reason(
            rec, "/x.ppt", 100.0, 10, 1001.0, failure_state.BackoffPolicy(enabled=False),
        ) == "정책 비활성"

    def test_reason_agrees_with_should_defer_on_every_branch(self):
        """should_defer가 False(처리)일 때 retry_reason은 항상 이유를 내놔야 하고,
        True(건너뜀)일 때만 '대기 만료'가 거짓이 된다 — 두 함수의 순서 일치 검증."""
        policy = failure_state.BackoffPolicy()
        base = _rec(failure_state.KIND_OPEN_ERROR)
        cases = [
            (base, 100.0, 10, 1_000_000.0),                                    # 만료
            (failure_state.FailureRecord(**{**base.__dict__, "force_retry": True}),
             100.0, 10, 1001.0),                                               # 사용자 요청
            (base, 555.0, 10, 1001.0),                                         # 파일 변경
        ]
        for rec, mtime, size, now in cases:
            assert not failure_state.should_defer(rec, "/x.ppt", mtime, size, now, policy)
            assert failure_state.retry_reason(rec, "/x.ppt", mtime, size, now, policy)
