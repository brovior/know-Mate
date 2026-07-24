"""lancedb 버전 호환성 진단(knowmate/lancedb_compat.py) 단위 테스트 — 사외 환경 전부 통과.

설계: 설계 리뷰 19차 M-2 — 배포는 PyInstaller onedir라 런타임 방어가 아니라
빌드 시점 실수를 조기에 드러내는 진단 신호. 그래서 검사는 앱을 막지 않고
경고 문구만 반환한다.
"""
from knowmate import lancedb_compat


class TestParseVersionTuple:
    def test_standard_version(self):
        assert lancedb_compat._parse_version_tuple("0.34.0") == (0, 34, 0)

    def test_two_component_version_padded_with_zero(self):
        assert lancedb_compat._parse_version_tuple("0.34") == (0, 34, 0)

    def test_pre_release_suffix_takes_leading_digits(self):
        assert lancedb_compat._parse_version_tuple("0.34.1rc1") == (0, 34, 1)

    def test_unparseable_returns_none(self):
        assert lancedb_compat._parse_version_tuple("unknown") is None

    def test_empty_string_returns_none(self):
        assert lancedb_compat._parse_version_tuple("") is None


class TestIsSupportedLancedbVersion:
    def test_min_boundary_supported(self):
        assert lancedb_compat.is_supported_lancedb_version("0.34.0") is True

    def test_within_range_supported(self):
        assert lancedb_compat.is_supported_lancedb_version("0.34.5") is True

    def test_below_min_unsupported(self):
        assert lancedb_compat.is_supported_lancedb_version("0.33.9") is False

    def test_max_boundary_exclusive_unsupported(self):
        """상한(0.35.0)은 배타적 — 도달하면 검증 범위 밖으로 취급한다."""
        assert lancedb_compat.is_supported_lancedb_version("0.35.0") is False

    def test_above_max_unsupported(self):
        assert lancedb_compat.is_supported_lancedb_version("0.40.0") is False

    def test_unparseable_version_treated_as_supported(self):
        """알 수 없는 버전 형식은 비호환 증거가 아니므로 오탐 방지를 위해 통과시킨다."""
        assert lancedb_compat.is_supported_lancedb_version("unknown") is True


class TestCheckLancedbVersion:
    def test_supported_version_returns_none(self, monkeypatch):
        import lancedb
        monkeypatch.setattr(lancedb, "__version__", "0.34.0", raising=False)
        assert lancedb_compat.check_lancedb_version() is None

    def test_unsupported_version_returns_warning_message(self, monkeypatch):
        import lancedb
        monkeypatch.setattr(lancedb, "__version__", "0.40.0", raising=False)
        warning = lancedb_compat.check_lancedb_version()
        assert warning is not None
        assert "0.40.0" in warning
