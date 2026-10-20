"""Exercise real file orchestration with fake children, clock and retained target."""
from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

from scripts.diagnostics.ppt_capture.gate import LIVE_LOCK_REASON
from scripts.diagnostics.ppt_capture.layout import STAGES
from scripts.diagnostics.ppt_capture.runner import replay
from scripts.diagnostics.ppt_capture.supervisor import capture
from scripts.diagnostics.ppt_capture.transport import CollectorRecords, JsonlTail

RUN = "0f100000000000000000000000000001"
BINDING = dict(run_id=RUN, pid=123, creation=456)


def header(**extra):
    return dict(kind="header", schema=3, layout=1, **BINDING, frequency=1_000_000_000,
                owner_native_thread=7, published_seq=1, dropped=0, offthread=0, append_reentry=0,
                owner_managed_thread=8, apartment=0, qualifier=0,
                capture_start_ticks=1_000_000_000, pre_attach_records=0,
                capture_scope="bind_to_capture_end", counters_are_lower_bounds=True, **extra)


def slot():
    return dict(kind="slot", seq=1, qpc_ticks=1_000_000_000, stage_id=STAGES.index("Bind"),
                outcome_id=1, a=0, b=0, c=0, native_thread=7, managed_thread=8,
                service_depth=1, event_depth=0, generation=1, apartment=0, qualifier=0)


class Clock:
    def __init__(self):
        self.value = 1_000_000_000

    def now(self):
        return self.value

    def pause(self, seconds):
        self.value += int(seconds * 1_000_000_000)


class FakeChildren:
    def __init__(self, clock, *, failure=None, other_exit=False, exits=False, no_tail=False, bad_frame=False, close_error=False, stop_error=False):
        self.clock, self.failure, self.other_exit, self.exits = clock, failure, other_exit, exits
        self.no_tail, self.bad_frame = no_tail, bad_frame
        self.close_error, self.stop_error = close_error, stop_error
        self.ticks, self.tick_times, self.stops = 0, [], []
        self.ended = False

    def _write(self, name, row):
        self.bundle.emit(self.bundle._streams[name], row)
        self.bundle._streams[name].flush()

    def start(self, bundle):
        self.bundle = bundle
        for row in (dict(kind="collector_ready", run_id=RUN), header(), slot()):
            self._write("host.jsonl", row)
        if self.bad_frame:
            self._write("host.jsonl", dict(kind="unrecognized"))
        for event in ("request_begin", "request_return", "controller_refs_drop_begin", "controller_refs_drop_end", "CoUninitialize_begin", "CoUninitialize_end"):
            self._write("controller.jsonl", dict(BINDING, event=event, qpc_ns=self.clock.now()))

    def tick(self, collector, controller):
        self.ticks += 1
        self.tick_times.append(self.clock.now())

    def binding(self):
        return BINDING

    def observe(self):
        rows = [dict(BINDING, event="target_state", retained_handle=True, identity_matches=True,
                     wait_result=0 if self.exits and self.clock.now() >= 1_060_000_000 else 258, qpc_ns=self.clock.now())]
        if self.other_exit:
            rows.append(dict(rows[0], pid=999, wait_result=0))
        return rows

    def health(self):
        return self.failure if self.clock.now() >= 1_020_000_000 else None

    def stop(self, reason):
        self.stops.append(reason)
        if self.stop_error:
            raise OSError("injected_stop")
        if reason in {"target_exit", "deadline", "cancelled", "blocked"} and not self.ended and not self.no_tail:
            self.ended = True
            self._write("host.jsonl", header())
            self._write("host.jsonl", dict(kind="collector_end", persisted_seq=1, producer_ready=True))

    def close(self):
        if self.close_error:
            raise OSError("injected_close")
        return dict(caller=dict(alive=True), collector=dict(alive=not self.ended))


def run(tmp_path, **options):
    tmp_path.mkdir(parents=True, exist_ok=True)
    clock = Clock()
    backend = FakeChildren(clock, **options)
    result = capture(tmp_path / "capture", dict(run_id=RUN), backend, now=clock.now, pause=clock.pause,
                     duration_ms=100, drain_ms=40)
    return result, backend, clock


