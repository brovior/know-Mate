"""secure 리더 공용 텍스트 유틸 (plain_reader / com_reader 공유)."""


def format_table(rows: list[list[str]]) -> str:
    """표 행렬을 행마다 ' | '로 묶은 텍스트로 변환한다 (빈 표는 빈 문자열).

    셀 내부의 줄바꿈(\\n, COM의 \\r 포함)은 공백으로 치환해 한 줄로 만든다.
    값이 모두 빈 행은 건너뛴다.
    """
    lines: list[str] = []
    for row in rows:
        cells = [(c or "").strip().replace("\r", " ").replace("\n", " ") for c in row]
        if any(cells):
            lines.append(" | ".join(cells))
    return "\n".join(lines)
