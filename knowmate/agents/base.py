"""AgentBackend 인터페이스 + Block 타입 정의 (CLAUDE.md 4-1)."""
from typing import TypedDict, Literal, Protocol


class TextBlock(TypedDict):
    type: Literal["text"]
    content: str


class SourceItem(TypedDict):
    badge: str       # "메일" | "문서"
    title: str
    subtitle: str
    score: float
    path: str


class SourcesBlock(TypedDict):
    type: Literal["sources"]
    title: str
    items: list[SourceItem]


class TableBlock(TypedDict):
    type: Literal["table"]
    title: str
    columns: list[str]
    rows: list[list]


class ChartBlock(TypedDict):
    type: Literal["chart"]
    chart_type: Literal["line", "bar"]
    title: str
    x: list[str]
    series: list[dict]


Block = TextBlock | SourcesBlock | TableBlock | ChartBlock


class AgentBackend(Protocol):
    def handle(self, query: str, context: dict) -> list[Block]:
        """질문을 받아 UI 블록 리스트를 반환한다."""
        ...
