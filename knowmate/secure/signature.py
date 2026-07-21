"""파일 매직 바이트로 실제 포맷을 판별하는 유틸.

확장자가 OOXML(.docx/.xlsx/.pptx)이라도 실제 내용이 구형 OLE2 바이너리인
오라벨 파일을 가려내기 위함 (CLAUDE.md 6-6의 '확장자만으로 판별' 전제 보완).
"""
from pathlib import Path

# 구형 Office 바이너리(doc/xls/ppt)는 OLE2 복합 문서 → 아래 8바이트로 시작
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
# OOXML(docx/xlsx/pptx)은 ZIP → 'PK\x03\x04' 로 시작
_ZIP_MAGIC = b"PK\x03\x04"


def is_ole2(path: str) -> bool:
    """파일이 OLE2 복합 문서(구형 Office 바이너리)이면 True를 반환한다."""
    try:
        with open(path, "rb") as f:
            return f.read(8) == _OLE2_MAGIC
    except OSError:
        return False


def is_zip(path: str) -> bool:
    """파일이 ZIP(OOXML 컨테이너)이면 True를 반환한다."""
    try:
        with open(path, "rb") as f:
            return f.read(4) == _ZIP_MAGIC
    except OSError:
        return False


class UnreadableFormatError(RuntimeError):
    """확장자는 OOXML이나 실제 zip이 아니어서(DRM 래핑·손상 등) 라이브러리로
    파싱할 수 없을 때 발생한다. COM 경유도 불가한 최종 안전망(fake/plain 모드,
    비Windows 등)에서만 발생 — COM 가능 환경(AutoReader)에서는 이 예외 대신
    COM 경로로 라우팅되어 정상 처리된다."""