def test_caller_hang_still_reaches_deadline_and_records_liveness(tmp_path):
    result, backend, clock = run(tmp_path)
    assert result["capture_closed"]["reason"] == "deadline"
    assert result["capture_closed"]["children"]["caller"]["alive"] is True
    assert result["os_exit"] is False
    assert result["post_cleanup_observed_ms"] == 100
    assert clock.value <= 1_140_000_000
    assert not result["evidence_loss"]
    assert {p.name for p in (tmp_path / "capture").iterdir()} == {"host.jsonl", "controller.jsonl", "os.jsonl", "manifest.json", "summary.json", "coverage.json", "hashes.json"}


def test_exact_target_exit_closes_while_other_pid_exit_does_not(tmp_path):
    result, backend, clock = run(tmp_path, exits=True)
    assert result["os_exit"] is True
    assert result["capture_closed"]["reason"] == "target_exit"
    assert clock.value == 1_080_000_000
    other, _, _ = run(tmp_path / "foreign", other_exit=True)
    assert other["capture_closed"]["reason"] == "deadline"
    assert other["os_exit"] is False
    assert other["evidence_loss"]


@pytest.mark.parametrize("options", [dict(failure="collector_failed"), dict(bad_frame=True)])
def test_failure_closes_admission_without_shortening_target_observation(tmp_path, options):
    result, backend, clock = run(tmp_path, **options)
    assert result["capture_closed"]["reason"] == "deadline"
    assert result["os_exit"] is False
    assert result["evidence_loss"]
    if options.get("failure"):
        assert backend.tick_times == [1_000_000_000]
    else:
        assert backend.ticks == 0
    assert clock.value <= 1_140_000_000


@pytest.mark.parametrize("options,issue", [(dict(no_tail=True), "collector_final_drain_unconfirmed"),
    (dict(close_error=True), "cleanup_OSError"), (dict(stop_error=True), "stop_OSError")])
def test_drain_stop_or_cleanup_failure_preserves_incomplete_bundle(tmp_path, options, issue):
    result, backend, clock = run(tmp_path, **options)
    assert issue in result["evidence_issues"]
    assert result["evidence_loss"]
    assert (tmp_path / "capture" / "hashes.json").is_file()
    assert clock.value <= 1_140_000_000


def test_replay_end_to_end_keeps_failure_and_refuses_changed_files(tmp_path):
    result, _, _ = run(tmp_path, failure="collector_failed")
    copy = replay(tmp_path / "capture", tmp_path / "replayed")
    assert copy["os_exit"] == result["os_exit"]
    assert "collector_failed" in copy["evidence_issues"]
    with (tmp_path / "capture" / "controller.jsonl").open("a") as stream:
        stream.write("{}\n")
    with pytest.raises(ValueError, match="replay_bundle_hashes"):
        replay(tmp_path / "capture", tmp_path / "tampered")


@pytest.mark.parametrize("change", [dict(seq=True), dict(service_depth=-1), dict(event_depth="1"),
    dict(native_thread=0), dict(a="secret document"), dict(extra="text"), dict(qpc_ticks=-1)])
def test_collector_rejects_malformed_numeric_or_extra_fields(change):
    records = CollectorRecords(RUN)
    records.accept(dict(kind="collector_ready", run_id=RUN))
    records.accept(header())
    with pytest.raises(ValueError):
        records.accept(dict(slot(), **change))


def test_foreign_header_missing_tail_duplicate_keys_and_partial_lines(tmp_path):
    records = CollectorRecords(RUN)
    records.accept(dict(kind="collector_ready", run_id=RUN))
    with pytest.raises(ValueError):
        records.accept(dict(header(), run_id="1" * 32))
    path = tmp_path / "rows"
    path.write_bytes(b'{"kind":"a","kind":"b"}\n')
    with pytest.raises(ValueError, match="duplicate"):
        JsonlTail(path).poll()
    path.write_bytes(b'{"event":"late"')
    tail = JsonlTail(path)
    assert tail.poll() == [] and tail.partial


