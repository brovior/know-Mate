"""purge_meta 모듈 pytest 테스트 — 순수 함수라 PyQt6·LanceDB 없이 사외 전체 통과.

설계: docs/ai-workflow/architecture.md § A-0002
"""
from pathlib import Path

import pytest

from knowmate.collector import purge_meta
from knowmate.collector.purge_meta import PurgeMeta


# ============================================================
# normalize_folders / belongs_to_any
# ============================================================

class TestNormalizeFolders:
    def test_dedup_and_sort(self):
        out = purge_meta.normalize_folders(["/a/b", "/a/c", "/a/b/"])
        assert out == sorted(set(out))
        assert len(out) == 2

    def test_trailing_separator_removed(self):
        a = purge_meta.normalize_folders(["/watch/folder/"])
        b = purge_meta.normalize_folders(["/watch/folder"])
        assert a == b

    def test_backslash_normalized_to_forward_slash(self):
        out = purge_meta.normalize_folders(["C:\\watch\\folder"])
        assert "\\" not in out[0]


class TestBelongsToAny:
    def test_exact_match(self):
        roots = purge_meta.normalize_folders(["/watch"])
        assert purge_meta.belongs_to_any("/watch", roots)

    def test_child_path_matches(self):
        roots = purge_meta.normalize_folders(["/watch"])
        assert purge_meta.belongs_to_any("/watch/sub/file.docx", roots)

    def test_sibling_prefix_does_not_match(self):
        """C:/watch가 C:/watch-old/... 를 포함한다고 오판하지 않는다(경계 인식 비교)."""
        roots = purge_meta.normalize_folders(["/watch"])
        assert not purge_meta.belongs_to_any("/watch-old/file.docx", roots)

    def test_unrelated_path_does_not_match(self):
        roots = purge_meta.normalize_folders(["/watch"])
        assert not purge_meta.belongs_to_any("/other/file.docx", roots)

    def test_nested_watch_folders(self):
        roots = purge_meta.normalize_folders(["/a", "/a/b"])
        assert purge_meta.belongs_to_any("/a/b/c.docx", roots)
        assert purge_meta.belongs_to_any("/a/x.docx", roots)


# ============================================================
# compute_op_sig
# ============================================================

class TestComputeOpSig:
    def test_deterministic(self):
        s1 = purge_meta.compute_op_sig(["/a", "/b"], False, 0.3)
        s2 = purge_meta.compute_op_sig(["/a", "/b"], False, 0.3)
        assert s1 == s2

    def test_changes_with_folders(self):
        s1 = purge_meta.compute_op_sig(["/a"], False, 0.3)
        s2 = purge_meta.compute_op_sig(["/a", "/b"], False, 0.3)
        assert s1 != s2

    def test_changes_with_dry_run(self):
        s1 = purge_meta.compute_op_sig(["/a"], False, 0.3)
        s2 = purge_meta.compute_op_sig(["/a"], True, 0.3)
        assert s1 != s2

    def test_changes_with_max_delete_ratio(self):
        s1 = purge_meta.compute_op_sig(["/a"], False, 0.3)
        s2 = purge_meta.compute_op_sig(["/a"], False, 0.5)
        assert s1 != s2

    def test_stable_across_folder_order(self):
        """normalize_folders가 이미 정렬하므로 입력 순서와 무관하게 같은 서명."""
        f1 = purge_meta.normalize_folders(["/b", "/a"])
        f2 = purge_meta.normalize_folders(["/a", "/b"])
        assert purge_meta.compute_op_sig(f1, False, 0.3) == purge_meta.compute_op_sig(f2, False, 0.3)


# ============================================================
# decide — 판정 순서(차단 → 백오프 → 성공 스킵 → 실행)
# ============================================================

