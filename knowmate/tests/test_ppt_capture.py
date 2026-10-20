"""Regression checks for evidence scope, core-first collection, and one-shot paths."""
from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from scripts.diagnostics.ppt_capture.bundle import Bundle
from scripts.diagnostics.ppt_capture.controller import OneShotRequest, observe_for_capture
from scripts.diagnostics.ppt_capture.identity import collect_core
from scripts.diagnostics.ppt_capture.layout import CAPACITY, HEADER_SIZE, MAGIC, SLOT_SIZE, decode_header, decode_slot
from scripts.diagnostics.ppt_capture.report import summarize

RUN = "1234567890abcdef1234567890abcdef"
BINDING = dict(run_id=RUN, pid=42, creation=123)


class Backend:
    """Return stable canonical objects and refuse all optional getters."""
    def __init__(self) -> None:
        self.calls: list[str] = []
    def unwrap_candidate(self, value):
        return value, dict(explicit_variant=False, vartype=None)
    def raw(self, value):
        return value
    def application(self, value):
        self.calls.append("application_qi")
        return value
    def canonical(self, value):
        self.calls.append("canonical")
        return value
    def typeinfo(self, value):
        self.calls.append("metadata")
        return dict(guid="00000000-0000-0000-0000-000000000001", typekind=4, major=1, minor=0)
    def get(self, *args):
        raise AssertionError("optional_getter_not_allowed")
    item = get


def host_report():
    return dict(schema=2, usable=True, outcome="complete", stopped=False, primary=None,
        cleanupErrors=[], cleanupFailed=False, dropped=0, acquired=1, releaseAttempts=1, released=1,
        ownership=[dict(id=1, acquired=True, attempted=True, released=True, blocked=False)],
        rows=[dict(name=name, status="observed", selfControl=1, group=0)
              for name in ("host", "candidate", "host.Application", "candidate.Application")])