@pytest.mark.parametrize("mode", ["--live", "--controller"])
def test_live_and_private_entry_are_locked_before_native_import_or_output_creation(tmp_path, mode):
    output = tmp_path / "must-not-exist"
    result = subprocess.run([sys.executable, "-m", "scripts.diagnostics.ppt_capture", mode,
                             "--output", str(output)], capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == LIVE_LOCK_REASON
    assert not output.exists()


def test_private_spool_late_write_cannot_modify_frozen_bundle(tmp_path):
    class Spooling(FakeChildren):
        def start(self, bundle):
            private = tmp_path / "private"
            private.mkdir()
            self.spool_streams = {name: (private / name).open("w", encoding="utf-8") for name in ("host.jsonl", "controller.jsonl")}
            super().start(types.SimpleNamespace(_streams=self.spool_streams, emit=bundle.emit))
            return {name: private / name for name in self.spool_streams}

        def close(self):
            self._write("controller.jsonl", dict(BINDING, event="late_after_snapshot", qpc_ns=self.clock.now()))
            for stream in self.spool_streams.values():
                stream.close()
            return super().close()
    clock = Clock()
    child = Spooling(clock)
    result = capture(tmp_path / "bundle", dict(run_id=RUN), child, now=clock.now, pause=clock.pause,
                     duration_ms=100, drain_ms=40)
    assert "late_after_snapshot" not in (tmp_path / "bundle/controller.jsonl").read_text()
    assert "late_after_snapshot" in (tmp_path / "private/controller.jsonl").read_text()
    assert replay(tmp_path / "bundle", tmp_path / "replay")["os_exit"] is False
    assert result["capture_closed"]["children"]["caller"]["alive"]


def test_4096_slot_bundle_replays_above_metadata_limit(tmp_path):
    class Full(FakeChildren):
        def start(self, bundle):
            self.bundle = bundle
            self._write("host.jsonl", dict(kind="collector_ready", run_id=RUN))
            self._write("host.jsonl", dict(header(), published_seq=4096))
            for sequence in range(1, 4097):
                self._write("host.jsonl", dict(slot(), seq=sequence))

        def stop(self, reason):
            if not self.ended:
                self.ended = True
                self._write("host.jsonl", dict(header(), published_seq=4096))
                self._write("host.jsonl", dict(kind="collector_end", persisted_seq=4096, producer_ready=True))
    clock = Clock()
    result = capture(tmp_path / "full", dict(run_id=RUN), Full(clock), now=clock.now,
                     pause=clock.pause, duration_ms=40, drain_ms=40)
    assert not result["evidence_loss"]
    assert (tmp_path / "full/summary.json").stat().st_size > 1024 * 1024
    assert replay(tmp_path / "full", tmp_path / "replay")["persisted_seq"] == 4096


def test_raw_dispatch_synthetic_read_has_no_dynamic_property_access_or_body_trace(monkeypatch):
    from knowmate.secure.ppt_capture_com import Session, _synthetic_read
    body = "SYNTHETIC BODY MUST NOT BE IN TRACE"
    reply = json.dumps(dict(schema=1, slide=1, counts=dict(shapes=1, groups=0, tables=0, cells=0, errors=0),
                            seconds=0, nodes=[dict(kind="text", text=body)]))
    events = []
    class Raw:
        def __init__(self, values):
            self.values = values

        def GetIDsOfNames(self, lcid, name):
            return list(self.values).index(name) + 1

        def Invoke(self, dispid, lcid, flags, result, *arguments):
            return list(self.values.values())[dispid - 1]
    session = Session(None, {}, lambda event, **values: events.append(dict(event=event, **values)))
    monkeypatch.setattr(session, "check", lambda: None)
    session.holds.update(fixture=Raw(dict(Slides=Raw(dict(Count=1)))), service=Raw(dict(ExtractSlide=reply)))
    assert _synthetic_read(session, "synthetic.pptx") == body
    assert [row["operation"] for row in events] == ["GetIDsOfNames", "GetIDsOfNames", "Invoke", "Invoke"] * 3
    assert body not in json.dumps(events)


@pytest.mark.parametrize("failing_event", ["controller_refs_drop_begin", "controller_refs_drop_end", "CoUninitialize_begin"])
def test_emit_failure_still_drops_refs_before_balanced_uninitialize(monkeypatch, failing_event):
    import knowmate.secure.ppt_capture_com as adapter
    order = []
    pythoncom = types.ModuleType("pythoncom")
    pythoncom.COINIT_APARTMENTTHREADED = 2
    pythoncom.CoInitializeEx = lambda flags: order.append("initialize")
    pythoncom.CoUninitialize = lambda: order.append("uninitialize")
    client = types.ModuleType("win32com.client")
    package = types.ModuleType("win32com")
    package.client = client
    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", package)
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    class Held:
        def __del__(self):
            order.append("wrapper_dropped")
    def operate(session, com, client):
        session.holds["app"] = Held()
        return "blocked"
    monkeypatch.setattr(adapter, "_operate", operate)
    def emit(event, **fields):
        order.append(event)
        if event == failing_event:
            raise OSError("injected_disk")
    assert adapter.run_apartment(None, {}, emit) == 2
    assert order.count("uninitialize") == 1
    assert order.index("wrapper_dropped") < order.index("uninitialize")


def test_package_source_pin_subset_is_not_an_admissible_package(tmp_path):
    from scripts.diagnostics.ppt_capture.gate import PAYLOADS, digest, validate_package
    package = tmp_path / "package"
    package.mkdir()
    for name in PAYLOADS:
        (package / name).write_bytes(b"synthetic binary")
    source = Path(__file__).resolve().parents[2]
    manifest = dict(schema=3, capture_schema=3, capture_layout=1, reader_version="1.0.6.0", protocol=3,
        mode="candidate", default_enabled=False, files={name: digest(package / name) for name in PAYLOADS},
        source_files={"knowmate/version.py": digest(source / "knowmate/version.py")})
    (package / "capture-manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="capture_source_pin_set"):
        validate_package(package, source, dict(package_manifest_sha256=digest(package / "capture-manifest.json")))


@pytest.mark.parametrize("reject_invoke", [True, False])
def test_external_quit_is_counted_only_after_its_final_owner_check(monkeypatch, reject_invoke):
    from knowmate.secure.ppt_capture_com import Session, _external_quit
    events, invokes, checks = [], [], []
    class Raw:
        def GetIDsOfNames(self, lcid, name):
            return 2021

        def Invoke(self, *arguments):
            invokes.append(arguments)
            return None
    session = Session(None, {}, lambda event, **fields: events.append(event))
    session.holds["app"] = Raw()
    def check():
        checks.append(True)
        if reject_invoke and len(checks) == 2:
            raise RuntimeError("owner_changed_before_invoke")
    monkeypatch.setattr(session, "check", check)
    if reject_invoke:
        with pytest.raises(RuntimeError, match="owner_changed"):
            _external_quit(session)
        assert invokes == [] and "quit_begin" not in events
    else:
        assert _external_quit(session) is None
        assert len(invokes) == events.count("quit_begin") == 1


def test_output_lease_outlives_final_summary_coverage_and_hashes(tmp_path, monkeypatch):
    from scripts.diagnostics.ppt_capture.bundle import Bundle
    clock = Clock()
    child = FakeChildren(clock)
    child.lease_alive = True
    order = []
    original = Bundle.finish
    def finish(bundle):
        assert child.lease_alive
        assert (bundle.root / "summary.json").exists()
        assert (bundle.root / "coverage.json").exists()
        original(bundle)
        order.append("hashes_finished")
    def release():
        assert (tmp_path / "capture/hashes.json").exists()
        order.append("lease_released")
        child.lease_alive = False
    child.release_output = release
    monkeypatch.setattr(Bundle, "finish", finish)
    capture(tmp_path / "capture", dict(run_id=RUN), child, now=clock.now, pause=clock.pause,
            duration_ms=40, drain_ms=40)
    assert order == ["hashes_finished", "lease_released"]
