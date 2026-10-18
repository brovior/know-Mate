using System;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Threading;
using AegisDesk.PptReader;
class DeferredConnectHarness {
 [UnmanagedFunctionPointer(CallingConvention.StdCall)]delegate int ProtocolCall(IntPtr self,out int value);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)]delegate int StringCall(IntPtr self,out IntPtr value);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)]delegate int ExtractCall(IntPtr self,[MarshalAs(UnmanagedType.BStr)]string path,int slide,out IntPtr value);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)]delegate int NamesCall(IntPtr self,ref Guid iid,[MarshalAs(UnmanagedType.LPArray,ArraySubType=UnmanagedType.LPWStr)]string[] names,uint count,uint locale,out int id);
 static T Slot<T>(IntPtr face,int slot){return (T)(object)Marshal.GetDelegateForFunctionPointer(Marshal.ReadIntPtr(Marshal.ReadIntPtr(face),slot*IntPtr.Size),typeof(T));}
 static void Interfaces(){
  var connect=new Connect();Array custom=null;connect.OnConnection(new Host(),1,new Addin(),ref custom);
  IntPtr unknown=Marshal.GetIUnknownForObject(connect);
  try{
   foreach(Type type in new[]{typeof(IPptReader),typeof(IPptReaderLifecycle)}){
    Guid iid=type.GUID;IntPtr face;Marshal.ThrowExceptionForHR(Marshal.QueryInterface(unknown,ref iid,out face));
    try{int protocol;Marshal.ThrowExceptionForHR(Slot<ProtocolCall>(face,7)(face,out protocol));Check(protocol==3,"dual_protocol");
     IntPtr text;Marshal.ThrowExceptionForHR(Slot<StringCall>(face,9)(face,out text));try{Check(Marshal.PtrToStringBSTR(text)!=null,"dual_version");}finally{Marshal.FreeBSTR(text);}
     Marshal.ThrowExceptionForHR(Slot<ExtractCall>(face,8)(face,"synthetic.ppt",1,out text));try{Check(Marshal.PtrToStringBSTR(text)=="","dual_read");}finally{Marshal.FreeBSTR(text);}
     if(type==typeof(IPptReaderLifecycle)){typeof(Connect).GetField("reading",BindingFlags.NonPublic|BindingFlags.Instance).SetValue(connect,true);Marshal.ThrowExceptionForHR(Slot<StringCall>(face,10)(face,out text));try{Check(Marshal.PtrToStringBSTR(text)=="protected","dual_quit_protected");}finally{Marshal.FreeBSTR(text);}}
    }finally{Marshal.Release(face);}
    Console.WriteLine("PASS native_dual_access "+iid);
   }
   IntPtr dispatch=Marshal.GetIDispatchForObject(connect);try{
    string[] names={"ProtocolVersion","ExtractSlide","ReaderVersion","TryQuitIfIdle","ArmDeferredQuit"};
    for(int i=0;i<names.Length;i++){Guid empty=Guid.Empty;int id;Marshal.ThrowExceptionForHR(Slot<NamesCall>(dispatch,5)(dispatch,ref empty,new[]{names[i]},1,0,out id));Check(id==i+1,"default_dispatch_id");}
   }finally{Marshal.Release(dispatch);}
  }finally{Marshal.Release(unknown);connect.OnDisconnection(1,ref custom);}
  Console.WriteLine("PASS default_dispatch_five_names");
 }
 [DllImport("kernel32.dll",SetLastError=true)]static extern IntPtr CreateEvent(IntPtr sa,bool manual,bool initial,string name);
 [DllImport("kernel32.dll")]static extern IntPtr GetCurrentProcess();
 [DllImport("kernel32.dll",SetLastError=true)]static extern bool DuplicateHandle(IntPtr a,IntPtr h,IntPtr b,out IntPtr result,uint rights,bool inherit,uint options);
 [DllImport("kernel32.dll")]static extern bool SetEvent(IntPtr h);
 [DllImport("kernel32.dll")]static extern uint WaitForSingleObject(IntPtr h,uint ms);
 [DllImport("kernel32.dll")]static extern bool CloseHandle(IntPtr h);
 [DllImport("kernelbase.dll",SetLastError=true)]static extern bool CompareObjectHandles(IntPtr a,IntPtr b);
 [DllImport("ntdll.dll")]static extern int NtQueryObject(IntPtr h,int kind,IntPtr data,int length,out int needed);
 static void Check(bool ok,string name){if(!ok)throw new Exception(name);}
 static IntPtr Sync(IntPtr handle){IntPtr result;Check(DuplicateHandle(GetCurrentProcess(),handle,GetCurrentProcess(),out result,0x100000,true,0),"duplicate");return result;}
 static void Env(string name,string value){Environment.SetEnvironmentVariable("AEGIS_PPT_DEFERRED_"+name,value);}
 static object Field(object value,string name){return value.GetType().GetField(name,BindingFlags.NonPublic|BindingFlags.Instance).GetValue(value);}
 static int HandleCount(IntPtr h){IntPtr data=Marshal.AllocHGlobal(56);try{int needed;Check(NtQueryObject(h,0,data,56,out needed)==0,"audit_query");return Marshal.ReadInt32(data,8);}finally{Marshal.FreeHGlobal(data);}}
 // Observe the actual product close delegate; independent handles retain the original objects.
 sealed class CloseAudit : IDisposable {
  readonly IntPtr e,p,eo,po;readonly PptDeferredResources resources;int ea,pa,es,ps,eventDrop;
  internal CloseAudit(Connect connect,IntPtr eventHandle,IntPtr processHandle){
   e=eventHandle;p=processHandle;eo=Sync(e);po=Sync(p);
   var gate=(PptDeferredGate)Field(Field(connect,"deferred"),"gate");
   resources=(PptDeferredResources)((Action)Field(gate,"cleanup")).Target;
   var release=(Action<IntPtr>)Field(resources,"close");
   typeof(PptDeferredResources).GetField("close",BindingFlags.NonPublic|BindingFlags.Instance).SetValue(resources,new Action<IntPtr>(delegate(IntPtr h){
    bool isEvent=h==e;Check(isEvent||h==p,"audit_unowned_close");
    if(isEvent)ea++;else pa++;
    Check(CompareObjectHandles(h,isEvent?eo:po),"audit_original_identity");
    int count=isEvent?HandleCount(eo):0;
    release(h); // The unchanged product CloseHandle wrapper throws on failure.
    if(isEvent){es++;eventDrop=count-HandleCount(eo);}else ps++;
    Console.WriteLine("CLOSE_AUDIT resource="+(isEvent?"event":"controller")+" success=true");
   }));
  }
  internal void Verify(){
   Check(ea==1&&pa==1&&es==1&&ps==1&&eventDrop==1&&resources.Empty,"audit_exact_cleanup");
   Check(WaitForSingleObject(po,0)==258,"audit_original_process_alive");
   // Never query a retired handle value: Windows may reject it or reuse it for another object.
   Console.WriteLine("CLOSE_AUDIT exact_once=true event_handle_count_drop="+eventDrop);
  }
  internal void SignalObserved(){Check(WaitForSingleObject(eo,0)==0,"audit_original_event_alive");}
  internal void Retained(){Check(CompareObjectHandles(e,eo)&&CompareObjectHandles(p,po),"prepare_capabilities_retained");}
  public void Dispose(){CloseHandle(eo);CloseHandle(po);}
 }
 static void Sentinel() {
  IntPtr raw=CreateEvent(IntPtr.Zero,true,false,null),eventSync=Sync(raw),processSync=Sync(GetCurrentProcess());
  Env("DIAGNOSTIC","1");Env("NONCE",Guid.NewGuid().ToString("N"));Env("CONTROLLER",processSync.ToInt64().ToString());
  try {
   Env("EVENT",raw.ToInt64().ToString());
   Check(PptDeferredDiagnostic.Capture(()=>"unexpected",()=>true,delegate{})==null,"full_access_sentinel_rejected");
   Check(SetEvent(raw)&&WaitForSingleObject(eventSync,0)==0,"sentinel_not_closed");
   Env("EVENT",eventSync.ToInt64().ToString());Env("CONTROLLER",raw.ToInt64().ToString());
   Check(!PptDeferredNative.CanAdopt(eventSync,raw),"partial_invalid_pair");
   Check(PptDeferredDiagnostic.Capture(()=>"unexpected",()=>true,delegate{})==null,"wrong_type_sentinel_rejected");
   Check(WaitForSingleObject(eventSync,0)==0&&SetEvent(raw),"partial_adoption_did_not_close");
  }finally{CloseHandle(eventSync);CloseHandle(processSync);CloseHandle(raw);Env("DIAGNOSTIC",null);Env("EVENT",null);Env("CONTROLLER",null);Env("NONCE",null);}
  Console.WriteLine("PASS sentinel_and_partial_adoption_preserved");
 }
 static void Existing(string mode) {
  var host=new Host();var addin=new Addin();var connect=new Connect();Array custom=null;connect.OnConnection(host,1,addin,ref custom);
  Check(connect.ArmDeferredQuit(1,"1","invalid")=="not_supported","ordinary_launch_denied");
  var field=typeof(Connect).GetField("shutdown",BindingFlags.Instance|BindingFlags.NonPublic);
  var existing=(PptShutdownGuard)field.GetValue(connect);var point=new Point{Calls=host.Calls};
  // Offline dependencies only; retain the very same OnConnection-created guard.
  typeof(PptShutdownGuard).GetField("resolvePoint",BindingFlags.Instance|BindingFlags.NonPublic).SetValue(existing,new Func<object,IConnectionPoint>(delegate{return point;}));
  typeof(PptShutdownGuard).GetField("createSink",BindingFlags.Instance|BindingFlags.NonPublic).SetValue(existing,new Func<PptShutdownState,object>(delegate{return new object();}));
  MethodInfo invoke=typeof(Connect).GetMethod("InvokeExistingShutdown",BindingFlags.Instance|BindingFlags.NonPublic);
  if(mode=="broken")host.OnQuit=delegate{throw new COMException("fixture",unchecked((int)0x80010108));};
  if(mode=="reading")typeof(Connect).GetField("reading",BindingFlags.Instance|BindingFlags.NonPublic).SetValue(connect,true);
  bool signal=false;string first=null;var gate=new PptDeferredGate(()=>true,()=>signal,()=>true,()=>true,delegate{first=(string)invoke.Invoke(connect,null);return first;},delegate{},delegate{});
  gate.Enter(true);Check(gate.Arm()=="armed","arm");gate.Leave();signal=true;gate.Tick();
  Check(Object.ReferenceEquals(existing,field.GetValue(connect)),"existing_guard_identity");
  int count=host.Calls.FindAll(x=>x=="quit").Count;
  Check(count==(mode=="reading"?0:1),"first_quit_count");
  if(mode=="reading")Check(first=="protected","execution_time_reading");
  else {
   int before=host.Calls.Count;string second=(string)invoke.Invoke(connect,null);
   Check(second==(mode=="broken"?"not_supported":"protected"),"sticky_state");
   Check(host.Calls.Count==before,"no_retry_rpc");
  }
  connect.OnDisconnection(1,ref custom);Console.WriteLine("PASS existing_guard_"+mode);
 }
 static void Adopted(string mode) {
  IntPtr raw=CreateEvent(IntPtr.Zero,true,false,null),e=Sync(raw),p=Sync(GetCurrentProcess());
  string token=Guid.NewGuid().ToString("N");Env("DIAGNOSTIC","1");Env("NONCE",token);Env("EVENT",e.ToInt64().ToString());Env("CONTROLLER",p.ToInt64().ToString());
  var connect=new Connect();var host=new Host();Array custom=null;connect.OnConnection(host,1,new Addin(),ref custom);
  var audit=new CloseAudit(connect,e,p);
  var guard=(PptShutdownGuard)typeof(Connect).GetField("shutdown",BindingFlags.NonPublic|BindingFlags.Instance).GetValue(connect);
  var point=new Point{Calls=host.Calls};
  typeof(PptShutdownGuard).GetField("resolvePoint",BindingFlags.NonPublic|BindingFlags.Instance).SetValue(guard,new Func<object,IConnectionPoint>(delegate{return point;}));
  typeof(PptShutdownGuard).GetField("createSink",BindingFlags.NonPublic|BindingFlags.Instance).SetValue(guard,new Func<PptShutdownState,object>(delegate{return new object();}));
  var invoke=typeof(Connect).GetMethod("InvokeExistingShutdown",BindingFlags.NonPublic|BindingFlags.Instance);
  if(mode=="disconnect")host.OnQuit=delegate{connect.OnBeginShutdown(ref custom);};
  if(mode=="broken")host.OnQuit=delegate{throw new COMException("fixture",unchecked((int)0x80010108));};
  if(mode=="attempted"||mode=="broken"){try{invoke.Invoke(connect,null);}catch(TargetInvocationException){Check(mode=="broken","unexpected_error");}}
  if(mode=="reading")typeof(Connect).GetField("reading",BindingFlags.NonPublic|BindingFlags.Instance).SetValue(connect,true);
  int before=host.Calls.FindAll(x=>x=="quit").Count;
  if(mode=="prepare") {
   Check(connect.ProtocolVersion()==3&&connect.ReaderVersion()!=null,"prepare_versions");
   // Real Connect service boundary; extraction body is the offline harness stub.
   Check(connect.ExtractSlide("synthetic.ppt",1)=="","prepare_read");
   Check(host.Presentations.Count==0&&host.ProtectedViewWindows.Count==0&&host.SlideShowWindows.Count==0,"prepare_zero3");
   audit.Retained();
  }
  if(mode=="new_disconnect") {
   connect.OnBeginShutdown(ref custom);
   audit.Verify();Check(SetEvent(raw),"new_disconnect_original_preserved");audit.SignalObserved();audit.Dispose();
   CloseHandle(raw);Console.WriteLine("PASS adopted_existing_"+mode);return;
  }
  Check(connect.ArmDeferredQuit(System.Diagnostics.Process.GetCurrentProcess().Id,PptDeferredNative.Creation().ToString(),token)=="armed","adopted_arm");
  if(mode=="pending_protocol")connect.ProtocolVersion();
  if(mode=="pending_version")connect.ReaderVersion();
  if(mode=="pending_read")connect.ExtractSlide("synthetic.ppt",1);
  Check(SetEvent(raw),"controller_signal");
  var watch=System.Diagnostics.Stopwatch.StartNew();while(watch.ElapsedMilliseconds<100){System.Windows.Forms.Application.DoEvents();Thread.Sleep(1);}
  int after=host.Calls.FindAll(x=>x=="quit").Count;Check(after==before+(mode=="normal"||mode=="disconnect"||mode=="prepare"?1:0),"adopted_sticky_guard");
  audit.Verify();
  Check(SetEvent(raw),"controller_original_preserved");
  audit.SignalObserved();
  connect.OnDisconnection(1,ref custom);
  connect.OnConnection(host,1,new Addin(),ref custom);
  Check(connect.ArmDeferredQuit(System.Diagnostics.Process.GetCurrentProcess().Id,PptDeferredNative.Creation().ToString(),token)=="not_supported","reconnect_no_readoption");
  audit.Verify();audit.Dispose();CloseHandle(raw);Console.WriteLine("PASS adopted_existing_"+mode);
 }
 [STAThread]static int Main(string[] args){try{
  if(args.Length!=0){Adopted(args[0]);return 0;}
  Env("DIAGNOSTIC",null);Sentinel();Interfaces();
  foreach(string mode in new[]{"attempted","broken","reading"})Existing(mode);
  Check(typeof(IPptReaderLifecycle).GUID==new Guid("9A20F4A6-6364-4BA7-930B-3E8857BEAF60")&&typeof(IPptReaderLifecycle).GetMethods().Length==4,"legacy_dual_unchanged");
  var face=(ComDefaultInterfaceAttribute)Attribute.GetCustomAttribute(typeof(Connect),typeof(ComDefaultInterfaceAttribute));
  Check(face.Value==typeof(IPptReaderDeferredDiagnostic)&&typeof(IPptReaderDeferredDiagnostic).GetMethods().Length==5,"diagnostic_dispatch_facade");
  Console.WriteLine("CONNECT_DEFERRED_RESULT PASS");return 0;
 }catch(Exception e){Console.WriteLine(e);return 1;}}
}
