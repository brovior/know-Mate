"""Historical 121 runner: preserved for review, not approved for a new live run."""
raise RuntimeError("handoff_live_not_reviewed; use offline tests only")
import sys,os,json,time,ctypes,subprocess,gc,uuid,traceback
from pathlib import Path
from datetime import datetime,timezone
root=Path(__file__).resolve().parent
run_id=os.environ.get('AEGIS_REVIEW_RUN') or uuid.uuid4().hex
os.environ['AEGIS_REVIEW_RUN']=run_id
trace_pid=None;trace_creation=None
from controller_support import perform_and_collect, collect_trace
pins=json.loads((root/'package-pins.json').read_text())
from verify_pins import verify
verify(root, installed=True)
sys.path.insert(0,pins['source_path'])
import observe_processes as obs
def emit(event,**data):
 row=dict(event=event,utc=datetime.now(timezone.utc).isoformat(),run_id=run_id,qpc_ns=time.perf_counter_ns(),target_pid=trace_pid,target_creation=trace_creation,**data)
 with (root/'control.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(row)+'\n')
 print(json.dumps(row),flush=True)
def traced_launch(path):
 from types import SimpleNamespace
 from ctypes import wintypes as w
 k=ctypes.WinDLL('kernel32',use_last_error=True)
 k.GetCurrentProcess.restype=w.HANDLE
 k.CreateEventW.argtypes=[ctypes.c_void_p,w.BOOL,w.BOOL,w.LPCWSTR];k.CreateEventW.restype=w.HANDLE
 k.DuplicateHandle.argtypes=[w.HANDLE,w.HANDLE,w.HANDLE,ctypes.POINTER(w.HANDLE),w.DWORD,w.BOOL,w.DWORD]
 k.CloseHandle.argtypes=[w.HANDLE];k.SetEvent.argtypes=[w.HANDLE];k.WaitForSingleObject.argtypes=[w.HANDLE,w.DWORD]
 compare=ctypes.WinDLL('kernelbase').CompareObjectHandles;compare.argtypes=[w.HANDLE,w.HANDLE];compare.restype=w.BOOL
 def dup(source,h,rights,inherit=False,options=0):
  value=w.HANDLE()
  if not k.DuplicateHandle(source,h,k.GetCurrentProcess(),ctypes.byref(value),rights,inherit,options):raise ctypes.WinError(ctypes.get_last_error())
  return value.value
 raw=[k.CreateEventW(None,True,False,None) for _ in range(3)]
 if not all(raw):raise RuntimeError('events_unavailable')
 inherited=[dup(k.GetCurrentProcess(),h,0x100002 if i==1 else 0x100000,True) for i,h in enumerate(raw+[k.GetCurrentProcess()])]
 token=uuid.uuid4().hex;env=os.environ.copy()
 for key in list(env):
  if key.startswith('AEGIS_PPT_DEFERRED_'):env.pop(key)
 env.update(AEGIS_PPT_EXTERNAL_DIAGNOSTIC='1',AEGIS_TEST_NONCE=token,AEGIS_PPT_LIFECYCLE_TRACE='1',AEGIS_PPT_TRACE_RUN=run_id,AEGIS_PPT_LIFECYCLE_DIR=str(root/'native-trace'))
 for name,h in zip(['START','ACK','COMPLETE','CONTROLLER'],inherited):env['AEGIS_TEST_CAP_'+name]=str(h)
 startup=subprocess.STARTUPINFO();startup.lpAttributeList={'handle_list':inherited}
 child=subprocess.Popen([str(path),'/EMBEDDING'],env=env,startupinfo=startup,close_fds=True,cwd=path.parent,creationflags=subprocess.CREATE_NO_WINDOW)
 observed=[]
 try:
  for name,h,original in zip(['START','ACK','COMPLETE','CONTROLLER'],inherited,raw+[k.GetCurrentProcess()]):
   copy=dup(int(child._handle),h,0,False,2)
   try:
    if not compare(copy,original):raise RuntimeError('child_capability_mismatch')
    observed.append(name)
   finally:k.CloseHandle(copy)
 finally:
  for h in inherited:k.CloseHandle(h)
 identity=obs.times(int(child._handle))[0]
 emit('capabilities_verified_in_actual_child',pid=child.pid,creation_filetime=identity,objects=observed)
 def signal(index):
  if not k.SetEvent(raw[index]):raise ctypes.WinError(ctypes.get_last_error())
 def ack(timeout=0):
  value=k.WaitForSingleObject(raw[1],timeout)
  if value not in (0,258):raise RuntimeError('ack_wait_failed')
  return value==0
 def close():
  for h in raw:k.CloseHandle(h)
 return SimpleNamespace(pid=child.pid,identity=identity,nonce=token,signal=signal,ack=ack,close=close,child=child)

def targets():return [x for x in obs.snapshot() if x[2].lower() in ('powerpnt.exe','aegisdesk.exe')]
def state(h):
 c,x=obs.times(h);return dict(creation_filetime=c,exit_filetime=x,wait_result=obs.k.WaitForSingleObject(h,0))
if '--child' in sys.argv:
 if os.environ.get('AEGIS_LIVE121_CHILD_AUTH')!=run_id:raise RuntimeError('parent_authorization_missing')
 import pythoncom,win32com.client,win32process
 pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
 from knowmate.secure import office_guard as guard
 from knowmate.secure.ppt_launch import LaunchedPowerPoint,trusted_powerpoint_path
 service=addin=addins=None;app=None;h=None;launched=None;stage='baseline';document=None
 def step(label,fn):
  global stage
  stage=label;emit('stage_begin',stage=stage);value=fn();emit('stage_return',stage=stage);return value
 def match():
  live=targets()
  if len(live)!=1 or live[0][0]!=launched.pid or live[0][2].lower()!='powerpnt.exe':raise RuntimeError('not_single_original_host')
  if obs.times(h)[0]!=launched.identity or obs.k.WaitForSingleObject(h,0)!=258:raise RuntimeError('original_identity_not_live')
 def zero():
  values={name:step(name+'.Count',lambda name=name:int(getattr(app,name).Count)) for name in ('Presentations','ProtectedViewWindows','SlideShowWindows')}
  emit('counts',**values)
  if any(values.values()):raise RuntimeError('nonempty_no_quit')
 try:
  if targets():raise RuntimeError('baseline_not_empty_no_activation')
  emit('activation_begin',caller_pid=os.getpid())
  launched=step('direct_launch',lambda:traced_launch(trusted_powerpoint_path(64)))
  pid=launched.pid;trace_pid=pid;trace_creation=launched.identity;h=obs.k.OpenProcess(0x1000|0x100000,False,pid)
  if not h:raise RuntimeError('identity_handle_unavailable_no_quit')
  emit('direct_child',pid=pid,**state(h));match()
  for _ in range(100):
   rows=[json.loads(line) for line in (root/'process-events.jsonl').read_text().splitlines()]
   if any(row['event']=='process_first_seen' and row['pid']==pid and row['creation_filetime']==launched.identity for row in rows):break
   time.sleep(.1)
  else:raise RuntimeError('independent_observer_did_not_hold_identity')
  emit('independent_observer_holds_original',pid=pid,creation_filetime=launched.identity)

  stage='ROT_attach_only'
  for _ in range(50):
   match()
   try:app=win32com.client.GetActiveObject('PowerPoint.Application');break
   except pythoncom.com_error as error:
    if error.hresult!=-2147221021:raise
    time.sleep(.2)
  if app is None:raise RuntimeError('ROT_unavailable_no_activation_fallback')
  emit('ROT_attached');match();zero()
  original=step('caption_read',lambda:app.Caption);marker='AegisDesk-basic-quit-'+uuid.uuid4().hex
  document=step('blank_create',lambda:app.Presentations.Add(-1))
  caption_changed=False;probe_error=None
  try:
   caption_changed=True;step('caption_set',lambda:setattr(app,'Caption',marker))
   if step('caption_verify',lambda:app.Caption)!=marker:raise RuntimeError('marker_changed')
   hwnd=step('unique_marker_window',lambda:guard._hwnd_for_caption_marker(marker))
   if hwnd is None or guard._pid_from_hwnd(hwnd)!=pid:raise RuntimeError('marker_pid_mismatch')
   match();emit('owned_marker_match',pid=pid,hwnd=hwnd,**state(h))
  except Exception as e:probe_error=e;raise
  finally:
   from knowmate.secure.com_fault import fatal_hresult
   if probe_error is None:
    if caption_changed:step('caption_restore',lambda:setattr(app,'Caption',original))
    step('owned_blank_close',lambda:document.Close());document=None
  def read_synthetic():
   from knowmate.secure.ppt_installation import MachineReadGuard
   from knowmate.secure.ppt_addin_reader import read_slides
   from knowmate.secure.com_fault import fatal_hresult
   import hashlib
   source=Path(pins['fixture_path'])
   expected=pins['fixture_sha256']
   digest=hashlib.sha256(source.read_bytes()).hexdigest()
   if digest!=expected:raise RuntimeError('synthetic_hash_mismatch')
   emit('read_helper_enter',source_sha256=digest)
   prs=service=addin=addins=None;failure=None
   with MachineReadGuard():
    try:
     match()
     addins=app.COMAddIns;addin=addins.Item('AegisDesk.PptReader')
     if not bool(addin.Connect):raise RuntimeError('addin_disabled')
     service=addin.Object
     version=service.ReaderVersion();protocol=service.ProtocolVersion()
     emit('service_verified',version=version,protocol=protocol)
     if version!=pins['version'] or protocol!=3:raise RuntimeError('service_incompatible')
     prs=app.Presentations.Open(str(source),ReadOnly=True,WithWindow=False)
     if prs is None:raise RuntimeError('document_missing')
     text=read_slides(prs,str(source),service,match)
     emit('read_result',characters=len(text),output_sha256=hashlib.sha256(text.encode('utf-8')).hexdigest())
     if len(text)!=65:raise RuntimeError('unexpected_character_count')
     if hashlib.sha256(text.encode('utf-8')).hexdigest()!='1d766c6794d7e50eba5a37b5850debc6f5309db02458793a941e3e614f9dd10e':raise RuntimeError('unexpected_output_hash')
    except Exception as error:
     failure=error;raise
    finally:
     if failure is None:
      if prs is not None:prs.Close();emit('synthetic_document_closed')
     prs=service=addin=addins=None
     emit('helper_com_locals_dropped')
   if hashlib.sha256(source.read_bytes()).hexdigest()!=expected:raise RuntimeError('synthetic_changed')
   return text
  match();zero()
  extracted=step('read_synthetic_helper',read_synthetic)
  emit('helper_scope_returned_plain_data',characters=len(extracted))
  extracted=None;gc.collect();emit('helper_scope_collected')
  if guard._pid_from_hwnd(hwnd)!=pid:raise RuntimeError('owned_window_changed_no_quit')
  zero();match()
  from knowmate.secure.ppt_installation import MachineReadGuard
  service=addin=addins=None
  with MachineReadGuard():
   match()
   addins=app.COMAddIns;addin=addins.Item('AegisDesk.PptReader')
   if not bool(addin.Connect):raise RuntimeError('addin_disabled')
   service=addin.Object
   if service is None:raise RuntimeError('service_missing')
   version=service.ReaderVersion();protocol=service.ProtocolVersion()
   if version!=pins['version'] or protocol!=3:raise RuntimeError('service_incompatible')
   emit('held_service_verified',version=version,protocol=protocol)
   addin=addins=None
   zero();match()
   if guard._pid_from_hwnd(hwnd)!=pid:raise RuntimeError('owned_window_changed')
   gc.collect();emit('temporary_refs_dropped_service_and_app_retained',pid=pid,**state(h))
   owner_tid,window_pid=win32process.GetWindowThreadProcessId(hwnd)
   if window_pid!=pid:raise RuntimeError('window_owner_mismatch')
   emit('binding_input_verified',pid=pid,creation_filetime=launched.identity,hwnd=hwnd,native_window_thread=owner_tid)
   stage='bootstrap_handshake'
   outcome=perform_and_collect(root,run_id,service,app,launched,pid,hwnd,match,emit)
   emit('bootstrap_handshake_outcome',outcome=outcome)
   if outcome!='completed':raise RuntimeError('handshake_'+outcome)
   emit('service_retained_observation_begin',seconds=30)
   for _ in range(300):
    if obs.k.WaitForSingleObject(h,0)==0:break
    time.sleep(.1)
   emit('service_retained_observation_end',pid=pid,**state(h))
  emit('cleanup_service_release_begin',pid=pid,**state(h))
  service=None;gc.collect();emit('cleanup_service_release_return',pid=pid,**state(h))
  emit('before_wrapper_drop',pid=pid,**state(h))
  app=None;gc.collect();emit('after_wrapper_drop',pid=pid,**state(h))
  time.sleep(3);emit('before_caller_exit',pid=pid,**state(h))
 except Exception as e:emit('blocked_or_failed',stage=stage,error_type=type(e).__name__,hresult=getattr(e,'hresult',None),excepinfo_hresult=e.excepinfo[5] if getattr(e,'excepinfo',None) else None)
 finally:
  if trace_pid is not None:
   report=collect_trace(root,run_id,trace_pid,trace_creation)
   emit('host_trace_collected',host_trace=report['host_trace'],evidence_complete=report['evidence_complete'])
  service=addin=addins=None;app=None;gc.collect()
  if h:obs.k.CloseHandle(h)
  if launched:launched.close()
  pythoncom.CoUninitialize();emit('caller_finishing')
else:
 if '--live' not in sys.argv:raise RuntimeError('explicit_live_argument_required_after_separate_install_authorization')
 if (root/'control.jsonl').exists():raise RuntimeError('single_run_only')
 (root/'native-trace').mkdir(exist_ok=False)
 if ctypes.windll.shell32.IsUserAnAdmin():raise RuntimeError('admin_not_allowed')
 if targets():raise RuntimeError('baseline_not_empty')
 import hashlib
 installed=Path('C:/Program Files/AegisDesk/PptReader')/pins['version']
 for name,expected in pins['installed_files'].items():
  if hashlib.sha256((installed/name).read_bytes()).hexdigest()!=expected:raise RuntimeError('installed_hash_changed')
 source=Path(pins['fixture_path'])
 if hashlib.sha256(source.read_bytes()).hexdigest()!=pins['fixture_sha256']:raise RuntimeError('synthetic_hash_mismatch_before_launch')
 emit('baseline_empty',installed_addin_unchanged=True)
 observer=subprocess.Popen([sys.executable,'-X','utf8',str(root/'observe_processes.py'),'--output',str(root/'process-events.jsonl'),'--duration','180'],stdout=(root/'observer.stdout.log').open('wb'),stderr=(root/'observer.stderr.log').open('wb'),creationflags=subprocess.CREATE_NO_WINDOW)
 for _ in range(100):
  if observer.poll() is not None:raise RuntimeError('observer_failed')
  if (root/'process-events.jsonl').exists() and '"armed"' in (root/'process-events.jsonl').read_text():break
  time.sleep(.1)
 else:raise RuntimeError('observer_not_armed')
 os.environ['AEGIS_LIVE121_CHILD_AUTH']=run_id
 child=subprocess.Popen([sys.executable,'-X','utf8',str(Path(__file__).resolve()),'--child'],stdout=(root/'caller.stdout.log').open('wb'),stderr=(root/'caller.stderr.log').open('wb'),creationflags=subprocess.CREATE_NO_WINDOW)
 emit('caller_started',pid=child.pid)
 try:child.wait(timeout=180)
 except subprocess.TimeoutExpired:emit('caller_timeout_no_kill_no_retry',pid=child.pid);sys.exit(2)
 emit('caller_exited',exit_code=child.returncode)
 time.sleep(3);emit('after_caller_exit_os_snapshot',targets=targets())

 if (root/'host-report.json').exists():
  previous=json.loads((root/'host-report.json').read_text())
  collect_trace(root,run_id,previous['pid'],previous['creation'],timeout=0)
