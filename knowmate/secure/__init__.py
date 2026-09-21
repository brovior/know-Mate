"""secure 패키지 — TextExtractor 팩토리 + AutoReader."""
import logging
from pathlib import Path
from typing import Callable

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

    def __init__(self, xlsx_block_rows: int | None = None) -> None:
        """AutoReader를 초기화한다.

        xlsx_block_rows: COM 경로에서 Excel 범위를 한 번에 읽을 행 수
            (config `chunking.xlsx_block_rows`). `secure/`는 전역 `get_config()`를
            직접 조회하지 않고 호출부가 값을 주입하는 관례를 따른다
            (`get_crypto_manager(cfg)`·`get_extractor(mode)`와 동일). None이면
            `ComReader`/`ExcelComReader`가 자체 기본값으로 폴백한다 — 여기서
            기본값을 해석하지 않아야 `com_reader`(COM 의존)를 모듈 로드 시점에
            import하지 않는 지연 import 격리가 유지된다.
        """
        self._plain = PlainReader()
        self._xlsx_block_rows = xlsx_block_rows
        self._on_com_begin: Callable[[str], None] | None = None
        self._on_com_end: Callable[[], None] | None = None
        self._last_com_used = False

    def set_com_operation_hooks(
        self,
        on_begin: Callable[[str], None] | None,
        on_end: Callable[[], None] | None,
    ) -> None:
        """동적 COM 폴백 직전/직후 호출할 scheduler 훅을 설정한다."""
        self._on_com_begin = on_begin
        self._on_com_end = on_end

    def take_actual_com_used(self) -> bool:
        """직전 extract가 실제 COM 경로에 진입했는지 반환하고 상태를 비운다."""
        used = self._last_com_used
        self._last_com_used = False
        return used

    def _extract_with_com(self, path: str, ext: str) -> str:
        """실제 COM 진입 구간만 scheduler에 알리고 추출한다."""
        self._guard_office_busy(ext, path)
        from knowmate.secure.office_guard import process_for_ext
        exe = process_for_ext(ext)
        try:
            # on_begin이 begin_com_op 뒤 watchdog.arm에서 실패할 수 있어도, 이미
            # 게시된 COM 작업 컨텍스트는 반드시 on_end로 해제해야 한다.
            if self._on_com_begin is not None and exe is not None:
                self._on_com_begin(exe)
            self._last_com_used = True
            from knowmate.secure.com_reader import ComReader
            return ComReader(xlsx_block_rows=self._xlsx_block_rows).extract(path)
        finally:
            if self._on_com_end is not None and exe is not None:
                self._on_com_end()

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
        self._last_com_used = False
        if ext == ".xls":
            try:
                return self._plain.extract(path)
            except Exception as exc:
                logger.warning(
                    "xlrd 파싱 실패(%s: %s) → COM 폴백: %s", type(exc).__name__, exc, path
                )
                return self._extract_with_com(path, ext)
        if ext in {".doc", ".ppt"}:
            return self._extract_with_com(path, ext)
        if ext in _OOXML_EXTS and not is_zip(path):
            logger.warning("확장자는 OOXML이나 실제 zip 아님(OLE2/DRM 등 추정) → COM 경유: %s", path)
            return self._extract_with_com(path, ext)
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


def get_extractor(mode: str, xlsx_block_rows: int | None = None) -> TextExtractor:
    """mode에 따라 적합한 TextExtractor 인스턴스를 반환한다.

    xlsx_block_rows: COM 경로에서 Excel 범위를 한 번에 읽을 행 수
        (config `chunking.xlsx_block_rows`). auto 모드에서만 의미가 있고,
        fake/plain 모드는 COM을 타지 않아 무시된다.
    """
    if mode == "fake":
        return FakeReader()
    if mode == "plain":
        return PlainReader()
    if mode == "auto":
        return AutoReader(xlsx_block_rows=xlsx_block_rows)
    raise ValueError(f"알 수 없는 extractor 모드: {mode!r}")
