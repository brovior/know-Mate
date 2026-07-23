"""secure 패키지 — TextExtractor 팩토리 + AutoReader."""
import logging
from pathlib import Path

from knowmate.secure.base import TextExtractor
from knowmate.secure.fake_reader import FakeReader
from knowmate.secure.plain_reader import PlainReader
from knowmate.secure.signature import is_zip

logger = logging.getLogger(__name__)

# 확장자는 OOXML이지만 실제 zip이 아닐 때(OLE2 오라벨·DRM 래핑 등) 매핑할
# COM 대상 (확장자 그대로 ComReader가 처리)
_OOXML_EXTS = {".docx", ".xlsx", ".pptx"}

__all__ = [
    "TextExtractor",
    "FakeReader",
    "PlainReader",
    "AutoReader",
    "get_extractor",
]


class AutoReader:
    """확장자 기반으로 PlainReader 또는 ComReader를 자동 선택하는 TextExtractor 구현체."""

    def __init__(self) -> None:
        """AutoReader를 초기화한다."""
        self._plain = PlainReader()

    def extract(self, path: str) -> str:
        """확장자에 따라 PlainReader 또는 ComReader로 파일을 파싱해 텍스트를 반환한다.

        확장자가 OOXML(.docx/.xlsx/.pptx)이라도 실제 내용이 zip이 아니면(OLE2
        오라벨, DRM 래핑 등) COM 리더로 폴백한다. Office(Excel/Word/PowerPoint)는
        사내 DRM 화이트리스트 프로세스라 COM으로 열면 투명 복호화된 내용을
        읽을 수 있다 — 탐색기·오피스에서는 정상 열리는데 우리 파서만 실패하던
        DRM 문서를 이 경로로 구제한다.

        .xls는 먼저 xlrd(순수 파이썬, Office 불필요)로 시도한다 — 대부분의
        정상 xls를 COM 경로 밖으로 빼서 행오버·좀비 프로세스·win32timezone
        문제를 원천 차단한다. xlrd가 실패하면(DRM 래핑·손상 등 소수) COM으로
        폴백해 기존과 동일하게 동작한다. .doc/.ppt는 xlrd 대응 라이브러리가
        없어 그대로 COM만 사용한다.

        COM 라우팅 직전, 해당 Office 앱을 사용자가 열어두었으면 OfficeBusyError를
        발생시켜 이번 사이클에서 건너뛴다(사용자 창 점유·응답없음 방지). 정상
        OOXML(.docx 등)은 라이브러리로 파싱하므로 이 가드의 영향을 받지 않는다.
        """
        ext = Path(path).suffix.lower()
        if ext == ".xls":
            try:
                return self._plain.extract(path)
            except Exception as exc:
                logger.warning(
                    "xlrd 파싱 실패(%s: %s) → COM 폴백: %s", type(exc).__name__, exc, path
                )
                self._guard_office_busy(ext, path)
                from knowmate.secure.com_reader import ComReader
                return ComReader().extract(path)
        if ext in {".doc", ".ppt"}:
            self._guard_office_busy(ext, path)
            # COM 의존 코드: secure/ 안에서만 import
            from knowmate.secure.com_reader import ComReader
            return ComReader().extract(path)
        if ext in _OOXML_EXTS and not is_zip(path):
            logger.warning("확장자는 OOXML이나 실제 zip 아님(OLE2/DRM 등 추정) → COM 경유: %s", path)
            self._guard_office_busy(ext, path)
            from knowmate.secure.com_reader import ComReader
            return ComReader().extract(path)
        return self._plain.extract(path)

    @staticmethod
    def _guard_office_busy(ext: str, path: str) -> None:
        """대상 Office 앱이 실행 중이면 OfficeBusyError를 발생시킨다."""
        from knowmate.secure.office_guard import OfficeBusyError, is_office_busy_for_ext, process_for_ext
        if is_office_busy_for_ext(ext):
            proc = process_for_ext(ext)
            raise OfficeBusyError(
                f"{proc} 실행 중 — {ext} COM 파싱을 이번 사이클에서 건너뜁니다: {path}"
            )


def get_extractor(mode: str) -> TextExtractor:
    """mode에 따라 적합한 TextExtractor 인스턴스를 반환한다."""
    if mode == "fake":
        return FakeReader()
    if mode == "plain":
        return PlainReader()
    if mode == "auto":
        return AutoReader()
    raise ValueError(f"알 수 없는 extractor 모드: {mode!r}")
