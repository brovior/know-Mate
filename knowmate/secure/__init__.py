"""secure 패키지 — TextExtractor 팩토리 + AutoReader."""
from pathlib import Path

from knowmate.secure.base import TextExtractor
from knowmate.secure.fake_reader import FakeReader
from knowmate.secure.plain_reader import PlainReader

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
        """확장자에 따라 PlainReader 또는 ComReader로 파일을 파싱해 텍스트를 반환한다."""
        ext = Path(path).suffix.lower()
        if ext in {".doc", ".xls", ".ppt"}:
            # COM 의존 코드: secure/ 안에서만 import
            from knowmate.secure.com_reader import ComReader
            return ComReader().extract(path)
        return self._plain.extract(path)


def get_extractor(mode: str) -> TextExtractor:
    """mode에 따라 적합한 TextExtractor 인스턴스를 반환한다."""
    if mode == "fake":
        return FakeReader()
    if mode == "plain":
        return PlainReader()
    if mode == "auto":
        return AutoReader()
    raise ValueError(f"알 수 없는 extractor 모드: {mode!r}")
