"""Primitive-only decoder for the diagnostic append buffer (schema 3/layout 1)."""
from __future__ import annotations

import struct
from typing import Any
from uuid import UUID

MAGIC = b"AEPCAP3\0"
SCHEMA = 3
LAYOUT = 1
HEADER_SIZE = 256
CAPACITY = 4096
SLOT_SIZE = 128
MAPPING_SIZE = HEADER_SIZE + CAPACITY * SLOT_SIZE
STAGES = ("Bind Prepare Admission Identity IdentityRelease Guard Resolve Sink Advise "
          "AlertsSet AlertsRead Presentations ProtectedViews SlideShows Poll Commit Ack "
          "Completion Cancel Disconnect Unadvise Release Capabilities CapabilityClose "
          "TimerCleanup ProtocolCleanup PrimaryError CleanupError FatalError Terminal "
          "Generation Reading Owner DeferredDepth Quit ServiceEntry ServiceExit EventEntry "
          "EventExit LifecycleEntry LifecycleExit IdentityObservation ErrorDetail").split()
OUTCOMES = "Entered Success Refused Failed Unknown Skipped".split()


class InvalidCapture(ValueError):
    """Reject malformed, foreign, or incomplete diagnostic evidence."""


def decode_header(data: bytes, run_id: str, pid: int, creation: int) -> dict[str, Any]:
    """Validate a copied header against an independently retained target identity."""
    if type(pid) is not int or pid <= 0 or type(creation) is not int or creation <= 0:
        raise InvalidCapture("target_identity")
    if len(data) != HEADER_SIZE or data[:8] != MAGIC:
        raise InvalidCapture("header_size_or_magic")
    schema, layout, capacity, slot_size, header_size, flags = struct.unpack_from("<6I", data, 8)
    if (schema, layout, capacity, slot_size, header_size) != (SCHEMA, LAYOUT, CAPACITY, SLOT_SIZE, HEADER_SIZE):
        raise InvalidCapture("layout")
    if UUID(bytes_le=data[32:48]).hex != UUID(run_id).hex:
        raise InvalidCapture("run_binding")
    target, owner = struct.unpack_from("<2I", data, 48)
    born, frequency = struct.unpack_from("<2q", data, 56)
    published, dropped, offthread, reentrant, ready = struct.unpack_from("<5I", data, 72)
    if (target, born) != (pid, creation) or not owner or frequency <= 0 or ready != 1:
        raise InvalidCapture("target_or_ready")
    if published > CAPACITY or flags != 1:
        raise InvalidCapture("published_or_flags")
    started = struct.unpack_from("<q", data, 96)[0]
    pre_attach, managed, apartment, qualifier = (struct.unpack_from("<I", data, offset)[0]
                                               for offset in (104, 112, 116, 120))
    if started <= 0 or managed <= 0 or apartment not in (0, 3) or any(data[92:96]) or any(data[108:112]) or any(data[124:]):
        raise InvalidCapture("header_reserved_or_time")
    return dict(schema=schema, layout=layout, run_id=UUID(run_id).hex, pid=target,
                creation=born, frequency=frequency, owner_native_thread=owner,
                published_seq=published, dropped=dropped, offthread=offthread,
                append_reentry=reentrant, counters_are_lower_bounds=True,
                capture_start_ticks=started, capture_scope="bind_to_capture_end",
                pre_attach_records=pre_attach, owner_managed_thread=managed,
                apartment=apartment, qualifier=qualifier)


def decode_slot(data: bytes, sequence: int, frequency: int) -> dict[str, Any]:
    """Decode one acquired, fully published slot; native addresses are never fields."""
    if len(data) != SLOT_SIZE or frequency <= 0:
        raise InvalidCapture("slot_size")
    seq, stage, ticks, result, a, b, c, native, managed, service, event, generation, apartment, qualifier = struct.unpack_from("<IIqi3i7i", data)
    if seq != sequence or not 0 <= stage < len(STAGES) or not 0 <= result < len(OUTCOMES) or ticks < 0:
        raise InvalidCapture("slot_sequence_or_enum")
    if native <= 0 or managed <= 0 or service < 0 or event < 0 or generation < 0 or apartment not in (0, 3) or any(data[60:]):
        raise InvalidCapture("slot_reserved_or_depth")
    return dict(seq=seq, stage=STAGES[stage], outcome=OUTCOMES[result],
                qpc_ns=ticks * 1_000_000_000 // frequency, values=[a, b, c],
                native_thread=native, managed_thread=managed, service_depth=service,
                event_depth=event, generation=generation, apartment=apartment, qualifier=qualifier)
