"""TextExtractor Protocol 인터페이스."""
from typing import Protocol


class TextExtractor(Protocol):
    def extract(self, path: str) -> str:
        """파일 경로를 받아 본문 텍스트를 반환한다."""
        ...
