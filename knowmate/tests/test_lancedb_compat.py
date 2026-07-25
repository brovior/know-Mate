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
    def test_exact_supported_version(self):
        assert lancedb_compat.is_supported_lancedb_version("0.34.0") is True

    def test_same_minor_higher_patch_unsupported(self):
        """리뷰20 M-1: 0.34.x 범위가 아니라 **정확히 한 버전**만 허용한다 — projection
        계약을 실측한 건 0.34.0뿐이라, 범위를 허용하면 검증 안 된 패치가 빌드에
        섞여도 이 검사를 통과해 버려 고정의 목적이 무너진다."""
        assert lancedb_compat.is_supported_lancedb_version("0.34.5") is False
        assert lancedb_compat.is_supported_lancedb_version("0.34.1") is False

    def test_lower_version_unsupported(self):
        assert lancedb_compat.is_supported_lancedb_version("0.33.9") is False

    def test_next_minor_unsupported(self):
        assert lancedb_compat.is_supported_lancedb_version("0.35.0") is False

    def test_much_higher_unsupported(self):
        assert lancedb_compat.is_supported_lancedb_version("0.40.0") is False

    def test_two_component_version_matches_when_patch_is_zero(self):
        """"0.34"는 (0,34,0)으로 정규화되므로 검증 버전과 일치한다."""
        assert lancedb_compat.is_supported_lancedb_version("0.34") is True

    def test_unparseable_version_treated_as_supported(self):
        """알 수 없는 버전 형식은 비호환 증거가 아니므로 오탐 방지를 위해 통과시킨다."""
        assert lancedb_compat.is_supported_lancedb_version("unknown") is True

    def test_supported_version_matches_requirements_pin(self):
        """SUPPORTED_VERSION과 requirements.txt의 `lancedb==` 값이 어긋나면, 시작 시
        검사가 정상 빌드를 경고하거나 잘못된 빌드를 통과시킨다 — 둘을 함께 고정한다."""
        from pathlib import Path
        req = Path(__file__).resolve().parents[2] / "requirements.txt"
        pinned = None
        for line in req.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("lancedb=="):
                pinned = stripped.split("==", 1)[1].split()[0].split("#")[0].strip()
                break
        assert pinned is not None, "requirements.txt에 `lancedb==` 고정이 없다"
        assert lancedb_compat._parse_version_tuple(pinned) == lancedb_compat.SUPPORTED_VERSION


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
