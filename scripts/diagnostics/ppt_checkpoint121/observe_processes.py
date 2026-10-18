"""Read-only Toolhelp/process-handle observer; no Office/COM activation or termination."""
import argparse,ctypes,json,os,subprocess,sys,time
from pathlib import Path
from datetime import datetime,timezone
from ctypes import wintypes as w
k=ctypes.WinDLL('kernel32',use_last_error=True)
class Entry(ctypes.Structure):
 _fields_=[('size',w.DWORD),('usage',w.DWORD),('pid',w.DWORD),('heap',ctypes.c_size_t),('module',w.DWORD),('threads',w.DWORD),('parent',w.DWORD),('priority',w.LONG),('flags',w.DWORD),('name',w.WCHAR*260)]
k.CreateToolhelp32Snapshot.argtypes=[w.DWORD,w.DWORD];k.CreateToolhelp32Snapshot.restype=w.HANDLE
k.Process32FirstW.argtypes=[w.HANDLE,ctypes.POINTER(Entry)];k.Process32NextW.argtypes=k.Process32FirstW.argtypes
k.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];k.OpenProcess.restype=w.HANDLE
k.CloseHandle.argtypes=[w.HANDLE];k.WaitForSingleObject.argtypes=[w.HANDLE,w.DWORD]
k.GetExitCodeProcess.argtypes=[w.HANDLE,ctypes.POINTER(w.DWORD)]
k.GetProcessTimes.argtypes=[w.HANDLE,*([ctypes.POINTER(w.FILETIME)]*4)]
k.QueryFullProcessImageNameW.argtypes=[w.HANDLE,w.DWORD,w.LPWSTR,ctypes.POINTER(w.DWORD)]
def stamp():return datetime.now(timezone.utc).isoformat()
def snapshot():
 h=k.CreateToolhelp32Snapshot(2,0)
 if h==ctypes.c_void_p(-1).value:raise OSError(ctypes.get_last_error(),'snapshot')
 try:
  e=Entry();e.size=ctypes.sizeof(e);ok=k.Process32FirstW(h,ctypes.byref(e));rows=[]
  while ok:
   rows.append((e.pid,e.parent,e.name));ok=k.Process32NextW(h,ctypes.byref(e))
  return rows
 finally:k.CloseHandle(h)
def times(h):
 c,x,a,b=w.FILETIME(),w.FILETIME(),w.FILETIME(),w.FILETIME()
 if not k.GetProcessTimes(h,ctypes.byref(c),ctypes.byref(x),ctypes.byref(a),ctypes.byref(b)):return None,None
 return (c.dwHighDateTime<<32)|c.dwLowDateTime,(x.dwHighDateTime<<32)|x.dwLowDateTime
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--output',required=True);ap.add_argument('--duration',type=int,default=300);ap.add_argument('--selftest',action='store_true');args=ap.parse_args()
 output=Path(args.output);held={};seen=set();rows=[];child=None
 names={'aegisdesk.exe','powerpnt.exe'}
 if not args.selftest and any(n.lower() in names for _,_,n in snapshot()):raise RuntimeError('old_app_or_ppt_present_do_not_arm')
 def emit(row):
  row.update(run_id=os.environ['AEGIS_REVIEW_RUN'],qpc_ns=time.perf_counter_ns(),target_pid=row.get('pid') if row['event'] in ('process_first_seen','process_exit','observer_final_handle_state') else None,target_creation=row.get('creation_filetime'))
  rows.append(row)
  with output.open('a',encoding='utf-8') as f:f.write(json.dumps(row)+'\n')
  print(json.dumps(row),flush=True)
 with output.open('x',encoding='utf-8'):pass
 emit(dict(event='armed',utc=stamp(),pid=os.getpid(),interval_ms=50,selftest=args.selftest,limitations='Polling can miss processes shorter than interval; command line not exposed by these APIs. No ownership inference.'))
 try:
  if args.selftest:child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(1)'],creationflags=subprocess.CREATE_NO_WINDOW)
  deadline=time.monotonic()+(4 if args.selftest else args.duration)
  while time.monotonic()<deadline:
   for pid,parent,name in snapshot():
    if (pid!=child.pid if args.selftest else name.lower() not in names):continue
    h=k.OpenProcess(0x1000|0x100000,False,pid)
    if not h:
     if (pid,None) not in seen:seen.add((pid,None));emit(dict(event='open_unavailable',utc=stamp(),pid=pid,parent=parent,name=name,error=ctypes.get_last_error()))
     continue
    identity,_=times(h);key=(pid,identity)
    if key in seen:k.CloseHandle(h);continue
    seen.add(key);held[key]=h;buf=ctypes.create_unicode_buffer(32768);size=w.DWORD(len(buf));path=buf.value if not k.QueryFullProcessImageNameW(h,0,buf,ctypes.byref(size)) else buf.value
    emit(dict(event='process_first_seen',utc=stamp(),pid=pid,parent=parent,name=name,creation_filetime=identity,image=path))
   for key,h in list(held.items()):
    if k.WaitForSingleObject(h,0)==0:
     identity,exited=times(h);code=w.DWORD();got=bool(k.GetExitCodeProcess(h,ctypes.byref(code)))
     emit(dict(event='process_exit',utc=stamp(),pid=key[0],creation_filetime=identity,exit_filetime=exited,exit_code=code.value if got else None));k.CloseHandle(h);del held[key]
   if any(r['event']=='process_exit' for r in rows):break
   time.sleep(.05)
 finally:
  for key,h in held.items():
   identity,exited=times(h);code=w.DWORD();got=bool(k.GetExitCodeProcess(h,ctypes.byref(code)))
   emit(dict(event='observer_final_handle_state',utc=stamp(),pid=key[0],creation_filetime=identity,exit_filetime=exited,wait_result=k.WaitForSingleObject(h,0),exit_code=code.value if got else None))
   k.CloseHandle(h)
 if args.selftest:
  assert child.wait(timeout=5)==0
  assert any(r['event']=='process_first_seen' and r['creation_filetime'] for r in rows)
  assert any(r['event']=='process_exit' and r['exit_code']==0 for r in rows)
 emit(dict(event='observer_finished',utc=stamp(),selftest_pass=args.selftest))
if __name__=='__main__':main()
