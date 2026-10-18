from pathlib import Path
import ctypes as c
from ctypes import wintypes as w
import os,subprocess,time,json,uuid,gc,hashlib,sys
import pythoncom,win32com.client
sys.path.insert(0,str(Path(__file__).resolve().parents[3]/'scripts/ppt_reader'))
from ppt_trace_report import read_terminal_trace
b=Path(sys.argv[1])
k=c.WinDLL('kernel32',use_last_error=True);k.GetCurrentProcess.restype=w.HANDLE
k.CreateEventW.argtypes=[c.c_void_p,w.BOOL,w.BOOL,w.LPCWSTR];k.CreateEventW.restype=w.HANDLE
k.DuplicateHandle.argtypes=[w.HANDLE,w.HANDLE,w.HANDLE,c.POINTER(w.HANDLE),w.DWORD,w.BOOL,w.DWORD]
k.SetEvent.argtypes=[w.HANDLE];k.WaitForSingleObject.argtypes=[w.HANDLE,w.DWORD];k.CloseHandle.argtypes=[w.HANDLE]
compare=c.WinDLL('kernelbase').CompareObjectHandles;compare.argtypes=[w.HANDLE,w.HANDLE];compare.restype=w.BOOL
def dup(source,h,rights,inherit=False,options=0):
 out=w.HANDLE();assert k.DuplicateHandle(source,h,k.GetCurrentProcess(),c.byref(out),rights,inherit,options);return out.value
