"""PPT 변환·내부 추가 기능을 운영 코드 변경 없이 비교한다.

사용법과 추가 기능 준비: docs/guides/PPT_PROBE.md.
변환은 직접 만든 합성 파일만 허용한다. 업무 문서는 메모리 추출만 한다.
"""
from __future__ import annotations

import argparse
import gc
import json
import logging
import platform
import statistics
import struct
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from knowmate.secure import com_reader
from knowmate.secure.ppt_diagnostics import probe_error_details
from scripts.ppt_probe.readers import (
    PROG_ID, ProbeCloseFailure, ProbeFailure, Snapshot, compare, convert_synthetic, decode_slide,
    opened, read_addin, read_baseline, read_plain,
)


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    """안전 조건을 확인하고 검증 방법과 반복 횟수를 받는다."""
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--synthetic", action="store_true", help="업무 내용 없는 합성 PPT 생성")
    source.add_argument("--file", type=Path, help="기존 PPT/PPTX를 저장 없이 읽기")
    source.add_argument("--self-test", action="store_true", help="Office 없이 응답·내용 비교 계약 확인")
    parser.add_argument("--method", choices=("all", "conversion", "addin", "plain"), default="all")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--rows", type=int, default=100)
    parser.add_argument("--columns", type=int, default=10)
    parser.add_argument("--slides", type=int, default=3)
    args = parser.parse_args(argv)
    if not 1 <= args.repeat <= 20 or not 1 <= args.rows <= 1000 or not 1 <= args.columns <= 100:
        parser.error("repeat=1..20, rows=1..1000, columns=1..100 범위만 허용합니다.")
    if not 1 <= args.slides <= 100 or args.rows * args.columns * args.slides > 100000:
        parser.error("slides=1..100, 총 합성 셀 수는 100000 이하로 제한합니다.")
    if args.file:
        args.file = args.file.resolve()
        if not args.file.is_file() or args.file.suffix.lower() not in (".ppt", ".pptx"):
            parser.error("존재하는 .ppt/.pptx 파일을 지정하세요.")
        if args.method == "conversion":
            parser.error("평문 사본 비저장 계약: 실제 파일 변환은 지원하지 않습니다. --synthetic 사용.")
        if args.method == "plain" and args.file.suffix.lower() != ".pptx":
            parser.error("직접 파싱 비교는 .pptx 파일만 지원합니다.")
    if args.synthetic and args.method == "plain":
        parser.error("합성 .ppt 입력은 --method conversion 또는 addin을 사용하세요.")
    return args


def _emit(record: dict[str, Any]) -> None:
    print(json.dumps(record, ensure_ascii=True, allow_nan=False), flush=True)


def self_test() -> int:
    """합성 내용으로 응답 계약과 순서 비교를 확인하며 성능은 판정하지 않는다."""
    counts = {"shapes": 2, "groups": 0, "tables": 1, "cells": 2, "errors": 0}
    reply = json.dumps({"schema": 1, "slide": 1, "counts": counts, "seconds": 0,
                        "nodes": [{"kind": "text", "text": "합성 제목"},
                                  {"kind": "table", "cells": [["합성\r줄", ""]]}]})
    texts, decoded, _ = decode_slide(reply, 1)
    reference = Snapshot([["합성 제목", "합성 줄 | "]], counts)
    result = compare(reference, Snapshot([texts], decoded))
    _emit({"environment": "fake_contract", "performance_verified": False,
           "method": "self_test", **result})
    return 0 if result["equal"] else 1


def _service(app: Any) -> Any:
    # 설치·Connect=True·매크로 설정 변경은 하지 않는다. 이미 로드된 시험용 객체만 사용.
    addin = app.COMAddIns.Item(PROG_ID)
    if not addin.Connect:
        raise ProbeFailure("addin_not_loaded")
    service = addin.Object
    if service is None or service.ProtocolVersion() != 1:
        raise ProbeFailure("addin_protocol_unavailable")
    return service


