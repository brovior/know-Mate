"""Validate add-in replies without persisting or logging document contents."""
from __future__ import annotations
import json
import math
import re
from knowmate.secure.ppt_diagnostics import log_probe_read_errors, _PROBE_KINDS, _PROBE_STAGES
from knowmate.secure.text_util import format_table
from knowmate.secure.com_fault import FATAL_HRESULTS as FATAL_HRESULTS
from knowmate.secure import com_fault

MAX_REPLY_CHARS = 32 * 1024 * 1024

class PptPayloadError(RuntimeError):
    """A fixed, content-free reply validation error."""


def exception_hresult_codes(exc: BaseException, *, app_exe: str | None = "POWERPNT.EXE") -> tuple[int, ...]:
    """Collect outer and safely decoded inner COM codes, including wrapped RPC."""
    return com_fault.exception_hresult_codes(exc, app_exe=app_exe)


def fatal_hresult(exc: BaseException, *, app_exe: str | None = "POWERPNT.EXE") -> int | None:
    """Return a fatal transport code even when DISP_E_EXCEPTION wraps it."""
    return com_fault.fatal_hresult(exc, app_exe=app_exe)


def decode_slide(reply: str, slide: int, *, strict: bool = False) -> tuple[list[str], dict[str, int], float]:
    """추가 기능의 응답을 검증하고 기존 표 정렬을 적용한다."""
    if not isinstance(reply, str) or len(reply) > MAX_REPLY_CHARS:
        raise PptPayloadError("invalid_reply_size")
    try:
        payload = json.loads(reply)
    except (ValueError, RecursionError) as exc:
        raise PptPayloadError("invalid_reply_json") from exc
    if not isinstance(payload, dict) or type(payload.get("schema")) is not int or payload.get("schema") != 1 or type(payload.get("slide")) is not int or payload.get("slide") != slide:
        raise PptPayloadError("invalid_reply_schema")
    counts = payload.get("counts")
    keys = {"shapes", "groups", "tables", "cells", "errors"}
    if not isinstance(counts, dict) or set(counts) != keys:
        raise PptPayloadError("invalid_reply_counts")
    if any(type(v) is not int or v < 0 for v in counts.values()):
        raise PptPayloadError("invalid_reply_counts")
    seconds = payload.get("seconds")
    try:
        finite = type(seconds) in (int, float) and math.isfinite(seconds) and seconds >= 0
    except OverflowError:
        finite = False
    if not finite:
        raise PptPayloadError("invalid_reply_time")
    nodes = payload.get("nodes")
    if not isinstance(nodes, list):
        raise PptPayloadError("invalid_reply_nodes")
    texts: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            raise PptPayloadError("invalid_reply_node")
        if node.get("kind") == "text" and isinstance(node.get("text"), str):
            text = node["text"].strip()
        elif node.get("kind") == "table" and isinstance(node.get("cells"), list):
            grid = node["cells"]
            if any(not isinstance(row, list) or any(not isinstance(c, str) for c in row) for row in grid):
                raise PptPayloadError("invalid_reply_cells")
            text = format_table(grid)
        else:
            raise PptPayloadError("invalid_reply_node")
        if text.strip():
            texts.append(text)
    if strict:
        _validate_complete_structure(payload, counts, nodes)
    log_probe_read_errors(payload, slide)
    return texts, counts, float(seconds)



def _validate_complete_structure(payload: dict, counts: dict, nodes: list) -> None:
    records = payload.get("read_errors", [])
    omitted = payload.get("errors_omitted", max(0, counts["errors"] - len(records)) if isinstance(records, list) else None)
    if not isinstance(records, list) or len(records) > 16 or type(omitted) is not int or omitted < 0:
        raise PptPayloadError("invalid_reply_errors")
    if len(records) + omitted != counts["errors"]:
        raise PptPayloadError("invalid_reply_errors")
    for record in records:
        if not isinstance(record, dict):
            raise PptPayloadError("invalid_reply_error")
        shape = record.get("shape")
        stage, kind = record.get("stage"), record.get("kind")
        if (not isinstance(stage, str) or not isinstance(kind, str)
                or stage not in _PROBE_STAGES or kind not in _PROBE_KINDS
                or not isinstance(shape, str) or len(shape) > 1024
                or re.fullmatch(r"[1-9][0-9]*(?:\.[1-9][0-9]*)*", shape) is None):
            raise PptPayloadError("invalid_reply_error")
        if any(type(record.get(key)) is not int or record[key] < 0 for key in ("row", "column", "hresult")):
            raise PptPayloadError("invalid_reply_error")
        if record["hresult"] > 0xFFFFFFFF:
            raise PptPayloadError("invalid_reply_error")
    if counts["groups"] + counts["tables"] > counts["shapes"] or len(nodes) > counts["shapes"]:
        raise PptPayloadError("invalid_reply_counts")
    if not counts["errors"]:
        tables = [node["cells"] for node in nodes if node["kind"] == "table"]
        if len(tables) != counts["tables"] or sum(len(row) for grid in tables for row in grid) != counts["cells"]:
            raise PptPayloadError("invalid_reply_counts")
        if any(grid and any(len(row) != len(grid[0]) for row in grid) for grid in tables):
            raise PptPayloadError("invalid_reply_cells")