rows=[]
for arch in [sys.argv[2]]:
 for mode in ['external_normal','normal','normal_fatal','deferred','reader_fence_failure','reading','identity_mismatch','disconnect_ready','disconnect_bound','invalid_capability','wrongnonce','wrongpid','wrongcreation','wrongnonce_bound','invalid_window','ordinary']:
  originals=[k.CreateEventW(None,True,False,None) for _ in range(3)]
  inherited=[dup(k.GetCurrentProcess(),h,0x100002 if i==1 and mode!='invalid_capability' else 0x100000,True) for i,h in enumerate(originals+[k.GetCurrentProcess()])]
  env=os.environ.copy()
  for key in list(env):
   if key.startswith('AEGIS_PPT_') or key.startswith('AEGIS_TEST_'):env.pop(key)
  nonce=uuid.uuid4().hex;env['AEGIS_TEST_NONCE']=nonce
  run_id=uuid.uuid4().hex;trace_dir=b/arch/(mode+'-trace');trace_dir.mkdir()
  env.update(AEGIS_PPT_LIFECYCLE_TRACE='1',AEGIS_PPT_TRACE_RUN=run_id,AEGIS_PPT_LIFECYCLE_DIR=str(trace_dir))
  if mode!='ordinary':env['AEGIS_PPT_EXTERNAL_DIAGNOSTIC']='1'
  if mode=='reader_fence_failure':env['AEGIS_PPT_EXTERNAL_READER_V1']='BLOCKED'
  for key,h in zip(['START','ACK','COMPLETE','CONTROLLER'],inherited):env['AEGIS_TEST_CAP_'+key]=str(h)
  si=subprocess.STARTUPINFO();si.lpAttributeList={'handle_list':inherited};token='AegisBootstrap-'+uuid.uuid4().hex
  child=subprocess.Popen([str(b/arch/'Fixture.exe'),token,str(b/arch/'AegisDesk.PptReader.Events.dll'),mode],env=env,startupinfo=si,close_fds=True,creationflags=subprocess.CREATE_NO_WINDOW,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
  control=reader=host=None;prefix='';record={};poisoned=False
  try:
   prefix=child.stdout.readline();assert prefix.startswith('LEGACY_DUAL PASS'),prefix
   line=child.stdout.readline();prefix+=line;assert line.startswith('READY '),line
   pid,creation,hwnd,tid=line[6:].strip().split('|')
   def event(stage,outcome,**values):
    row=dict(run_id=run_id,pid=int(pid),creation=int(creation),qpc_ns=time.perf_counter_ns(),stage=stage,outcome=outcome,**values)
    with (trace_dir/'controller.jsonl').open('a',encoding='utf-8') as stream:stream.write(json.dumps(row)+'\n')
   event('target','verified')
   for h,original in zip(inherited,originals+[k.GetCurrentProcess()]):
    copy=dup(int(child._handle),h,0,False,2)
    try:assert compare(copy,original)
    finally:k.CloseHandle(copy)
   moniker=pythoncom.CreateItemMoniker('!',token);rot=pythoncom.GetRunningObjectTable()
   control=win32com.client.Dispatch(rot.GetObject(moniker).QueryInterface(pythoncom.IID_IDispatch))
   reader=control.Reader();host=control.Host(False);assert reader.ProtocolVersion()==3
   expected=4
   if mode=='ordinary':
    result=reader.TryQuitIfIdle();expected=0
   else:
    bound=reader.BindExternalBootstrap(int(pid)+1 if mode=='wrongpid' else int(pid),'0' if mode=='wrongcreation' else creation,'0'*32 if mode=='wrongnonce' else nonce,0 if mode=='invalid_window' else int(hwnd));record['bind']=bound;event('bind','returned',result=bound)
    if mode in ['wrongnonce','wrongpid','wrongcreation','invalid_capability']:
     assert bound in ['binding_rejected','capability_invalid'];expected=0;result=bound
    else:
     assert bound=='bound',bound
     if mode=='normal_fatal':
      try:result=reader.TryQuitIfIdle()
      except pythoncom.com_error as error:
       assert error.excepinfo[5]==-2147417848
       result='COM_ERROR';poisoned=True;record['com_error_hresult']=error.excepinfo[5]
      else:raise AssertionError('fatal HRESULT did not propagate')
     elif mode=='normal':result=reader.TryQuitIfIdle()
     elif mode=='deferred':result=control.Deferred()
     elif mode=='disconnect_bound':control.Reconnect();result='disconnected'
     else:
      if mode=='reading':control.Busy(True)
      if mode=='identity_mismatch':host=control.Host(True)
      result=reader.PrepareExternalBootstrap('0'*32 if mode=='wrongnonce_bound' else nonce,host);event('prepare','returned',result=result)
      if mode in ['external_normal','disconnect_ready']:
       assert result=='ready',result
       assert k.WaitForSingleObject(originals[1],0)==258
       if mode=='disconnect_ready':control.Reconnect()
       else:
        event('START','entered');assert k.SetEvent(originals[0]);event('START','success');assert k.WaitForSingleObject(originals[1],3000)==0;event('ACK','observed')
        event('Quit','entered');host.Quit();event('Quit','normal_return');assert k.SetEvent(originals[2]);event('Completion','success')
        end=time.monotonic()+3
        while 'phase=Terminal' not in reader.ExternalBootstrapStatus() and time.monotonic()<end:time.sleep(.01)
        assert 'phase=Terminal' in reader.ExternalBootstrapStatus()
      else:assert result!='ready',result
   status='not_queried_after_fatal' if poisoned else reader.ExternalBootstrapStatus();record.update(result=result,status=status)
   if mode in ['external_normal','identity_mismatch']:assert 'identity=2/2' in status,status
   if not poisoned:assert 'close='+str(expected)+' ' in status,status
   if not poisoned:assert 'closed='+str(expected)+' ' in status,status
   if not poisoned and mode!='ordinary':
    deadline=time.monotonic()+3
    while True:
     trace=read_terminal_trace(trace_dir,run_id,int(pid),int(creation))
     if trace['evidence_complete'] or time.monotonic()>=deadline:break
     time.sleep(.01)
    assert trace['evidence_complete'],trace
    events=trace['frames'][-1]['records']
    assert any(e['stage']=='Bind' and e['outcome']=='Entered' for e in events),events
    if mode=='identity_mismatch':assert any(e['stage']=='Identity' and e['outcome']=='Refused' for e in events),events
    if mode=='external_normal':
     assert any(e['stage']=='Ack' and e['outcome']=='Success' for e in events),events
     assert any(e['stage']=='Completion' and e['outcome']=='Success' for e in events),events
     assert len(events)<=128 and trace['frames'][-1]['dropped']==0,trace
     for stage in ['Resolve','Sink','Advise','Unadvise','Release','TimerCleanup','CapabilityClose']:
      assert any(e['stage']==stage and e['outcome']=='Success' for e in events),events
     assert events[-1]['stage']=='Terminal' and events[-1]['outcome']=='Success',events
    (trace_dir/'report.json').write_text(json.dumps(trace,indent=2))
   if not poisoned:control.Stop()
   rest=child.communicate(timeout=25)[0];(b/arch/(mode+'.log')).write_text(prefix+rest+'\n'+json.dumps(record))
   assert child.returncode==0,rest
   event('OSexit','observed',exit_code=child.returncode)
   assert 'close='+str(expected)+' ' in rest,rest
   if expected==0:assert 'NO_ADOPTION_PRESERVED True' in rest,rest
   try:rot.GetObject(moniker)
   except pythoncom.com_error:pass
   else:raise AssertionError('ROT entry survived revoke')
   for h in originals:assert k.SetEvent(h)
   rows.append(dict(arch=arch,mode=mode,verdict='PASS',**record));print(arch,mode,'PASS',flush=True)
  finally:
   if child.poll() is None:
    if control is not None and not poisoned:control.Stop()
    rest=child.communicate(timeout=25)[0];(b/arch/(mode+'-failure.log')).write_text(prefix+rest+'\n'+json.dumps(record))
   control=reader=host=None;gc.collect()
   for h in inherited+originals:k.CloseHandle(h)
(b/'native-results.json').write_text(json.dumps(rows,indent=2))