def benchmark(app: Any, path: Path, args: argparse.Namespace, scratch: Path) -> int:
    """동일 파일에서 순서를 번갈아 비교하고 반복별 수치만 출력한다."""
    methods = ["conversion", "addin"] if args.method == "all" else [args.method]
    incomplete = False
    if not args.synthetic and "conversion" in methods:
        methods.remove("conversion")
        incomplete = True
        _emit({"method": "conversion", "status": "blocked", "reason": "real_file_save_forbidden"})
    service = None
    if "addin" in methods:
        try:
            service = _service(app)
        except Exception as exc:
            methods.remove("addin")
            incomplete = True
            _emit({"method": "addin", "status": "unavailable", "error_type": type(exc).__name__})
    original = (path.stat().st_size, path.stat().st_mtime_ns)
    failures = False
    for method in methods:
        elapsed: list[float] = []
        ratios: list[float] = []
        for iteration in range(args.repeat):
            result: dict[str, Snapshot] = {}
            order = ("baseline", method) if iteration % 2 == 0 else (method, "baseline")
            try:
                for name in order:
                    if name == "baseline":
                        result[name] = opened(app, path, lambda p: read_baseline(p, path))
                    elif name == "conversion":
                        result[name] = convert_synthetic(app, path, scratch / f"converted-{iteration}.pptx")
                    elif name == "addin":
                        result[name] = opened(app, path, lambda p: read_addin(p, path, service))
                    else:
                        started = time.perf_counter()
                        result[name] = read_plain(path)
                        result[name].seconds["total"] = time.perf_counter() - started
            except Exception as exc:
                if isinstance(exc, (ProbeCloseFailure, com_reader.OfficeComPoisonError)) or (
                    com_reader.is_fatal_transport_hresult(com_reader.hresult_of(exc))
                ):
                    raise
                failures = True
                _emit({"method": method, "iteration": iteration + 1, "status": "error",
                       "failed_stage": name, "error_type": type(exc).__name__,
                       "hresult": com_reader.hresult_of(exc),
                       **probe_error_details(exc)})
                break
            verdict = compare(result["baseline"], result[method])
            total = result[method].seconds["total"]
            ratio = result["baseline"].seconds["total"] / total if total > 0 else 0
            elapsed.append(total)
            ratios.append(ratio)
            failures |= not verdict["equal"]
            _emit({"method": method, "iteration": iteration + 1, "order": list(order),
                   "status": "pass" if verdict["equal"] else "mismatch",
                   "baseline_seconds": result["baseline"].seconds, "speedup": ratio, **verdict})
        if elapsed:
            _emit({"method": method, "completed_repeats": len(elapsed),
                   "median_total_seconds": statistics.median(elapsed),
                   "median_speedup": statistics.median(ratios), "warmup_included": True})
    unchanged = original == (path.stat().st_size, path.stat().st_mtime_ns)
    _emit({"source_unchanged": unchanged,
           "performance_verified": bool(methods) and not failures and not incomplete,
           "adoption_decided": False})
    return 1 if failures or not unchanged else 2 if incomplete else 0


def main(argv: list[str] | None = None) -> int:
    """실데이터를 저장하지 않고 비교하며 미검증은 종료 코드 2로 구분한다."""
    args = arguments(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.self_test:
        return self_test()
    try:
        dispatch = time.perf_counter()
        app = com_reader._get_ppt_app()
        _emit({"environment": "synthetic" if args.synthetic else "real_read_only",
               "python": platform.python_version(), "python_bits": struct.calcsize("P") * 8,
               "office_version": str(app.Version), "dispatch_seconds": time.perf_counter() - dispatch,
               "tracing": tracemalloc.is_tracing(),
               "trace_depth": tracemalloc.get_traceback_limit() if tracemalloc.is_tracing() else 0})
        with tempfile.TemporaryDirectory(prefix="aegis_ppt_probe_") as directory:
            scratch = Path(directory)
            path = args.file
            if args.synthetic:
                from scripts.ppt_probe.fixture import create_legacy, create_pptx
                source = scratch / "synthetic.pptx"
                path = scratch / "synthetic.ppt"
                create_pptx(source, args.rows, args.columns, args.slides)
                create_legacy(app, source, path)
                _emit({"fixture_created": True, "preparation_in_benchmark": False})
            assert path is not None
            return benchmark(app, path, args, scratch)
    except Exception as exc:
        _emit({"status": "error", "error_type": type(exc).__name__,
               "hresult": com_reader.hresult_of(exc)})
        return 1
    finally:
        # PowerPoint에 Quit·강제 종료·추가 Close를 요청하지 않는다.
        com_reader._tls.ppt = None
        gc.collect()


if __name__ == "__main__":
    raise SystemExit(main())
