using System;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using AegisDesk.PptReader;
static class CleanupTests {
 const int Fatal=unchecked((int)0x80010108);
 static void Check(bool value,string reason){if(!value)throw new Exception(reason);}
 static Exception Capture(Action action){try{action();}catch(Exception error){return error is TargetInvocationException?error.InnerException:error;}throw new Exception("exception_missing");}
 static void Bind(Fixture f,Exception cleanup){f.Connect.BindExternalDiagnostic(new ExternalDiagnosticServices(new ProcessFence(f.Window,System.Diagnostics.Process.GetCurrentProcess().Id,ProcessFence.Creation()),f.Caps.Validate,()=>f.Now,f.Caps.Ack,()=>{f.Caps.Release();throw cleanup;},f.Identity));}
 static void Run(string mode,IntPtr hwnd){
  if(mode=="nested_priority"||mode=="independent_cleanup"){
   var primary=new COMException("primary_fatal",Fatal);var secondary=new InvalidOperationException("cleanup");var outer=new DiagnosticOperation();var inner=new DiagnosticOperation();int attempts=0;
   var seen=Capture(()=>outer.Run<int>(()=>inner.Run<int>(()=>{throw primary;},()=>{attempts++;throw secondary;}),()=>{attempts++;throw new Exception("outer_cleanup");}));
   Check(Object.ReferenceEquals(seen,primary)&&attempts==2,"nested_primary_preserved");Check(inner.Errors.Cleanup==secondary&&outer.Errors.Cleanup!=null,"independent_ledgers");
   var independent=new DiagnosticOperation();attempts=0;independent.Cleanup(()=>{attempts++;throw primary;});independent.Cleanup(()=>{attempts++;throw secondary;});Check(attempts==2&&independent.Errors.StickyFatal==primary,"all_cleanup_attempted");return;
  }
  using(var f=new Fixture(hwnd)){
#if AEGIS_BOOTSTRAP_CLEANUP_TEST
   if(mode=="begin_shutdown_reentrant"){
    int stops=0,disposals=0,guards=0;var old=f.Guard;
    var timer=new Timer();timer.Interval=100;timer.Start();
    typeof(Connect).GetField("bootstrapTimer",BindingFlags.NonPublic|BindingFlags.Instance).SetValue(f.Connect,timer);
    BootstrapCleanupFaults.AfterTimerStop=()=>{stops++;Array nested=null;f.Connect.OnBeginShutdown(ref nested);throw new InvalidOperationException("timer_stop_failed");};
    BootstrapCleanupFaults.AfterTimerDispose=()=>{disposals++;throw new InvalidOperationException("timer_dispose_failed");};
    BootstrapCleanupFaults.AfterGuardDisconnect=()=>{guards++;};
    f.Connect.BindExternalDiagnostic(new ExternalDiagnosticServices(new ProcessFence(hwnd,System.Diagnostics.Process.GetCurrentProcess().Id,ProcessFence.Creation()),f.Caps.Validate,()=>f.Now,f.Caps.Ack,()=>{f.Caps.Release();Array nested=null;f.Connect.OnBeginShutdown(ref nested);throw new InvalidOperationException("handle_close_failed");},f.Identity));
    Array custom=null;f.Connect.OnBeginShutdown(ref custom);f.Connect.OnBeginShutdown(ref custom);
    Check(stops==1&&disposals==1&&guards==1&&f.Caps.ReleaseCalls==1,"reentrant_exactly_once");
    Check(Tests.Field(f.Connect,"application")==null&&Tests.Field(f.Connect,"shutdown")==null&&Tests.Field(f.Connect,"bootstrapTimer")==null&&(int)Tests.Field(f.Connect,"mainThread")==0,"begin_shutdown_fields_clear");
    Check(old.TryQuitIfIdleTracked(false,out ignored)=="not_supported","begin_shutdown_guard_disconnected");
    Check(f.Connect.DiagnosticOperations.Exists(op=>op.Errors.Unknown),"lifecycle_unknown_recorded");
    Check(f.Connect.DiagnosticOperations.Exists(op=>op.CleanupFailures.Count==2&&op.CleanupFailures[0].Message=="timer_stop_failed"&&op.CleanupFailures[1].Message=="timer_dispose_failed"),"both_timer_errors_recorded");
    Check(f.Connect.DiagnosticOperations.Exists(op=>op.CleanupFailures.Exists(error=>error.Message=="handle_close_failed")),"handle_error_recorded");
    BootstrapCleanupFaults.AfterTimerStop=()=>{};BootstrapCleanupFaults.AfterTimerDispose=()=>{};BootstrapCleanupFaults.AfterGuardDisconnect=()=>{};return;
   }
#endif
   var primary=mode=="normal_unknown"?(Exception)new PptCleanupOutcomeUnknown(new Exception("uncertain")):new COMException("primary_fatal",Fatal);
   var cleanup=mode=="cleanup_fatal"?(Exception)new COMException("cleanup_fatal",Fatal):new InvalidOperationException("cleanup_failed");
   Bind(f,cleanup);
   if(mode=="normal_fatal"||mode=="normal_unknown")f.Host.OnQuit=()=>{throw primary;};
   if(mode=="normal_fatal"||mode=="normal_unknown"||mode=="cleanup_fatal"||mode=="cleanup_only"){
    var error=Capture(()=>f.Connect.TryQuitIfIdle());
    if(mode=="normal_fatal"||mode=="cleanup_fatal")Check(Marshal.GetHRForException(error)==Fatal,"fatal_priority");else Check(error is PptCleanupOutcomeUnknown,"unknown_preserved");
    var ledger=f.Connect.DiagnosticOperations[f.Connect.DiagnosticOperations.Count-1].Errors;
    Check(ledger.Cleanup==cleanup&&ledger.Unknown,"cleanup_recorded");
    if(mode=="normal_fatal"||mode=="normal_unknown")Check(ledger.Primary!=null,"primary_recorded");
   }else if(mode=="reader_begin_failure"){
    Environment.SetEnvironmentVariable("AEGIS_PPT_EXTERNAL_READER_V1","BLOCKED");
    Check(Capture(()=>f.Begin()) is PptCleanupOutcomeUnknown,"begin_cleanup_unknown");Check(f.Session==null,"no_protocol");
   }else if(mode=="prepare_fatal"){
    f.Identity.Acquire=o=>{throw primary;};Check(f.Begin()=="external_failed","prepare_failed");Check(Marshal.GetHRForException(f.Session.Error)==Fatal,"prepare_primary_priority");Check(f.Session.CleanupFailed,"protocol_cleanup_recorded");
   }else if(mode=="disconnect_bound"||mode=="reconnect_bound"||mode=="disconnect_ready"){
    if(mode=="disconnect_ready")f.Ready();
    var old=f.Guard;Array custom=null;
    if(mode!="reconnect_bound"){
     f.Connect.OnDisconnection(1,ref custom);
     Check(Tests.Field(f.Connect,"shutdown")==null&&Tests.Field(f.Connect,"application")==null&&(int)Tests.Field(f.Connect,"mainThread")==0,"fields_detached");
    }else{f.Reconnect();Check(f.Connect.ProtocolVersion()==3&&!Object.ReferenceEquals(f.Guard,old),"reconnect_completed");}
    Check(f.Connect.DiagnosticOperations.Exists(op=>op.Errors.Cleanup==cleanup),"lifecycle_failure_recorded");
    if(mode!="disconnect_ready")Check(old.TryQuitIfIdleTracked(false,out ignored)=="not_supported","old_guard_disconnected");
   }else throw new Exception("unknown_case");
   Check(f.Caps.ReleaseCalls==1,"exactly_one_release");
   Array end=null;f.Connect.OnDisconnection(1,ref end);f.Connect.OnDisconnection(1,ref end);Check(f.Caps.ReleaseCalls==1,"idempotent_lifecycle");
  }
 }
 static int ignored;
 [STAThread]static int Main(string[] args){var window=new NativeWindow();try{Tests.Events=Assembly.LoadFrom(args[1]);window.CreateHandle(new CreateParams{Caption="CleanupFixture"});Run(args[0],window.Handle);Console.WriteLine(args[0]+" PASS");return 0;}catch(Exception error){Console.WriteLine(error);return 1;}finally{window.DestroyHandle();}}
}