class CaptureTests(unittest.TestCase):
    def test_metadata_is_after_all_core_identity(self):
        backend=Backend();app=object()
        result=collect_core(app,("nonce",app),backend)
        self.assertTrue(result["usable"])
        self.assertEqual(backend.calls.count("canonical"),5)
        self.assertLess(max(i for i,x in enumerate(backend.calls) if x=="canonical"),backend.calls.index("metadata"))
        self.assertTrue(all(row["status"]=="omitted_by_plan" for row in result["rows"][-4:]))

    def test_identity_failure_stops_without_metadata_or_host(self):
        class Broken(Backend):
            def canonical(self,value):
                raise RuntimeError("private synthetic payload must not escape")
        app=object();calls=[];exports=[]
        result=observe_for_capture(app,app,"nonce",Broken(),lambda value:calls.append(value),exports.append,lambda event:None)
        self.assertFalse(result["eligible"]);self.assertFalse(calls)
        self.assertNotIn("private synthetic",str(result))
        self.assertEqual(len(exports),1)

    def test_each_failed_boundary_records_numeric_failure_without_description(self):
        class Broken(Backend):
            def canonical(self,value):
                error=RuntimeError("private description")
                error.hresult=-2147352567
                error.excepinfo=(7,"private source","private text",None,0,-2147467259)
                raise error
        events=[];app=object();result=collect_core(app,("nonce",app),Broken(),events.append)
        self.assertFalse(result["usable"])
        self.assertEqual([row["event"] for row in events], ["com_begin","com_return","com_begin","com_fail"])
        failure=events[-1]
        self.assertEqual(failure["wcode"],7)
        self.assertEqual(failure["inner_hresult"],-2147467259)
        self.assertNotIn("private",str(events))

    def test_metadata_failure_preserves_both_canonical_groups(self):
        class Broken(Backend):
            def typeinfo(self,value):
                raise RuntimeError("metadata")
        app=object();result=collect_core(app,("nonce",app),Broken())
        rows={row["name"]:row for row in result["rows"]}
        self.assertTrue(result["core_complete"])
        self.assertEqual(rows["candidate"]["group"],rows["rot_app"]["group"])
        self.assertFalse(result["usable"])

    def test_host_cleanup_failure_and_export_failure_block_request(self):
        app=object()
        for mode in ("cleanup","export"):
            host=host_report()
            if mode=="cleanup":host["cleanupFailed"]=True
            def export(value):
                if mode=="export":raise OSError("private output path")
            result=observe_for_capture(app,app,"nonce",Backend(),lambda candidate:host,export,lambda event:None)
            self.assertFalse(result["eligible"])

    def test_internal_does_not_call_external_protocol_and_is_one_shot(self):
        events=[];calls=[];request=OneShotRequest("internal",events.append)
        def forbidden(*args):raise AssertionError("external path")
        args=(dict(eligible=True,exported=True,primary=None),lambda:calls.append("internal") or "quit_requested",forbidden,forbidden,forbidden,forbidden,forbidden,forbidden,lambda:None)
        self.assertEqual(request.run(*args),"quit_requested")
        self.assertEqual(calls,["internal"])
        with self.assertRaisesRegex(RuntimeError,"request_consumed"):request.run(*args)

    def test_observer_write_failure_prevents_host_and_request(self):
        app=object();calls=[]
        def broken_emit(event):
            raise OSError("output unavailable")
        observed=observe_for_capture(app,app,"nonce",Backend(),lambda value:calls.append("host"),lambda value:None,broken_emit)
        self.assertFalse(observed["eligible"])
        self.assertFalse(calls)

    def test_external_failure_does_not_complete_or_retry(self):
        for fail in ("prepare","status","ack","quit"):
            calls=[];events=[]
            def prepare():calls.append("prepare");return "refused" if fail=="prepare" else "ready"
            def status():calls.append("status");return "bad" if fail=="status" else "identity_result=Same identity=2/2 phase=Ready"
            def ack(timeout):calls.append("ack");return bool(timeout) and fail!="ack"
            def quit_call():calls.append("quit");raise RuntimeError("opaque")
            result=OneShotRequest("external",events.append).run(dict(eligible=True,exported=True),lambda:None,prepare,status,lambda:calls.append("start"),ack,quit_call,lambda:calls.append("complete"),lambda:None)
            self.assertNotEqual(result,"quit_requested")
            self.assertNotIn("complete",calls)
            self.assertLessEqual(calls.count("quit"),1)
            if fail=="prepare":self.assertEqual(calls,["prepare"])

    def test_header_and_slot_reject_foreign_or_partial_data(self):
        from uuid import UUID
        raw=bytearray(HEADER_SIZE);raw[:8]=MAGIC
        struct.pack_into("<6I",raw,8,3,1,CAPACITY,SLOT_SIZE,HEADER_SIZE,1);raw[32:48]=UUID(RUN).bytes_le
        struct.pack_into("<2I2q5I",raw,48,42,7,123,1_000_000_000,1,0,0,0,1)
        struct.pack_into("<q",raw,96,1);struct.pack_into("<3I",raw,112,8,3,0)
        header=decode_header(bytes(raw),RUN,42,123)
        self.assertEqual(header["published_seq"],1)
        with self.assertRaises(ValueError):decode_header(bytes(raw),RUN,43,123)
        slot=bytearray(SLOT_SIZE);struct.pack_into("<IIqi3i7i",slot,0,1,35,100,0,0,0,0,7,8,1,1,0,3,0)
        self.assertEqual(decode_slot(bytes(slot),1,1_000_000_000)["service_depth"],1)
        slot[127]=1
        with self.assertRaises(ValueError):decode_slot(bytes(slot),1,1_000_000_000)

    def test_request_complete_is_not_os_exit_or_lifecycle_complete(self):
        header=dict(BINDING,published_seq=0,dropped=0,offthread=0,append_reentry=0)
        controller=[dict(BINDING,event="request_return",qpc_ns=10)]
        os_events=[dict(BINDING,event="target_state",retained_handle=True,identity_matches=True,wait_result=258,qpc_ns=20)]
        result=summarize(BINDING,header,[],controller,os_events,dict(reason="deadline",qpc_ns=30),0)
        self.assertTrue(result["request_complete"])
        self.assertFalse(result["os_exit"])
        self.assertFalse(result["coverage"]["host_lifecycle_complete"])
        self.assertEqual(result["coverage"]["stages"]["Disconnect"]["reached"],"unknown")

    def test_drop_reentry_and_offthread_are_evidence_loss(self):
        for counter in ("dropped","offthread","append_reentry"):
            header=dict(BINDING,published_seq=0,dropped=0,offthread=0,append_reentry=0);header[counter]=1
            result=summarize(BINDING,header,[],[],[],dict(reason="deadline",qpc_ns=30),0)
            self.assertTrue(result["evidence_loss"])
            self.assertIn(counter,result["evidence_issues"])

    def test_cleanup_interval_is_measured_not_assumed(self):
        header=dict(BINDING,published_seq=0,dropped=0,offthread=0,append_reentry=0)
        events=[dict(BINDING,event="controller_refs_drop_end",qpc_ns=9_000_000),dict(BINDING,event="CoUninitialize_end",qpc_ns=9_500_000)]
        os_events=[dict(BINDING,event="target_state",qpc_ns=t,retained_handle=True,identity_matches=True,wait_result=258) for t in (8_000_000,10_000_000)]
        result=summarize(BINDING,header,[],events,os_events,dict(reason="deadline",qpc_ns=10_000_000),0)
        self.assertEqual(result["post_cleanup_observed_ms"],0.5)
        result=summarize(BINDING,header,[],events[:1],[],dict(reason="deadline",qpc_ns=10_000_000),0)
        self.assertIsNone(result["post_cleanup_observed_ms"])

    def test_foreign_or_late_return_cannot_prove_completion(self):
        header=dict(BINDING,published_seq=0,dropped=0,offthread=0,append_reentry=0)
        for row in (dict(BINDING,pid=99,event="request_return",qpc_ns=5),dict(BINDING,event="request_return",qpc_ns=50)):
            result=summarize(BINDING,header,[],[row],[],dict(reason="deadline",qpc_ns=30),0)
            self.assertIsNone(result["request_complete"])
            self.assertTrue(result["evidence_loss"])

    def test_cleanup_interval_ends_at_last_valid_os_observation(self):
        header=dict(BINDING,published_seq=0,dropped=0,offthread=0,append_reentry=0)
        controller=[dict(BINDING,event="controller_refs_drop_end",qpc_ns=5_000_000),dict(BINDING,event="CoUninitialize_end",qpc_ns=6_000_000)]
        os_events=[dict(BINDING,event="target_state",qpc_ns=t,retained_handle=True,identity_matches=True,wait_result=258) for t in (1_000_000,7_000_000)]
        result=summarize(BINDING,header,[],controller,os_events,dict(reason="deadline",qpc_ns=100_000_000),0)
        self.assertEqual(result["post_cleanup_observed_ms"],1)

    def test_external_quit_count_is_not_inferred_from_host_guard(self):
        header=dict(BINDING,published_seq=0,dropped=0,offthread=0,append_reentry=0)
        result=summarize(BINDING,header,[],[dict(BINDING,event="quit_begin",qpc_ns=5)],[],dict(reason="deadline",qpc_ns=30),0)
        self.assertEqual(result["quit_calls"],1)
        self.assertEqual(result["controller_quit_calls_observed"],1)
        self.assertEqual(result["host_quit_calls_observed"],0)

    def test_bundle_never_overwrites_or_includes_unrelated_files(self):
        with tempfile.TemporaryDirectory() as parent:
            path=Path(parent)/"capture";bundle=Bundle(path)
            bundle.write("manifest.json",dict(mode="offline_fixture"))
            with self.assertRaises(ValueError):bundle.write("manifest.json",{})
            with self.assertRaises(ValueError):bundle.write("secret.txt",{})
            bundle.finish()
            self.assertEqual({p.name for p in path.iterdir()},{"manifest.json","hashes.json"})
            with self.assertRaises(FileExistsError):Bundle(path)


if __name__=="__main__":unittest.main()
