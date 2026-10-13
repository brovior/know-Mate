"""Validate add-in replies without persisting or logging document contents."""
from __future__ import annotations
import json
import math
import re
from knowmate.secure.ppt_diagnostics import log_probe_read_errors, probe_error_details, _PROBE_KINDS, _PROBE_STAGES
from knowmate.secure.text_util import format_table

MAX_REPLY_CHARS = 32 * 1024 * 1024
FATAL_HRESULTS = frozenset({0x800706BA, 0x800706BE, 0x800706BF, 0x80010006,
                           0x80010007, 0x80010012, 0x80010108, 0x800401FD})

class PptPayloadError(RuntimeError):
    """A fixed, content-free reply validation error."""


def exception_hresult_codes(exc: BaseException) -> tuple[int, ...]:
    """Collect outer and safely decoded inner COM codes, including wrapped RPC."""
    codes = []
    outer = getattr(exc, "hresult", None)
    if type(outer) is not int:
        outer = exc.args[0] if exc.args and type(exc.args[0]) is int else None
    if type(outer) is int:
        codes.append(outer & 0xFFFFFFFF)
    details = probe_error_details(exc)
    for key in ("inner_hresult", "probe_inner_hresult"):
        if type(details.get(key)) is int:
            codes.append(details[key] & 0xFFFFFFFF)
    return tuple(dict.fromkeys(codes))


def fatal_hresult(exc: BaseException) -> int | None:
    """Return a fatal transport code even when DISP_E_EXCEPTION wraps it."""
    return next((code for code in exception_hresult_codes(exc) if code in FATAL_HRESULTS), None)


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
