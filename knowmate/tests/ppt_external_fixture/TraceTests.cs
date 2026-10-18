using System;
using System.Collections.Generic;
using System.Reflection;
using System.Threading;
using System.Windows.Forms;
using AegisDesk.PptReader;
static class TraceTests {
 static void Check(bool value,string label){Tests.Check(value,label);}
 static ExternalTrace Trace(Fixture f){return (ExternalTrace)Tests.Field(f.Connect,"externalTrace");}
 static void Boundary(Fixture f){typeof(Connect).GetMethod("ExportExternalBoundary",BindingFlags.NonPublic|BindingFlags.Instance).Invoke(f.Connect,null);}
 [STAThread]static int Main(string[] args){
  var window=new NativeWindow();try{
   Tests.Events=Assembly.LoadFrom(args[1]);window.CreateHandle(new CreateParams{Caption="TraceFixture"});
   using(var f=new Fixture(window.Handle)){
    var trace=Trace(f);var texts=new List<string>();var signal=new AutoResetEvent(false);int writes=0;
    if(args[0]=="processloss"){f.Bind();f.Ready();Console.WriteLine("OWNED_PROCESS_EXIT_BEFORE_TERMINAL");Environment.Exit(0);}
    if(args[0]=="nativewriter"){
     trace.ExportTerminal(false);Check(trace.ExportAttempts==0,"pre_request_disconnect_cannot_consume_export");trace.Start();
     trace.Record(ExternalTrace.Stage.Terminal,ExternalTrace.Result.Unknown);trace.ExportTerminal(true);
     trace.Record(ExternalTrace.Stage.ProtocolCleanup,ExternalTrace.Result.Success);trace.ExportTerminal(false);
     string directory=Environment.GetEnvironmentVariable("AEGIS_PPT_LIFECYCLE_DIR");string prefix=System.IO.Path.Combine(directory,"host-"+System.Diagnostics.Process.GetCurrentProcess().Id+"-external-");
     for(int i=0;i<300&&(!System.IO.File.Exists(prefix+"0.log")||!System.IO.File.Exists(prefix+"1.log"));i++)Thread.Sleep(10);
     for(int i=0;i<2;i++){
      string text=null;for(int j=0;j<100;j++){try{text=System.IO.File.ReadAllText(prefix+i+".log");if(text.Contains("END count="))break;}catch(System.IO.IOException){}Thread.Sleep(10);}
      Check(text!=null&&text.Contains("END count=")&&text.Contains("snapshot="+i)&&text.Contains("incomplete="+(i==0?1:0)),"native_immutable_frame");
      string before=text;Check(!PptLifecycleFile.WriteExternalSnapshot("replacement",directory,i),"create_new_no_overwrite");Check(System.IO.File.ReadAllText(prefix+i+".log")==before,"immutable");
     }
     Check(trace.ExportFailures==0,"native_writer_success");Console.WriteLine("PASS nativewriter");return 0;
    }
    trace.TestWriter=(text,slot)=>{Interlocked.Increment(ref writes);lock(texts)texts.Add(text);if(args[0]=="writerfail"||args[0]=="primarycleanup")throw new Exception("telemetry_only");signal.Set();};
    f.Bind();
    if(args[0]=="primarycleanup"){
     f.Identity.Acquire=o=>{if(f.Identity.Acquired==1)throw new System.Runtime.InteropServices.COMException("primary",unchecked((int)0x80004005));return System.Runtime.InteropServices.Marshal.GetIUnknownForObject(o);};
     f.Identity.Release=p=>{System.Runtime.InteropServices.Marshal.Release(p);throw new System.Runtime.InteropServices.COMException("cleanup",unchecked((int)0x80010108));};
     Check(f.Begin()=="external_failed"&&f.Session.Error.HResult==unchecked((int)0x80010108),"fatal_cleanup_priority");
    }
    else if(args[0]=="earlyrefusal")Check(f.Connect.BeginExternalDiagnostic(new Host())=="external_refused","refusal");
    else {f.Ready();Boundary(f);Check(trace.ExportAttempts==0,"ready_never_exports");
     if(args[0]=="overflow")for(int i=0;i<400;i++)trace.Record(ExternalTrace.Stage.Poll,ExternalTrace.Result.Entered);
     f.Commit();Check(trace.ExportAttempts==0,"ack_never_exports");
     if(args[0]=="unknown"){f.Now=6000;f.Connect.PollExternalDiagnostic(false,false,true);}
     else f.Complete();
    }
    var state=f.Session.State;var outcome=f.Guard.ExternalOutcome;int quits=f.Quits,acks=f.Caps.AckCalls,cleanups=f.Caps.ReleaseCalls;
    Boundary(f);
    if(args[0]=="writerfail"||args[0]=="primarycleanup"){for(int i=0;i<100&&trace.ExportFailures==0;i++)Thread.Sleep(10);Check(trace.ExportFailures==1,"write_failure_separate");}
    else Check(signal.WaitOne(3000),"terminal_exported");
    Check(f.Session.State==state&&f.Guard.ExternalOutcome==outcome&&f.Quits==quits&&f.Caps.AckCalls==acks&&f.Caps.ReleaseCalls==cleanups,"logging_preserves_behavior");
    Boundary(f);Check(trace.ExportAttempts==1,"no_duplicate_export");
    if(args[0]=="unknown"){
     Check(state==ExternalProtocol.Phase.UnknownProtected&&quits==0&&acks==1&&cleanups==0,"unknown_no_cleanup_retry");
     lock(texts)Check(texts[0].Contains("incomplete=1"),"unknown_marker");
     Array custom=null;f.Connect.OnDisconnection(1,ref custom);Check(signal.WaitOne(3000),"later_cleanup_separate_export");
     Check(trace.ExportAttempts==2,"two_bounded_snapshots");
    }else if(args[0]=="earlyrefusal"){
     Check(quits==0&&acks==0&&cleanups==1,"refusal_unchanged");
     lock(texts)Check(texts[0].Contains("Identity Refused")&&texts[0].Contains("first_stage=3")&&!texts[0].Contains("Guard Entered"),"first_refusal_and_skipped_guard");
    }else if(args[0]=="primarycleanup"){
     Check(f.Session.Error.HResult==unchecked((int)0x80010108)&&quits==0&&acks==0&&cleanups==1,"writer_cannot_replace_primary_cleanup");
     lock(texts)Check(texts[0].Contains("PrimaryError Failed -2147467259")&&texts[0].Contains("CleanupError Failed -2147417848"),"both_original_errors_retained");
    }else Check(quits==1&&acks==1&&cleanups==1&&outcome=="normal_return","normal_unchanged");
    Check(ExternalTrace.Current==null,"ambient_trace_restored");
    if(args[0]!="writerfail")lock(texts){
     string last=texts[texts.Count-1];Check(last.Contains("END count="),"complete_frame");
     if(args[0]=="overflow")Check(!last.Contains("dropped=0"),"overflow_marked");
     else Check(last.Contains("Terminal "),"terminal_record");
    }
    Console.WriteLine("PASS "+args[0]+" quits="+quits+" ack="+acks+" cleanup="+cleanups+" state="+state+" writes="+writes);
   }return 0;
  }catch(Exception error){Console.WriteLine(error);return 1;}finally{window.DestroyHandle();}
 }
}