class TestDecide:
    def test_no_meta_runs(self):
        d = purge_meta.decide(PurgeMeta(), "sig-a", processed_count=0, now=1000.0)
        assert d.should_run and d.reason == "run"

    def test_success_skip_when_unchanged_and_recent(self):
        meta = PurgeMeta(reconciled_sig="sig-a", last_purge_ts=1000.0)
        d = purge_meta.decide(meta, "sig-a", processed_count=0, now=1010.0, force_reconcile_sec=86400)
        assert not d.should_run and d.reason == "success_skip"

    def test_runs_when_processed_nonzero_even_if_reconciled(self):
        meta = PurgeMeta(reconciled_sig="sig-a", last_purge_ts=1000.0)
        d = purge_meta.decide(meta, "sig-a", processed_count=5, now=1010.0, force_reconcile_sec=86400)
        assert d.should_run

    def test_runs_when_op_sig_changed(self):
        meta = PurgeMeta(reconciled_sig="sig-a", last_purge_ts=1000.0)
        d = purge_meta.decide(meta, "sig-b", processed_count=0, now=1010.0, force_reconcile_sec=86400)
        assert d.should_run

    def test_force_reconcile_after_period_elapsed(self):
        meta = PurgeMeta(reconciled_sig="sig-a", last_purge_ts=1000.0)
        d = purge_meta.decide(
            meta, "sig-a", processed_count=0, now=1000.0 + 86400 + 1, force_reconcile_sec=86400,
        )
        assert d.should_run and d.reason == "run"

    def test_backoff_suppresses_before_next_retry(self):
        """일시 실패 후 next_retry_ts 전에는 조회 없이 스킵된다."""
        meta = PurgeMeta(failed_sig="sig-a", next_retry_ts=2000.0)
        d = purge_meta.decide(meta, "sig-a", processed_count=0, now=1900.0, backoff_sec=1800)
        assert not d.should_run and d.reason == "backoff"

    def test_backoff_expires_and_retries(self):
        """백오프 만료 후 실제로 재시도된다(성공 스킵에 가로막히지 않음 — 리뷰4 B-2 회귀)."""
        meta = PurgeMeta(failed_sig="sig-a", next_retry_ts=2000.0)
        d = purge_meta.decide(meta, "sig-a", processed_count=0, now=2001.0, backoff_sec=1800)
        assert d.should_run

    def test_stale_success_meta_does_not_block_retry_after_backoff(self):
        """실패 시 reconciled_sig가 해제되므로, 이전 성공 메타가 남아 있어도 백오프
        만료 후 재시도가 24h까지 막히지 않는다(리뷰4 B-2가 지적한 결함의 회귀 테스트)."""
        # on_transient_failure를 거쳐 reconciled_sig가 None이 된 상태를 재현
        meta = PurgeMeta(reconciled_sig="sig-a", last_purge_ts=1000.0)
        after_failure = purge_meta.on_transient_failure(meta, "sig-a", now=1500.0, backoff_sec=1800)
        assert after_failure.reconciled_sig is None
        d = purge_meta.decide(after_failure, "sig-a", processed_count=0, now=1500.0 + 1800 + 1, backoff_sec=1800)
        assert d.should_run

    def test_blocked_suppresses_regardless_of_time(self):
        meta = PurgeMeta(blocked_sig="sig-a")
        d = purge_meta.decide(meta, "sig-a", processed_count=0, now=1e12)
        assert not d.should_run and d.reason == "blocked"

    def test_blocked_does_not_suppress_after_op_sig_changes(self):
        """구성·차단율 변경(op_sig 변경) 시에는 차단이 자동 해제된다."""
        meta = PurgeMeta(blocked_sig="sig-a")
        d = purge_meta.decide(meta, "sig-b", processed_count=0, now=1000.0)
        assert d.should_run

    def test_blocked_takes_priority_over_backoff(self):
        """판정 순서 — 차단이 백오프보다 먼저(같은 op_sig에 둘 다 설정된 비정상 상태 방어)."""
        meta = PurgeMeta(blocked_sig="sig-a", failed_sig="sig-a", next_retry_ts=1e12)
        d = purge_meta.decide(meta, "sig-a", processed_count=0, now=1.0)
        assert not d.should_run and d.reason == "blocked"

    def test_last_purge_ts_future_value_invalidates_success_skip(self):
        """last_purge_ts가 미래값이면(시각 역행 등) 성공 스킵이 무효화되어 실행된다."""
        meta = PurgeMeta(reconciled_sig="sig-a", last_purge_ts=5000.0)
        d = purge_meta.decide(meta, "sig-a", processed_count=0, now=1000.0, force_reconcile_sec=86400)
        assert d.should_run

    def test_next_retry_ts_far_future_is_treated_as_corrupted(self):
        """next_retry_ts가 설정 백오프 범위를 넘는 먼 미래값이면 손상으로 간주해
        백오프를 무시한다(next_retry_ts는 정의상 미래값이 정상이므로 값 자체가 아니라
        범위로 판정 — 리뷰4 B-1/리뷰6 회귀)."""
        meta = PurgeMeta(failed_sig="sig-a", next_retry_ts=1000.0 + 1800 * 100)
        d = purge_meta.decide(meta, "sig-a", processed_count=0, now=1000.0, backoff_sec=1800)
        assert d.should_run  # 손상 취급 → 억제 해제


