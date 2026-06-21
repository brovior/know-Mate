"""파일 타입별 청크 분할 전략 (CLAUDE.md 6-4, 6-5)."""
import re


def chunk_text(
    text: str,
    file_type: str,
    chunk_size: int = 400,
    overlap: int = 80,
) -> list[str]:
    """텍스트를 파일 타입에 맞는 전략으로 분할해 청크 리스트를 반환한다."""
    if not text or not text.strip():
        return []

    ft = file_type.lower().lstrip(".")

    if ft in {"txt", "md", "log"}:
        segments = re.split(r"\n{2,}", text)
        return _merge_and_split(segments, chunk_size, overlap)

    if ft == "docx":
        segments = [line for line in text.splitlines() if line.strip()]
        return _merge_and_split(segments, chunk_size, overlap)

    if ft == "pdf":
        # 페이지 단위 독립 처리 → 초과 시 재분할
        pages = re.split(r"\n{2,}", text)
        chunks: list[str] = []
        for page in pages:
            page = page.strip()
            if not page:
                continue
            if len(page) <= chunk_size:
                chunks.append(page)
            else:
                chunks.extend(_split_by_size(page, chunk_size, overlap))
        return chunks

    if ft == "pptx":
        # 슬라이드 단위 독립 처리 → 초과 시 재분할
        slides = re.split(r"\n{2,}", text)
        chunks = []
        for slide in slides:
            slide = slide.strip()
            if not slide:
                continue
            if len(slide) <= chunk_size:
                chunks.append(slide)
            else:
                chunks.extend(_split_by_size(slide, chunk_size, overlap))
        return chunks

    if ft in {"xlsx", "xls"}:
        lines = [l for l in text.splitlines() if l.strip()]
        if len(lines) <= 20:
            joined = "\n".join(lines)
            return [joined] if joined.strip() else []
        # 5행씩 분할
        chunks = []
        for i in range(0, len(lines), 5):
            group = "\n".join(lines[i : i + 5])
            if group.strip():
                chunks.append(group)
        return chunks

    # 그 외 파일 타입 — 기본 크기 분할
    return _split_by_size(text, chunk_size, overlap)


def _split_by_size(text: str, chunk_size: int, overlap: int) -> list[str]:
    """텍스트를 chunk_size씩, step=chunk_size-overlap 으로 슬라이싱한다."""
    if not text.strip():
        return []
    step = max(1, chunk_size - overlap)
    chunks: list[str] = []
    start = 0
    while start < len(text):
        chunk = text[start : start + chunk_size].strip()
        if chunk:
            chunks.append(chunk)
        start += step
    return chunks


def _merge_and_split(
    segments: list[str], chunk_size: int, overlap: int
) -> list[str]:
    """세그먼트를 합치다 chunk_size 초과 시 새 청크 시작. 초과 세그먼트는 재분할."""
    chunks: list[str] = []
    buffer = ""

    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue

        if len(seg) > chunk_size:
            # 버퍼 먼저 flush
            if buffer.strip():
                chunks.append(buffer.strip())
                buffer = ""
            chunks.extend(_split_by_size(seg, chunk_size, overlap))
            continue

        candidate = (buffer + "\n" + seg).strip() if buffer else seg
        if len(candidate) > chunk_size:
            if buffer.strip():
                chunks.append(buffer.strip())
            buffer = seg
        else:
            buffer = candidate

    if buffer.strip():
        chunks.append(buffer.strip())

    return chunks
