"""Build bounded, privacy-safe PPT summaries from one run's log and final JUnit."""
from __future__ import annotations

import argparse
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    from .ppt_diagnostic_summary import build_short_report
except ImportError:  # Direct script execution by the PowerShell collector.
    from ppt_diagnostic_summary import build_short_report

MAX_BYTES = 16 * 1024 * 1024


def _read_bounded(path: Path) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("input_limit")
    return data


def _owned_paths(log: Path, xml: Path, run_id: str) -> bool:
    return (bool(re.fullmatch(r"[0-9a-f]{32}", run_id)) and log.name == f"AegisDesk-PptRename-{run_id}.txt"
            and xml.name == f"AegisDesk-PptRename-{run_id}.xml" and log.parent.resolve() == xml.parent.resolve()
            and not log.is_symlink() and not xml.is_symlink())


def finalize_report(log: Path, xml: Path, run_id: str, pytest_exit: int) -> int:
    """Atomically wrap this run's original log and remove only its owned XML."""
    owned = False
    temporary = log.with_name(log.name + ".rewrite")
    temporary_created = False
    encoding = None
    try:
        owned = _owned_paths(log, xml, run_id)
        if not owned:
            raise ValueError("run_identity")
        data = _read_bounded(log)
        encoding = ("utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else
                    "utf-8-sig" if data.startswith(b"\xef\xbb\xbf") else "utf-8")
        body = data.decode(encoding)
        xml_data = _read_bounded(xml)
        xml_text = xml_data.decode("utf-8-sig")
        if "<!DOCTYPE" in xml_text.upper() or "<!ENTITY" in xml_text.upper():
            raise ValueError("xml_declaration")
        ET.fromstring(xml_text)
        short = build_short_report(body, xml_data, pytest_exit)
        block = "\n".join(short) + "\n"
        with temporary.open("x", encoding=encoding, newline="") as output:
            temporary_created = True
            output.write(block + body + ("" if body.endswith("\n") else "\n") + block)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, log)
        # XML remains available until the final summary is safely in the txt log.
        xml.unlink()
        print(block, end="")
        return 0
    except (OSError, UnicodeError, ValueError, ET.ParseError):
        print("PPT_SHORT_REPORT status=INCOMPLETE required_gate=INCOMPLETE product_approval=false\n"
              "PPT_COLLECTION_ERROR code=report_collection_failed\nPPT_SHORT_END")
        return 1
    finally:
        if temporary_created:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                print("PPT_COLLECTION_ERROR code=intermediate_cleanup_failed")


def main() -> int:
    """Accept only the collector's current run paths and numeric pytest exit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--xml", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--pytest-exit", type=int, required=True)
    args = parser.parse_args()
    return finalize_report(args.log, args.xml, args.run_id, args.pytest_exit)


if __name__ == "__main__":
    sys.exit(main())
