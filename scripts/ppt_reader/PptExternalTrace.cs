#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
using System;
using System.Diagnostics;
using System.Globalization;
using System.Text;
using System.Threading;
namespace AegisDesk.PptReader {
// Telemetry owns no Office objects, native capabilities, or shutdown decisions.
internal sealed class ExternalTrace {
 [ThreadStatic]internal static ExternalTrace Current;
 internal enum Stage { Bind,Prepare,Admission,Identity,IdentityRelease,Guard,Resolve,Sink,Advise,AlertsSet,AlertsRead,Presentations,ProtectedViews,SlideShows,Poll,Commit,Ack,Completion,Cancel,Disconnect,Unadvise,Release,Capabilities,CapabilityClose,TimerCleanup,ProtocolCleanup,PrimaryError,CleanupError,FatalError,Terminal,Generation,Reading,Owner,DeferredDepth,Quit,ServiceEntry,ServiceExit,EventEntry,EventExit,LifecycleEntry,LifecycleExit,IdentityObservation,ErrorDetail }
 internal enum Result { Entered,Success,Refused,Failed,Unknown,Skipped }
 struct Entry {internal long ticks;internal int stage,result,a,b,c;}
 const int Capacity=128;
 readonly Entry[] entries=new Entry[Capacity];
 readonly object sync=new object();
 readonly string directory,run;readonly int pid;readonly long creation;
 int count,dropped,exported,terminal,firstStage=-1,firstReason,firstFlags,firstDetail;bool enabled,requested;
 internal void Start(){requested=true;
#if AEGIS_PPT_CAPTURE_DIAGNOSTIC
  if(captureRequested&&System.Threading.Interlocked.Exchange(ref captureAttachAttempted,1)==0){capture=PptCaptureBuffer.TryAdopt(preAttachRecords);if(capture!=null)capture.SetGeneration(latestGeneration);}
#endif
 }
 internal int ExportAttempts,ExportFailures;int latestGeneration;
#if AEGIS_PPT_CAPTURE_DIAGNOSTIC
 readonly bool captureRequested=Environment.GetEnvironmentVariable("AEGIS_PPT_CAPTURE_DIAGNOSTIC")=="1";
 PptCaptureBuffer capture;int captureAttachAttempted,preAttachRecords;
#endif
 internal bool CaptureHealthy {get{
#if AEGIS_PPT_CAPTURE_DIAGNOSTIC
  return !captureRequested||capture!=null&&capture.Healthy;
#else
  return true;
#endif
 }}
 internal void ServiceBoundary(bool enter){
#if AEGIS_PPT_CAPTURE_DIAGNOSTIC
  if(capture==null)return;if(enter)capture.EnterService();Record(enter?Stage.ServiceEntry:Stage.ServiceExit,Result.Success);if(!enter)capture.LeaveService();
#endif
 }
 internal void EventBoundary(int kind,bool enter,int flags){
#if AEGIS_PPT_CAPTURE_DIAGNOSTIC
  if(capture==null)return;if(enter)capture.EnterEvent();Record(enter?Stage.EventEntry:Stage.EventExit,Result.Success,kind,flags);if(!enter)capture.LeaveEvent();
#endif
 }
 internal void LifecycleBoundary(int kind,bool enter,int mode){
#if AEGIS_PPT_CAPTURE_DIAGNOSTIC
  if(enter)EventBoundary(kind,true,0);Record(enter?Stage.LifecycleEntry:Stage.LifecycleExit,Result.Success,kind,mode);if(!enter)EventBoundary(kind,false,0);
#endif
 }
 internal void Generation(int value){latestGeneration=value;
#if AEGIS_PPT_CAPTURE_DIAGNOSTIC
  if(capture!=null)capture.SetGeneration(value);
#endif
 }
#if AEGIS_BOOTSTRAP_CLEANUP_TEST
 internal Action<string,int> TestWriter;
#endif
 internal ExternalTrace(){
  try {
   enabled=Environment.GetEnvironmentVariable("AEGIS_PPT_EXTERNAL_DIAGNOSTIC")=="1"&&Environment.GetEnvironmentVariable("AEGIS_PPT_LIFECYCLE_TRACE")=="1";
   if(!enabled)return;
   Guid id;if(!Guid.TryParseExact(Environment.GetEnvironmentVariable("AEGIS_PPT_TRACE_RUN"),"N",out id)||id==Guid.Empty){enabled=false;return;}
   run=id.ToString("N");pid=Process.GetCurrentProcess().Id;creation=ProcessFence.Creation();
   #if AEGIS_PPT_CAPTURE_DIAGNOSTIC
   if(captureRequested)return;
#endif
   directory=PptLifecycleFile.CaptureDirectory(Environment.GetEnvironmentVariable("AEGIS_PPT_LIFECYCLE_DIR"));
  }catch{enabled=false;}
 }
 internal void Record(Stage stage,Result result,int a=0,int b=0,int c=0){
  try{if(!enabled)return;
#if AEGIS_PPT_CAPTURE_DIAGNOSTIC
   if(captureRequested){if(capture!=null)capture.Record((int)stage,(int)result,a,b,c);else if(preAttachRecords<int.MaxValue)preAttachRecords++;return;}
#endif
   lock(sync){if(count==Capacity){if(dropped<int.MaxValue)dropped++;return;}entries[count++]=new Entry{ticks=Stopwatch.GetTimestamp(),stage=(int)stage,result=(int)result,a=a,b=b,c=c};}}catch{/* Telemetry cannot poison the guard. */}
 }
 internal void Refuse(Stage stage,int reason,int flags=0,int detail=0){
  try{if(!enabled)return;
#if AEGIS_PPT_CAPTURE_DIAGNOSTIC
   if(captureRequested){Record(stage,Result.Refused,reason,flags,detail);return;}
#endif
   lock(sync){if(firstStage<0){firstStage=(int)stage;firstReason=reason;firstFlags=flags;firstDetail=detail;}}Record(stage,Result.Refused,reason,flags,detail);}catch{}
 }
 internal void Error(Exception error,bool cleanup){try{for(int depth=0;error!=null&&depth<16;depth++,error=error.InnerException)Record(cleanup?Stage.CleanupError:Stage.PrimaryError,Result.Failed,error.HResult,depth);}catch{}}
 internal void Errors(PptCleanupErrors errors){try{Error(errors.Primary,false);Error(errors.Cleanup,true);if(errors.StickyFatal!=null)Record(Stage.FatalError,Result.Failed,errors.StickyFatal.HResult);}catch{}}
 // Called after the outer request/poll/lifecycle body has unwound. The independent
 // best-effort worker may overlap callback return: no callback-thread file I/O,
 // Office/COM access, owner wait, or guard-state mutation. No owner dispatch.
 // The worker serializes the copied entries and captured immutable metadata only.
 internal void ExportTerminal(bool incomplete){
  try{
   if(!enabled||!requested)return;
#if AEGIS_PPT_CAPTURE_DIAGNOSTIC
   if(captureRequested){Record(Stage.Terminal,incomplete?Result.Unknown:Result.Success);return;}
#endif
   Entry[] copy;int n,loss,slot,first,reason,flags,detail;
   lock(sync){
    if(terminal==2||incomplete&&terminal!=0)return;
    terminal=incomplete?1:2;slot=exported++;if(slot>=2)return;
    n=count;loss=dropped;first=firstStage;reason=firstReason;flags=firstFlags;detail=firstDetail;copy=new Entry[n];Array.Copy(entries,copy,n);
   }
   ExportAttempts++;
   ThreadPool.QueueUserWorkItem(delegate{
    try{
     var text=new StringBuilder();
     text.AppendLine("schema=2 run="+run+" pid="+pid+" creation="+creation+" frequency="+Stopwatch.Frequency+" snapshot="+slot+" incomplete="+(incomplete?1:0)+" dropped="+loss+" first_stage="+first+" first_reason="+reason+" first_flags="+flags+" first_detail="+detail);
     for(int i=0;i<n;i++){Entry e=copy[i];text.AppendLine(i+" "+e.ticks+" "+((Stage)e.stage)+" "+((Result)e.result)+" "+e.a+" "+e.b+" "+e.c);}
     text.AppendLine("END count="+n);
#if AEGIS_BOOTSTRAP_CLEANUP_TEST
     if(TestWriter!=null){TestWriter(text.ToString(),slot);return;}
#endif
     if(!PptLifecycleFile.WriteExternalSnapshot(text.ToString(),directory,slot))Interlocked.Increment(ref ExportFailures);
    }catch{Interlocked.Increment(ref ExportFailures);}
   });
  }catch{Interlocked.Increment(ref ExportFailures);}
 }
 internal string Status {get{return "trace_attempts="+ExportAttempts+" trace_failures="+ExportFailures;}}
}
}
#endif
