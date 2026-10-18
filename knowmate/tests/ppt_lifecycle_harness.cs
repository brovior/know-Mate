using System;
using System.Collections.Generic;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Threading;
using AegisDesk.PptReader;
public class Counts { public int Count {get;set;} }
public class Host {
 public List<string> Calls=new List<string>(); public Action OnQuit;
 public int DisplayAlerts { get {Calls.Add("alerts_get");return 2;} set {Calls.Add("alerts_set");} }
 public Counts Docs=new Counts();
 public Counts Presentations {get {Calls.Add("presentations");return Docs;}}
 public Counts ProtectedViewWindows {get {Calls.Add("protectedviews");return new Counts();}}
 public Counts SlideShowWindows {get {Calls.Add("slideshows");return new Counts();}}
 public void Quit(){Calls.Add("quit");if(OnQuit!=null)OnQuit();}
}
public class Addin { public object Object {get;set;} }
public class Point:IConnectionPoint {
 public List<string> Calls; public Action OnUnadvise;
 public void Advise(object value,out int cookie){Calls.Add("advise");cookie=1;}
 public void Unadvise(int cookie){Calls.Add("unadvise");if(OnUnadvise!=null)OnUnadvise();}
 public void GetConnectionInterface(out Guid id){id=Guid.Empty;}
 public void GetConnectionPointContainer(out IConnectionPointContainer p){p=null;}
 public void EnumConnections(out IEnumConnections p){p=null;}
}
namespace AegisDesk.PptReader {
 internal static class ShutdownBuildPolicy { public const bool Enabled=true; }
 internal static class PptEventContract { internal static IConnectionPoint Resolve(object host){throw new Exception("unexpected_resolver");} }
 internal static class PptSinkLoader { internal static object Create(PptShutdownState state){throw new Exception("unexpected_loader");} }
 internal static class PptLifecycleFile { internal static int Writes; internal static string Text; internal static string CaptureDirectory(string input){if(Environment.GetEnvironmentVariable("AEGIS_PPT_LIFECYCLE_TRACE")!="1")throw new Exception("disabled_directory_io");return @"C:\fixture";} internal static void WriteOnce(string text,string directory){Writes++;Text=text;} }
}
namespace AegisDesk.PptShared {internal static class PptExtractionCore {
 public static string ExtractSlide(object a,string p,int s){return "";}
 public static void ReadShape(object a,List<Dictionary<string,object>> b,Dictionary<string,int> c){}
 public static void ReadShapeTracked(object a,List<Dictionary<string,object>> b,Dictionary<string,int> c,List<Dictionary<string,object>> d,string e){}
}}
class LifecycleHarness {
 static int checks;
 static void Check(bool yes,string code){checks++;if(!yes)throw new Exception(code);}
 static object Field(object x,string n){return x.GetType().GetField(n,BindingFlags.Instance|BindingFlags.NonPublic).GetValue(x);}
 // Managed ownership observations only: these fixtures do not model Office RCW counts or process exit.
 static void ReferenceLifecycle(string mode, bool enabled) {
  Environment.SetEnvironmentVariable("AEGIS_PPT_LIFECYCLE_TRACE",enabled?"1":null);
  PptLifecycleFile.Writes=0;PptLifecycleFile.Text=null;
  var reader=new Connect();var host=new Host();var addin=new Addin();Array custom=null;
  reader.OnConnection(host,1,addin,ref custom);
  var point=new Point{Calls=host.Calls};PptShutdownState state=null;
  var trace=(PptLifecycleTrace)Field(reader,"lifecycle");
  var guard=new PptShutdownGuard(host,Thread.CurrentThread.ManagedThreadId,
   delegate{host.Calls.Add("resolve");return point;},
   delegate(PptShutdownState s){state=s;host.Calls.Add("sink");return new object();},trace);
  reader.GetType().GetField("shutdown",BindingFlags.NonPublic|BindingFlags.Instance).SetValue(reader,guard);
  Action<bool> references=delegate(bool cleared){
   Check(cleared ? Field(reader,"application")==null : Object.ReferenceEquals(Field(reader,"application"),host),"reader_host_"+mode);
   Check(cleared ? Field(guard,"application")==null : Object.ReferenceEquals(Field(guard,"application"),host),"guard_host_"+mode);
   Check(cleared ? Field(reader,"shutdown")==null : Object.ReferenceEquals(Field(reader,"shutdown"),guard),"reader_guard_"+mode);
   Check(Object.ReferenceEquals(addin.Object,reader),"addin_object_retained_"+mode);
  };
  Action callbacks=delegate{
   int before=host.Calls.Count;
   string sequence=mode.Substring(mode.IndexOf('_')+1);
   foreach(char callback in sequence) {
    if(callback=='B')reader.OnBeginShutdown(ref custom);
    else reader.OnDisconnection(1,ref custom);
    references(true);
    Check(host.Calls.Count==before,"callback_rpc_"+mode);
   }
  };
  bool synchronous=mode.StartsWith("sync_");
  references(false);
  if(synchronous)host.OnQuit=delegate{
   Check(guard.Entered&&state.Active,"active_before_callback_"+mode);
   callbacks();
   Check(guard.Entered&&state.Active,"local_guard_preserved_"+mode);
   int before=host.Calls.Count;
   Check(reader.TryQuitIfIdle()=="not_supported","reader_reentry_"+mode);
   int token;Check(guard.TryQuitIfIdleTracked(false,out token)=="not_supported"&&token==0,"guard_reentry_"+mode);
   Check(host.Calls.Count==before,"reentry_rpc_"+mode);
  };
  Check(reader.TryQuitIfIdle()=="quit_requested","normal_return_"+mode);
  references(synchronous);
  Check(!guard.Entered&&!state.Active,"inactive_after_return_"+mode);
  string expected="resolve,sink,advise,alerts_set,alerts_get,presentations,protectedviews,slideshows,quit";
  if(!synchronous)expected+=",unadvise";
  Check(String.Join(",",host.Calls)==expected,"exact_rpc_sequence_"+mode);
  if(mode.StartsWith("delayed_")) { callbacks();Check(!guard.Entered&&!state.Active,"delayed_inactive_"+mode); }
  int count=host.Calls.Count;int secondToken;
  string expectedRetry=mode=="normal"?"protected":"not_supported";
  Check(reader.TryQuitIfIdle()==expectedRetry,"reader_retry_"+mode);
  Check(guard.TryQuitIfIdleTracked(false,out secondToken)==expectedRetry&&secondToken==0,"guard_retry_"+mode);
  Check(host.Calls.Count==count,"retry_rpc_"+mode);
  Check(PptLifecycleFile.Writes==(enabled?1:0),"reference_export_count_"+mode);
  Console.WriteLine("PASS references "+mode+" trace="+enabled+" host_fields="+(mode=="normal"?"retained":"cleared")+" addin_object=retained");
 }
 static string Run(string mode,bool enabled) {
  Environment.SetEnvironmentVariable("AEGIS_PPT_LIFECYCLE_TRACE",enabled?"1":null);
  PptLifecycleFile.Writes=0;PptLifecycleFile.Text=null;
  var reader=new Connect();var host=new Host();var point=new Point{Calls=host.Calls};Array custom=null;
  reader.OnConnection(host,1,new Addin(),ref custom);
  var trace=(PptLifecycleTrace)Field(reader,"lifecycle");PptShutdownState state=null;
  var guard=new PptShutdownGuard(host,Thread.CurrentThread.ManagedThreadId,delegate{host.Calls.Add("resolve");return point;},delegate(PptShutdownState s){state=s;host.Calls.Add("sink");return new object();},trace);
  reader.GetType().GetField("shutdown",BindingFlags.NonPublic|BindingFlags.Instance).SetValue(reader,guard);
  Action nested=delegate{Check(reader.TryQuitIfIdle()=="protected","nested_status");Check(PptLifecycleFile.Writes==0,"nested_export");};
  if(mode=="nested_quit")host.OnQuit=nested;
  if(mode=="nested_unadvise")point.OnUnadvise=nested;
  if(mode=="cancel")host.OnQuit=delegate{state.Cancelled=true;};
  if(mode=="broken")host.OnQuit=delegate{state.MarkBroken();};
  if(mode=="fatal")host.OnQuit=delegate{throw new COMException("fixed",unchecked((int)0x800706BA));};
  if(mode=="unknown")host.OnQuit=delegate{throw new InvalidOperationException();};
  if(mode=="unadvise_fail")point.OnUnadvise=delegate{throw new COMException("fixed",unchecked((int)0x80004005));};
  if(mode=="disconnect")host.OnQuit=delegate{reader.OnBeginShutdown(ref custom);};
  if(mode=="count")host.Docs.Count=1;
  string status;
  try {status=reader.TryQuitIfIdle();}catch(COMException e){status="exception:"+e.HResult.ToString("X8");}
  bool noComplete=mode=="count"||mode=="broken"||mode=="fatal"||mode=="unknown"||mode=="unadvise_fail";
  Check(PptLifecycleFile.Writes==(enabled&&!noComplete?1:0),"export_count_"+mode);
  Check(state==null||!state.Active,"sink_inactive");
  if(mode=="fatal"||mode=="unknown"||mode=="broken"||mode=="disconnect")Check(!host.Calls.Contains("unadvise"),"extra_rpc");
  if(enabled&&mode=="unadvise_fail"){bool found=false;foreach(object entry in (Array)Field(trace,"entries")){if((int)entry.GetType().GetField("stage",BindingFlags.Instance|BindingFlags.NonPublic).GetValue(entry)==14)found=true;}Check(found,"failure_trace_before_publication");}
  if(enabled&&mode=="disconnect")Check(PptLifecycleFile.Text.Contains(" 2 ")&&PptLifecycleFile.Text.Contains(" 3 0 "),"disconnect_trace");
  int token;string again=guard.TryQuitIfIdleTracked(false,out token);Check(token==0,"second_token");
  trace.FlushNormalReturn(token);Check(PptLifecycleFile.Writes==(enabled&&!noComplete?1:0),"second_consumes");
  return status+":"+String.Join(",",host.Calls);
 }
 static void DeepWrappedFatal() {
  Environment.SetEnvironmentVariable("AEGIS_PPT_LIFECYCLE_TRACE","1");
  PptLifecycleFile.Writes=0;
  var host=new Host();var point=new Point{Calls=host.Calls};var reader=new Connect();Array custom=null;
  reader.OnConnection(host,1,new Addin(),ref custom);
  var trace=(PptLifecycleTrace)Field(reader,"lifecycle");
  var guard=new PptShutdownGuard(host,Thread.CurrentThread.ManagedThreadId,delegate{return point;},delegate{return new object();},trace);
  Exception error=new COMException("fixture_fatal",unchecked((int)0x80010108));
  for(int i=0;i<12;i++)error=new InvalidOperationException("fixture_wrapper",error);
  host.OnQuit=delegate{throw error;};int token=-1;
  try{guard.TryQuitIfIdleTracked(false,out token);throw new Exception("deep_fatal_hidden");}
  catch(COMException e){Check(e.HResult==unchecked((int)0x80010108),"deep_fatal_hresult");}
  Check(token==0,"deep_fatal_token");trace.FlushNormalReturn(token);
  Check(PptLifecycleFile.Writes==0&&!host.Calls.Contains("unadvise"),"deep_fatal_no_export_or_rpc");
  int calls=host.Calls.Count;Check(guard.TryQuitIfIdleTracked(false,out token)=="not_supported"&&token==0&&host.Calls.Count==calls,"deep_fatal_retry_blocked");
  Console.WriteLine("PASS deep_wrapped_fatal hresult=80010108 token=0 retry_rpc=0");
 }
 [STAThread] static int Main(){try {
  DeepWrappedFatal();
  foreach(string mode in new[]{"normal","sync_B","sync_D","sync_BD","sync_DB","sync_BBDD","delayed_B","delayed_D","delayed_BD","delayed_DB","delayed_DDBB"})
   foreach(bool enabled in new[]{false,true})ReferenceLifecycle(mode,enabled);
  foreach(string mode in new[]{"normal","nested_quit","nested_unadvise","cancel","broken","fatal","unknown","unadvise_fail","disconnect","count"}) {
   string disabled=Run(mode,false),enabled=Run(mode,true);Check(disabled==enabled,"rpc_equivalence_"+mode);Console.WriteLine("PASS "+mode);
  }
  Environment.SetEnvironmentVariable("AEGIS_PPT_LIFECYCLE_TRACE",null);var off=new PptLifecycleTrace();off.BindOwner(Thread.CurrentThread.ManagedThreadId);
  Check(Field(off,"entries")==null&&off.Begin()==0&&off.Complete(1)==0,"disabled");off.Record(1,2,3);off.FlushNormalReturn(1);
  Environment.SetEnvironmentVariable("AEGIS_PPT_LIFECYCLE_TRACE","1");var trace=new PptLifecycleTrace();trace.BindOwner(Thread.CurrentThread.ManagedThreadId);
  var threads=new List<Thread>();for(int i=0;i<8;i++){var t=new Thread(delegate(){for(int j=0;j<10000;j++)trace.Record(1,0,0);Check(trace.Begin()==0&&trace.Complete(1)==0,"offthread_token");trace.FlushNormalReturn(1);});threads.Add(t);t.Start();}foreach(var t in threads)t.Join();
  int lower=(int)Field(trace,"offThreadDroppedLowerBound");Check(lower>0&&lower<=65535&&lower<=80000,"counter_lower_bound");Check((int)Field(trace,"count")==0,"offthread_buffer");
  for(int i=0;i<100;i++)trace.Record(1,0,0);Check((int)Field(trace,"count")==64&&(int)Field(trace,"dropped")==36,"buffer_bound");
  var host=new Host();var point=new Point{Calls=host.Calls};var guard=new PptShutdownGuard(host,Thread.CurrentThread.ManagedThreadId,delegate{return point;},delegate{return new object();},trace);
  int early;Check(guard.TryQuitIfIdleTracked(true,out early)=="protected"&&early==0,"reading_early");
  var wrong=new Thread(delegate(){int token;Check(guard.TryQuitIfIdleTracked(false,out token)=="not_supported"&&token==0,"wrong_thread");});wrong.Start();wrong.Join();
  guard.Disconnect();Check(guard.TryQuitIfIdleTracked(false,out early)=="not_supported"&&early==0,"disconnected_early");
  foreach(string mode in new[]{"fatal","unknown","broken"}) {
   var h=new Host();var cp=new Point{Calls=h.Calls};PptShutdownState st=null;
   var g=new PptShutdownGuard(h,Thread.CurrentThread.ManagedThreadId,delegate{return cp;},delegate(PptShutdownState value){st=value;return new object();},trace);
   h.OnQuit=delegate{if(mode=="broken")st.MarkBroken();else if(mode=="fatal")throw new COMException("fixed",unchecked((int)0x800706BA));else throw new Exception("fixed");};
   int token=-1;bool failed=false;try{g.TryQuitIfIdleTracked(false,out token);}catch(COMException){failed=true;}
   Check(failed&&token==0,"exception_token_"+mode);
  }
  Console.WriteLine("PASS checks="+checks);return 0;
 }catch(Exception e){Console.WriteLine("FAIL "+e);return 1;}}
}