# ============================================================
# on_success / on_transient_failure / on_blocked — 전이별 필드 완전성
# ============================================================

class TestMetaTransitions:
    def test_on_success_clears_all_suppressions(self):
        meta = PurgeMeta(failed_sig="x", next_retry_ts=1.0, blocked_sig="y")
        out = purge_meta.on_success(meta, "sig-a", now=1000.0)
        assert out.reconciled_sig == "sig-a"
        assert out.last_purge_ts == 1000.0
        assert out.failed_sig is None
        assert out.next_retry_ts is None
        assert out.blocked_sig is None

    def test_on_transient_failure_clears_reconciled_and_blocked(self):
        """리뷰8 m-1: 실패 시 reconciled_sig뿐 아니라 blocked_sig도 함께 해제해야
        오래된 차단 상태가 다시 유효해지지 않는다."""
        meta = PurgeMeta(reconciled_sig="old-success", blocked_sig="old-blocked")
        out = purge_meta.on_transient_failure(meta, "sig-a", now=1000.0, backoff_sec=1800)
        assert out.failed_sig == "sig-a"
        assert out.next_retry_ts == 2800.0
        assert out.reconciled_sig is None
        assert out.blocked_sig is None

    def test_on_blocked_clears_reconciled_and_failed(self):
        """리뷰8 m-1: 차단 시 reconciled_sig·failed_sig·next_retry_ts를 모두 해제해야
        오래된 백오프·성공 상태가 다시 유효해지지 않는다."""
        meta = PurgeMeta(reconciled_sig="old-success", failed_sig="old-failed", next_retry_ts=123.0)
        out = purge_meta.on_blocked(meta, "sig-a")
        assert out.blocked_sig == "sig-a"
        assert out.reconciled_sig is None
        assert out.failed_sig is None
        assert out.next_retry_ts is None
        assert out.blocked_reason == "mass_delete"

    def test_on_blocked_reason_unsupported(self):
        """리뷰12 m-1: reason="unsupported"를 넘기면 blocked_reason에 그대로 반영돼
        재시작 후 알림 문구를 대량삭제 차단과 구분할 수 있다."""
        meta = PurgeMeta()
        out = purge_meta.on_blocked(meta, "sig-a", reason="unsupported")
        assert out.blocked_sig == "sig-a"
        assert out.blocked_reason == "unsupported"


# ============================================================
# load_purge_meta / save_purge_meta — sidecar 원자 저장·보수적 검증
# ============================================================

class TestPurgeMetaPersistence:
    def test_missing_file_returns_default(self, tmp_path: Path):
        meta = purge_meta.load_purge_meta(tmp_path / "no_such_file.json")
        assert meta == PurgeMeta()

    def test_roundtrip(self, tmp_path: Path):
        f = tmp_path / "meta.json"
        original = PurgeMeta(reconciled_sig="sig-a", last_purge_ts=1000.0)
        assert purge_meta.save_purge_meta(f, original)
        loaded = purge_meta.load_purge_meta(f)
        assert loaded == original

    def test_roundtrip_blocked_reason(self, tmp_path: Path):
        """리뷰12 m-1: blocked_reason도 sidecar에 저장·복원돼야 재시작 후 알림 문구를
        대량삭제 차단과 API 비호환으로 구분할 수 있다."""
        f = tmp_path / "meta.json"
        original = PurgeMeta(blocked_sig="sig-a", blocked_reason="unsupported")
        assert purge_meta.save_purge_meta(f, original)
        loaded = purge_meta.load_purge_meta(f)
        assert loaded == original
        assert loaded.blocked_reason == "unsupported"

    def test_atomic_save_uses_tmp_then_replace(self, tmp_path: Path):
        f = tmp_path / "meta.json"
        purge_meta.save_purge_meta(f, PurgeMeta(reconciled_sig="sig-a"))
        assert f.exists()
        assert not f.with_suffix(".tmp").exists()

    def test_corrupted_json_treated_as_missing(self, tmp_path: Path):
        f = tmp_path / "meta.json"
        f.write_text("{not valid json", encoding="utf-8")
        meta = purge_meta.load_purge_meta(f)
        assert meta == PurgeMeta()

    def test_wrong_type_field_treated_as_missing(self, tmp_path: Path):
        """타입 이상(예: reconciled_sig가 문자열 아님)은 메타 전체를 부재로 취급한다
        (보수적 — 스킵 불가)."""
        import json
        f = tmp_path / "meta.json"
        f.write_text(json.dumps({"reconciled_sig": 123, "last_purge_ts": 1.0}), encoding="utf-8")
        meta = purge_meta.load_purge_meta(f)
        assert meta == PurgeMeta()

    def test_nan_timestamp_treated_as_missing(self, tmp_path: Path):
        f = tmp_path / "meta.json"
        f.write_text('{"last_purge_ts": NaN}', encoding="utf-8")
        meta = purge_meta.load_purge_meta(f)
        assert meta == PurgeMeta()

    def test_non_dict_json_treated_as_missing(self, tmp_path: Path):
        f = tmp_path / "meta.json"
        f.write_text("[1, 2, 3]", encoding="utf-8")
        meta = purge_meta.load_purge_meta(f)
        assert meta == PurgeMeta()


