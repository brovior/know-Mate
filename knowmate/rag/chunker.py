"""파일 타입별 청크 분할 전략 (CLAUDE.md 6-4, 6-5)."""
import logging
import re

logger = logging.getLogger(__name__)

# chunk_text()에 전달하는 옵션 기본값
_DEFAULT_MAX_CHUNKS = 500
_DEFAULT_XLSX_MAX_ROWS = 2000


def chunk_text(
    text: str,
    file_type: str,
    chunk_size: int = 400,
    overlap: int = 80,
    max_chunks_per_file: int = _DEFAULT_MAX_CHUNKS,
    xlsx_max_rows_per_sheet: int = _DEFAULT_XLSX_MAX_ROWS,
) -> list[str]:
    """텍스트를 파일 타입에 맞는 전략으로 분할해 청크 리스트를 반환한다.

    max_chunks_per_file: 파일당 최대 청크 수. 초과분은 잘라낸다 (최후 안전망).
    xlsx_max_rows_per_sheet: xlsx 시트당 최대 행 수. 초과 시트는 메타 청크 1개로 대체.
    """
    if not text or not text.strip():
        return []

    ft = file_type.lower().lstrip(".")

    if ft in {"txt", "md", "log"}:
        segments = re.split(r"\n{2,}", text)
        chunks = _merge_and_split(segments, chunk_size, overlap)

    elif ft == "docx":
        segments = [line for line in text.splitlines() if line.strip()]
        chunks = _merge_and_split(segments, chunk_size, overlap)

    elif ft == "pdf":
        # 페이지 단위 독립 처리 → 초과 시 재분할
        pages = re.split(r"\n{2,}", text)
        chunks = []
        for page in pages:
            page = page.strip()
            if not page:
                continue
            if len(page) <= chunk_size:
                chunks.append(page)
            else:
                chunks.extend(_split_by_size(page, chunk_size, overlap))

    elif ft == "pptx":
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

    elif ft in {"xlsx", "xls"}:
        chunks = _chunk_xlsx(text, xlsx_max_rows_per_sheet)

    else:
        # 그 외 파일 타입 — 기본 크기 분할
        chunks = _split_by_size(text, chunk_size, overlap)

    # 파일당 최대 청크 수 상한 (최후 안전망)
    if len(chunks) > max_chunks_per_file:
        logger.warning(
            "청크 수 상한 초과 (%d → %d): max_chunks_per_file=%d",
            len(chunks), max_chunks_per_file, max_chunks_per_file,
        )
        chunks = chunks[:max_chunks_per_file]

    return chunks


def _chunk_xlsx(text: str, max_rows_per_sheet: int) -> list[str]:
    """xlsx/xls 텍스트를 시트 단위로 분할한다.

    시트 구분자는 plain_reader가 삽입한 '=== 시트: ...' 헤더로 감지한다.
    헤더가 없으면 전체를 단일 시트로 취급한다.
    max_rows_per_sheet 초과 시트는 메타 청크 1개로 대체한다.
    """
    # plain_reader의 시트 헤더 패턴: "=== 시트: <이름> ==="
    sheet_pattern = re.compile(r"^=== 시트: (.+?) ===$", re.MULTILINE)
    sheet_headers = list(sheet_pattern.finditer(text))

    if not sheet_headers:
        # 시트 구분자 없음 → 단일 시트로 처리
        return _chunk_single_sheet(text, "", max_rows_per_sheet)

    chunks: list[str] = []
    for idx, match in enumerate(sheet_headers):
        sheet_name = match.group(1)
        start = match.end()
        end = sheet_headers[idx + 1].start() if idx + 1 < len(sheet_headers) else len(text)
        sheet_text = text[start:end].strip()
        chunks.extend(_chunk_single_sheet(sheet_text, sheet_name, max_rows_per_sheet))

    return chunks


def _chunk_single_sheet(text: str, sheet_name: str, max_rows_per_sheet: int) -> list[str]:
    """단일 시트 텍스트를 청킹한다. 행 초과 시 메타 청크 1개를 반환한다."""
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return []

    row_count = len(lines)

    if row_count > max_rows_per_sheet:
        # 첫 행에서 컬럼 정보 추출
        col_info = lines[0] if lines else ""
        label = f"[시트: {sheet_name}] " if sheet_name else ""
        meta = (
            f"{label}{row_count}행 데이터 시트 (최대 {max_rows_per_sheet}행 초과로 전문 인덱싱 생략). "
            f"컬럼 정보: {col_info}"
        )
        logger.warning(
            "xlsx 시트 행 수 초과 → 메타 청크 대체: 시트='%s' 행수=%d 상한=%d",
            sheet_name, row_count, max_rows_per_sheet,
        )
        return [meta]

    if row_count <= 20:
        joined = "\n".join(lines)
        return [joined] if joined.strip() else []

    # 5행씩 분할
    chunks = []
    for i in range(0, row_count, 5):
        group = "\n".join(lines[i : i + 5])
        if group.strip():
            chunks.append(group)
    return chunks


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
