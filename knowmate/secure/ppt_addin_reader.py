"""Production PPT service selection; COM references stay in the owning worker."""
from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Any, Callable
from knowmate.secure.ppt_payload import PptPayloadError, decode_slide, FATAL_HRESULTS
from knowmate.secure.ppt_diagnostics import PptReadDiagnostics
from knowmate.secure.ppt_operation import PptOperationError, PptPreparationPending
from knowmate.secure.office_guard import OfficeBusyError, OfficeCleanupPendingError

logger = logging.getLogger(__name__)
PROG_ID = "AegisDesk.PptReader"
REQUIRED_VERSION = "1.0.0.0"
PROTOCOL = 2


class PptAddinUnavailable(RuntimeError):
    """The service is missing/disabled/incompatible; never force activation."""


class PptPartialRead(PptPayloadError):
    """The add-in returned an incomplete slide; discard the entire candidate."""


def select_service(app: Any, cache: Any) -> Any:
    """Cache compatibility per exact app reference without retaining document data."""
    current = getattr(cache, "ppt_service", None)
    if current is not None and current[0] is app:
        if current[1] is None:
            raise PptAddinUnavailable(current[2])
        return current[1]
    try:
        addin = app.COMAddIns.Item(PROG_ID)
        if not bool(addin.Connect):
            raise PptAddinUnavailable("service_disabled")
        service = addin.Object
        if service is None:
            raise PptAddinUnavailable("service_unavailable")
        protocol = service.ProtocolVersion()
        version = service.ReaderVersion()
        if type(protocol) is not int or protocol != PROTOCOL or version != REQUIRED_VERSION:
            raise PptAddinUnavailable("service_incompatible")
    except (PptOperationError, PptPreparationPending, OfficeBusyError, OfficeCleanupPendingError):
        raise
    except PptAddinUnavailable as exc:
        cache.ppt_service = (app, None, str(exc))
        raise
    except Exception as exc:
        # Do not hide a packaged fatal code in availability selection.
        from knowmate.secure.ppt_payload import fatal_hresult
        if fatal_hresult(exc) is not None:
            raise
        cache.ppt_service = (app, None, "service_unavailable")
        raise PptAddinUnavailable("service_unavailable") from None
    cache.ppt_service = (app, service, "ready")
    logger.info("[ppt] 운영 서비스 준비: version=%s protocol=%d", version, protocol)
    return service


def read_slides(prs: Any, path: str, service: Any, check: Callable[[], None],
                note_count: Callable[[int], None] | None = None) -> str:
    """Read each slide once and reject partial output before returning any text."""
    with PptReadDiagnostics(path) as diagnostics:
        check()
        absolute_path = str(Path(path).resolve())
        check()
        count = diagnostics.read("slide_count", lambda: prs.Slides.Count, 0)
        check()
        if type(count) is not int or count < 0:
            raise PptPayloadError("invalid_slide_count")
        if note_count is not None:
            note_count(count)
        slides = []
        for index in range(1, count + 1):
            check()
            reply = diagnostics.read("addin_slide",
                                     lambda: service.ExtractSlide(absolute_path, index), index)
            check()
            texts, counts, seconds = decode_slide(reply, index, strict=True)
            logger.info("[ppt] 추가 기능 슬라이드: slide=%d shapes=%d groups=%d tables=%d cells=%d errors=%d characters=%d internal=%.2fs <%s>",
                        index, counts["shapes"], counts["groups"], counts["tables"], counts["cells"], counts["errors"],
                        sum(len(text) for text in texts), seconds, path)
            if counts["errors"]:
                # Error records are not logged verbatim; fatal inner codes prohibit fallback.
                records = json.loads(reply).get("read_errors", [])
                if isinstance(records, list):
                    for record in records:
                        code = record.get("hresult") if isinstance(record, dict) else None
                        if type(code) is int and (code & 0xFFFFFFFF) in FATAL_HRESULTS:
                            from knowmate.secure.com_reader import OfficeComPoisonError
                            raise OfficeComPoisonError("POWERPNT.EXE", code)
                raise PptPartialRead("partial_read")
            if texts:
                slides.append("\n".join(texts))
        return "\n\n".join(slides)