# ============================================================
# is_valid_ratio / is_valid_positive_seconds — fail-closed 검증 (설계 리뷰 10차 B-1)
# ============================================================

class TestIsValidRatio:
    """max_delete_ratio는 삭제 안전장치에 쓰이므로 fail-closed 검증 대상이다.
    사용자가 config.yaml을 직접 편집할 수 있어 NaN·Infinity·범위 밖 값이 들어올 수 있다."""

    def test_normal_values_valid(self):
        assert purge_meta.is_valid_ratio(0.0)
        assert purge_meta.is_valid_ratio(0.3)
        assert purge_meta.is_valid_ratio(1.0)
        assert purge_meta.is_valid_ratio(1)  # int도 허용

    def test_nan_invalid(self):
        assert not purge_meta.is_valid_ratio(float("nan"))

    def test_infinity_invalid(self):
        assert not purge_meta.is_valid_ratio(float("inf"))
        assert not purge_meta.is_valid_ratio(float("-inf"))

    def test_negative_invalid(self):
        assert not purge_meta.is_valid_ratio(-0.1)

    def test_greater_than_one_invalid(self):
        assert not purge_meta.is_valid_ratio(1.1)

    def test_non_numeric_invalid(self):
        assert not purge_meta.is_valid_ratio("0.3")
        assert not purge_meta.is_valid_ratio(None)

    def test_bool_invalid(self):
        """bool은 int의 서브클래스라 isinstance(True, int)가 참이지만, 설정값으로는
        의미 없는 타입이므로 명시적으로 배제한다."""
        assert not purge_meta.is_valid_ratio(True)
        assert not purge_meta.is_valid_ratio(False)


class TestIsValidPositiveSeconds:
    def test_normal_values_valid(self):
        assert purge_meta.is_valid_positive_seconds(1.0)
        assert purge_meta.is_valid_positive_seconds(86400)

    def test_zero_invalid(self):
        assert not purge_meta.is_valid_positive_seconds(0)

    def test_negative_invalid(self):
        assert not purge_meta.is_valid_positive_seconds(-1.0)

    def test_nan_invalid(self):
        assert not purge_meta.is_valid_positive_seconds(float("nan"))

    def test_infinity_invalid(self):
        assert not purge_meta.is_valid_positive_seconds(float("inf"))

    def test_non_numeric_invalid(self):
        assert not purge_meta.is_valid_positive_seconds("86400")


class TestComputeOpSigRejectsNonFinite:
    def test_nan_max_delete_ratio_raises(self):
        """compute_op_sig는 allow_nan=False로 직렬화한다 — 호출부가 is_valid_ratio로
        미리 걸러야 하며, 걸러지지 않은 NaN이 들어오면 조용히 넘어가지 않고 즉시 실패한다."""
        with pytest.raises(ValueError):
            purge_meta.compute_op_sig(["/a"], False, float("nan"))

    def test_infinity_max_delete_ratio_raises(self):
        with pytest.raises(ValueError):
            purge_meta.compute_op_sig(["/a"], False, float("inf"))
