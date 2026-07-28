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
        assert policy.timeout_ladder_sec == (1800.0, 21600.0, 86400.0)
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
        assert policy.timeout_ladder_sec == (1800.0, 21600.0, 86400.0)

    def test_ladder_with_invalid_item_falls_back_entirely(self):
        policy = failure_state.BackoffPolicy.from_config({
            "failure_backoff": {"timeout_ladder_sec": [60.0, "bad", 120.0]}
        })
        assert policy.timeout_ladder_sec == (1800.0, 21600.0, 86400.0)

    def test_ladder_not_a_list_falls_back(self):
        policy = failure_state.BackoffPolicy.from_config({
            "failure_backoff": {"timeout_ladder_sec": "1800,21600"}
        })
        assert policy.timeout_ladder_sec == (1800.0, 21600.0, 86400.0)

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

    def test_open_timeout_ladder_first_failure(self):
        policy = failure_state.BackoffPolicy()
        rec = _rec(failure_state.KIND_OPEN_TIMEOUT, consecutive=1)
        assert failure_state.backoff_seconds(rec, "/x.xlsx", policy) == 1800.0

    def test_open_timeout_ladder_second_failure(self):
        policy = failure_state.BackoffPolicy()
        rec = _rec(failure_state.KIND_OPEN_TIMEOUT, consecutive=2)
        assert failure_state.backoff_seconds(rec, "/x.xlsx", policy) == 21600.0

    def test_open_timeout_ladder_third_and_beyond(self):
        policy = failure_state.BackoffPolicy()
        for consecutive in (3, 4, 10, 1000):
            rec = _rec(failure_state.KIND_OPEN_TIMEOUT, consecutive=consecutive)
            assert failure_state.backoff_seconds(rec, "/x.xlsx", policy) == 86400.0

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
        ) == 86400.0

    def test_file_changed_is_zero(self):
        policy = failure_state.BackoffPolicy()
        rec = _rec(failure_state.KIND_FILE_CHANGED)
        assert failure_state.backoff_seconds(rec, "/x.xlsx", policy) == 0.0

    def test_needs_user_action_uses_max_sec(self):
        policy = failure_state.BackoffPolicy()
        rec = _rec(failure_state.KIND_NEEDS_USER_ACTION)
        assert failure_state.backoff_seconds(rec, "/x.xlsx", policy) == 7 * 24 * 3600.0

    def test_custom_ladder_from_policy(self):
        policy = failure_state.BackoffPolicy(timeout_ladder_sec=(60.0, 120.0))
        rec = _rec(failure_state.KIND_OPEN_TIMEOUT, consecutive=5)
        assert failure_state.backoff_seconds(rec, "/x.xlsx", policy) == 120.0


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
