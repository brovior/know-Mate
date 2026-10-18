"""Run the exact live121 handshake with fake COM and real evidence writers."""
import json,sys
from pathlib import Path
from types import SimpleNamespace
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parent))
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
import controller_support as target
from knowmate.tests.test_ppt_identity_controller import host_report
from knowmate.tests.test_ppt_identity_observation import Fake,ComFailure
RUN='1234567890abcdef1234567890abcdef'
@pytest.mark.parametrize('case',['normal','refused','bind_refused','prepare_error','host_unknown','cleanup','observe_error','controller_fatal','export_error','status_bad','noack','quit_error','quit_nonempty'])
def test_actual_handshake_records_and_stops(tmp_path,monkeypatch,case):
 calls=[];events=[]
 (tmp_path/'native-trace').mkdir()
 pins=dict(version='1.0.21.0',source_fingerprint='4d71bac5',installed_files={},helper_sha256='fixture')
 (tmp_path/'package-pins.json').write_text(json.dumps(pins))
 if case=='export_error':(tmp_path/'identity-observation.json').mkdir()
 monkeypatch.setattr(target,'_backend',lambda:Fake(ComFailure(-2147417848)) if case=='controller_fatal' else Fake())
 class Service:
  def BindExternalBootstrap(self,*args):calls.append('bind');return 'refused' if case=='bind_refused' else 'bound'
  def ObserveExternalIdentity(self,*args):
   calls.append('observe')
   if case=='observe_error':raise RuntimeError('synthetic')
   r=host_report()
   if case=='host_unknown':r['usable']=False
   if case=='cleanup':r['cleanupFailed']=True
   return json.dumps(r)
  def PrepareExternalBootstrap(self,*args):
   calls.append('prepare')
   if case=='prepare_error':raise RuntimeError('synthetic')
   return 'external_refused' if case=='refused' else 'ready'
  def ExternalBootstrapStatus(self):
   calls.append('status');return 'identity_result='+('Different' if case=='status_bad' else 'Same')+' identity=2/2 phase=Ready'
 class Ole:
  def GetIDsOfNames(self,name):calls.append('dispid');return 2021
  def Invoke(self,*args):
   calls.append('quit')
   if case=='quit_error':raise RuntimeError('synthetic')
   return 1 if case=='quit_nonempty' else None
 launch=SimpleNamespace(identity=123,nonce='fixture',signal=lambda i:calls.append(('signal',i)),ack=lambda timeout=0:bool(timeout) and case!='noack')
 def emit(event,**values):events.append(event)
 if case=='quit_error':
  with pytest.raises(RuntimeError):target.perform_and_collect(tmp_path,RUN,Service(),SimpleNamespace(_oleobj_=Ole()),launch,42,1,lambda:None,emit)
 else:
  outcome=target.perform_and_collect(tmp_path,RUN,Service(),SimpleNamespace(_oleobj_=Ole()),launch,42,1,lambda:None,emit)
  assert (outcome=='completed')==(case=='normal')
 result=json.loads((tmp_path/'identity-controller-result.json').read_text())['observation']
 assert len(result['host']['rows'])==4 and len(result['controller']['rows'])==9
 assert result['prepare_calls']==calls.count('prepare')
 if case in ('refused','bind_refused','prepare_error','host_unknown','cleanup','observe_error','controller_fatal','export_error'):
  assert 'status' not in calls and 'dispid' not in calls and 'quit' not in calls and not any(isinstance(c,tuple) for c in calls)
 assert events[-1]=='host_trace_collected'
 assert (tmp_path/'host-report.json').exists()

def test_pinned_runner_preflight_and_shared_entry():
 import ast
 text=Path(__file__).with_name('run.py').read_text();ast.parse(text)
 assert 'handoff_live_not_reviewed' in text
 assert 'verify(root, installed=True)' in text and "if '--live' not in sys.argv" in text
 assert "AEGIS_LIVE121_CHILD_AUTH" in text and 'perform_and_collect' in text
 assert 'prepare_observed' in Path(target.__file__).read_text()
 assert 'if probe_error is None:' in text and 'if failure is None:' in text
 assert 'or fatal_hresult' not in text
