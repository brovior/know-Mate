using System;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Threading;
using AegisDesk.PptReader;
partial class Probe {
 static Assembly Events;
 static readonly string[] Cases={"idle","nonzero","cancel_count","cancel_quit","disconnect_count","disconnect_quit","broken_count","broken_quit","refused","limit","partial","fatal_advise","zero_cookie","unadvise_failure","unadvise_fatal","quit_fatal","quit_unknown","bad_contract","alias"};
 [System.Runtime.CompilerServices.MethodImpl(System.Runtime.CompilerServices.MethodImplOptions.NoInlining)]
 static void RunProduct(Spy spy,string scenario){
  current=spy;object host=Marshal.GetObjectForIUnknown(spy.Pointer),sink=null;PptShutdownState state=null;var point=spy.CP;
  Meta.BadContract=scenario=="bad_contract";
  var guard=new PptShutdownGuard(host,Thread.CurrentThread.ManagedThreadId,null,s=>{state=s;sink=Activator.CreateInstance(Events.GetType("AegisDesk.PptEvents.PptEventSink"),new object[]{s});return sink;});
  Action cancel=()=>{object[] a={null,false};sink.GetType().GetMethod("PresentationBeforeClose").Invoke(sink,a);Check((bool)a[1],"active_cancel");};
  if(scenario=="nonzero")spy.Children["Presentations"].CountValue=1;
  spy.Children["Presentations"].OnInvoke=n=>{if(n!="Count")return;if(scenario=="cancel_count")cancel();if(scenario=="disconnect_count")guard.Disconnect();if(scenario=="broken_count")state.MarkBroken();};
  spy.OnQuit=()=>{if(scenario=="cancel_quit"||scenario.StartsWith("unadvise_"))cancel();if(scenario=="disconnect_quit")guard.Disconnect();if(scenario=="broken_quit")state.MarkBroken();if(scenario=="quit_fatal")throw new COMException("fixture",unchecked((int)0x80010108));if(scenario=="quit_unknown")throw new InvalidOperationException();};
  if(scenario=="refused")point.AdviseError=unchecked((int)0x80040202);
  if(scenario=="limit")point.AdviseError=unchecked((int)0x80040201);
  if(scenario=="partial"||scenario=="fatal_advise"){point.Partial=true;point.AdviseError=scenario=="partial"?unchecked((int)0x80004005):unchecked((int)0x80010108);}
  if(scenario=="zero_cookie")point.ZeroCookie=true;
  if(scenario=="unadvise_failure"||scenario=="unadvise_fatal")point.UnadviseError=scenario=="unadvise_fatal"?unchecked((int)0x80010108):unchecked((int)0x80004005);
  IntPtr alias=IntPtr.Zero;if(scenario=="alias"){alias=point.Pointer;Marshal.AddRef(alias);}
  string result=null;Exception error=null;try{result=guard.TryQuitIfIdle(false);}catch(Exception e){error=e;}
  bool fatal=scenario=="fatal_advise"||scenario=="quit_fatal"||scenario=="unadvise_fatal";
  bool unknown=scenario=="partial"||scenario=="zero_cookie"||scenario=="broken_count"||scenario=="broken_quit"||scenario=="quit_unknown"||scenario=="unadvise_failure";
  if(fatal)Check(error is COMException&&error.HResult==unchecked((int)0x80010108),"fatal_priority_"+scenario);
  else if(unknown)Check(error is COMException&&error.HResult==unchecked((int)0x80004005),"unknown_result_"+scenario);
  else{Check(error==null,"unexpected_error_"+scenario);string expected=scenario=="nonzero"||scenario.StartsWith("cancel_")?"protected":scenario=="disconnect_count"||scenario=="refused"||scenario=="limit"||scenario=="bad_contract"?"not_supported":"quit_requested";Check(result==expected,"result_"+scenario+"_"+result);}
  bool quit=scenario=="idle"||scenario=="alias"||scenario=="cancel_quit"||scenario=="disconnect_quit"||scenario=="broken_quit"||scenario.StartsWith("quit_")||scenario.StartsWith("unadvise_");
  Check(spy.Quits==(quit?1:0)&&spy.AfterQuit==0,"single_quit_"+scenario);
  if(scenario=="idle"||scenario=="alias"){
   Check(state.Active&&point.Refs==2+(alias!=IntPtr.Zero?1:0)&&point.Unadvised==0,"retained_for_late_close_"+scenario);
   cancel();Check(!state.Active&&state.CloseCancelled,"late_close_cancelled_"+scenario);
   Check(guard.QueryOutcome()=="protected"&&point.Unadvised==1,"late_close_yielded_"+scenario);
   Check(guard.QueryOutcome()=="protected"&&point.Unadvised==1,"late_cleanup_once_"+scenario);
  }
  Check(point.Refs==1+(alias!=IntPtr.Zero?1:0),"owned_cp_refs_before_gc_"+scenario);
  if(scenario=="partial"||scenario=="fatal_advise"||scenario=="zero_cookie"||scenario.StartsWith("disconnect_")||scenario.StartsWith("broken_")||scenario.StartsWith("quit_"))Check(point.Unadvised==0,"unsafe_unadvise_"+scenario);
  if(state!=null){Check(!state.Active,"inactive_after_return");object[] a={null,false};sink.GetType().GetMethod("PresentationBeforeClose").Invoke(sink,a);Check(!(bool)a[1],"inactive_sink_cancels");}
  if(quit||fatal||unknown){int calls=spy.Quits;guard.TryQuitIfIdle(false);Check(spy.Quits==calls,"quit_retried");}
  if(alias!=IntPtr.Zero){point.VerifyContract();Marshal.Release(alias);}
  Console.WriteLine("PRODUCT_OWNED_PASS scenario="+scenario+" cp_refs_before_gc="+point.Refs+" quits="+spy.Quits+" unadvise="+point.Unadvised);
  point.FixtureDropConnection();spy.OnQuit=null;foreach(var child in spy.Children.Values)child.OnInvoke=null;Meta.BadContract=false;GC.KeepAlive(host);
 }
 [STAThread]static int Main(string[] args){try{
  Core=Assembly.GetExecutingAssembly();Events=Assembly.LoadFrom(args[0]);Guid id=new Guid("91493440-5A91-11CF-8700-00AA0060263B");ITypeLib lib;LoadRegTypeLib(ref id,2,12,0,out lib);int count=0;
  foreach(string scenario in Cases)foreach(bool shared in new[]{false,true}){
   var spy=new Spy(Find(lib,"_Application"));spy.CP=new NativePoint();spy.CP.Owner=spy.container;roots.Add(spy);Spy first=null;
   foreach(string n in new[]{"Presentations","ProtectedViewWindows","SlideShowWindows"}){var child=shared&&first!=null?first:new Spy(Find(lib,n),true);if(first==null)first=child;roots.Add(child);spy.Children[n]=child;}
   RunProduct(spy,scenario);GC.Collect();GC.WaitForPendingFinalizers();GC.Collect();Meta.VerifyAfterGc();
   // Report CLR-owned host references separately; explicit CP balance was asserted before GC.
   Console.WriteLine("PRODUCT_POST_GC scenario="+scenario+" host_refs="+spy.refs);count++;
  }
  Console.WriteLine("PRODUCT_OWNED_RESULT cases="+count+" arch="+(IntPtr.Size*8));GC.KeepAlive(roots);return 0;
 }catch(Exception e){Console.WriteLine(e);return 1;}}
}
