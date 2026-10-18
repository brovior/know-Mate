#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
using System;
using System.Runtime.InteropServices;
using System.Threading;
using AegisDesk.PptReader;
// Explicit diagnostic build only; never registers COM or starts Office.
internal sealed class IdentityProbe {
 internal enum Comparison { NotChecked,Same,Different,Failed }
 internal Comparison Result {get;private set;}
 internal ExternalTrace Trace;
 internal int Acquired,Released,ReleaseAttempts;
 internal readonly PptCleanupErrors Errors=new PptCleanupErrors();
#if AEGIS_BOOTSTRAP_CLEANUP_TEST
 internal Func<object,IntPtr> Acquire=Marshal.GetIUnknownForObject;
 internal Action<IntPtr> Release=p=>{Marshal.Release(p);};
#else
 internal readonly Func<object,IntPtr> Acquire=Marshal.GetIUnknownForObject;
 internal readonly Action<IntPtr> Release=p=>{Marshal.Release(p);};
#endif
 void Return(ref IntPtr pointer){
  IntPtr owned=pointer;pointer=IntPtr.Zero;if(owned==IntPtr.Zero)return;
  ReleaseAttempts++;if(Trace!=null)Trace.Record(ExternalTrace.Stage.IdentityRelease,ExternalTrace.Result.Entered,ReleaseAttempts);
  try{Release(owned);Released++;if(Trace!=null)Trace.Record(ExternalTrace.Stage.IdentityRelease,ExternalTrace.Result.Success,Released);}catch(Exception error){Errors.Unknown=true;Errors.Observe(error,false);if(Trace!=null)Trace.Error(error,true);}
 }
 internal bool Same(object host,object candidate,bool failSecond) {
  Result=Comparison.NotChecked;if(Trace!=null)Trace.Record(ExternalTrace.Stage.Identity,ExternalTrace.Result.Entered);
  IntPtr first=IntPtr.Zero,second=IntPtr.Zero;bool same=false;
  try {
   first=Acquire(host);Acquired++;
   if(failSecond)throw new InvalidOperationException("identity_second_acquisition");
   second=Acquire(candidate);Acquired++;
   if(first==IntPtr.Zero||second==IntPtr.Zero)throw new InvalidOperationException("identity_null");
   same=first==second;
   Result=same?Comparison.Same:Comparison.Different;if(Trace!=null){Trace.Record(ExternalTrace.Stage.Identity,ExternalTrace.Result.Success,(int)Result,Acquired);if(!same)Trace.Refuse(ExternalTrace.Stage.Identity,(int)Result);}
  }catch(Exception error){Result=Comparison.Failed;Errors.Observe(error,true);if(Trace!=null)Trace.Error(error,false);}
  finally {Return(ref second);Return(ref first);}
  Errors.Throw();return same;
 }
}
internal sealed class ExternalProtocol {
 internal enum Phase { New,Preparing,Ready,Committed,AckPublished,Completing,Terminal,UnknownProtected,Disconnected }
 internal Phase State {get;private set;}
 internal enum PrepareDecision { NotReached,Ready,IdentityMismatch,GenerationOrStateChange,Presignaled,GuardResult,Failed }
 internal PrepareDecision Decision {get;private set;}
 internal IdentityProbe.Comparison IdentityResult {get;private set;}
 internal PptShutdownGuard.PreparePrecheck GuardPrecheck {get;private set;}
 internal bool GuardReached {get;private set;}
 internal string GuardResult {get;private set;}
 internal readonly PptShutdownGuard Guard;
 internal ExternalTrace Telemetry;
 void Note(ExternalTrace.Stage stage,ExternalTrace.Result result,int a=0,int b=0,int c=0){if(Telemetry!=null)Telemetry.Record(stage,result,a,b,c);}
 void Refusal(int reason){if(Telemetry!=null)Telemetry.Refuse(ExternalTrace.Stage.Prepare,reason);}
 bool CurrentGeneration(){int current=readGeneration();Note(ExternalTrace.Stage.Generation,ExternalTrace.Result.Success,generation,current);return current==generation;}
 readonly int owner=Thread.CurrentThread.ManagedThreadId,generation;
 readonly Func<long> clock;readonly Action ack,release;long deadline;bool released,disconnected;
 internal int AckCount,ReleaseCount;internal bool CleanupFailed;
 readonly PptCleanupErrors errors=new PptCleanupErrors();readonly Func<int> readGeneration;readonly Func<bool> readBusy;
 int prepareDepth;bool finishing;string deferredFinish;int lastPoll=-1;
 internal Exception Error {get{return errors.Chosen;}}
 internal ExternalProtocol(PptShutdownGuard guard,int currentGeneration,Func<long> monotonic,Action publishAck,Action disposeCapabilities,Func<int> current=null,Func<bool> busy=null){Guard=guard;generation=currentGeneration;clock=monotonic;ack=publishAck;release=disposeCapabilities;readGeneration=current??(()=>generation);readBusy=busy??(()=>false);}
 void Owner(){if(Thread.CurrentThread.ManagedThreadId!=owner||Thread.CurrentThread.GetApartmentState()!=ApartmentState.STA)throw new InvalidOperationException("wrong_owner");}
 internal bool Prepare(object host,object candidate,IdentityProbe identity,bool failIdentity,bool presignaled,bool reading=false){
  Owner();Note(ExternalTrace.Stage.Prepare,ExternalTrace.Result.Entered,(int)State,generation,reading?1:0);if(State!=Phase.New){Refusal(100);return false;}
  State=Phase.Preparing;prepareDepth++;
  try{
   bool same=identity.Same(host,candidate,failIdentity);IdentityResult=identity.Result;
   if(State!=Phase.Preparing||!CurrentGeneration()){Decision=PrepareDecision.GenerationOrStateChange;Refusal((int)Decision);Finish("disconnect");return false;}
   if(!same||presignaled){Decision=!same?PrepareDecision.IdentityMismatch:PrepareDecision.Presignaled;Refusal((int)Decision);Finish("before");return false;}
   bool busy=reading||readBusy();Note(ExternalTrace.Stage.Reading,ExternalTrace.Result.Success,busy?1:0);GuardReached=true;string result=Guard.PrepareExternal(busy);
   GuardPrecheck=Guard.ExternalPrecheck;GuardResult=result;
   if(State!=Phase.Preparing||!CurrentGeneration()){Decision=PrepareDecision.GenerationOrStateChange;Refusal((int)Decision);Finish("disconnect");return false;}
   if(result!="ready"){Decision=PrepareDecision.GuardResult;Refusal((int)Decision);Finish("before");return false;}
   deadline=clock()+5000;Decision=PrepareDecision.Ready;State=Phase.Ready;Note(ExternalTrace.Stage.Prepare,ExternalTrace.Result.Success);return true;
  }catch(Exception error){IdentityResult=identity.Result;GuardPrecheck=Guard.ExternalPrecheck;Decision=PrepareDecision.Failed;Refusal((int)Decision);if(Telemetry!=null)Telemetry.Error(error,false);errors.Observe(error,true);Guard.NoteExternalUnknown();Finish("before");return false;}
  finally{
   prepareDepth--;
   if(prepareDepth==0&&deferredFinish!=null){string reason=deferredFinish;deferredFinish=null;Finish(reason);}
  }
 }
 internal void Poll(bool start,bool completion,bool controllerAlive,int currentGeneration){
  Owner();
  int poll=(int)State*16+(start?1:0)+(completion?2:0)+(controllerAlive?4:0);if(poll!=lastPoll){lastPoll=poll;Note(ExternalTrace.Stage.Poll,ExternalTrace.Result.Entered,start?1:0,completion?1:0,controllerAlive?1:0);}
  if(State==Phase.Terminal||State==Phase.Disconnected)return;
  if(currentGeneration!=generation){Refusal(101);Finish("disconnect");return;}
  if(State==Phase.UnknownProtected)return; // Late completion never silently restores certainty.
  if(State==Phase.Ready){
   if(completion||!controllerAlive||clock()>=deadline){Refusal(completion?102:!controllerAlive?103:104);Finish("before");return;}
   if(!start)return;
   bool busyNow=readBusy();Note(ExternalTrace.Stage.Reading,ExternalTrace.Result.Success,busyNow?1:0);if(busyNow||!Guard.CommitExternal()){Refusal(105);Finish("before");return;}
   Note(ExternalTrace.Stage.Commit,ExternalTrace.Result.Success);
   State=Phase.Committed; // Irreversible attempted field belongs to the existing guard.
   try{Note(ExternalTrace.Stage.Ack,ExternalTrace.Result.Entered);ack();AckCount++;Note(ExternalTrace.Stage.Ack,ExternalTrace.Result.Success,AckCount);if(State==Phase.Committed){State=Phase.AckPublished;deadline=clock()+5000;}}
   catch(Exception error){if(Telemetry!=null)Telemetry.Error(error,false);Refusal(106);Unknown();}
   return;
  }
  if(State==Phase.Committed){if(completion||!controllerAlive||clock()>=deadline){Refusal(completion?102:!controllerAlive?103:104);Unknown();}return;}
  if(State==Phase.AckPublished){
   // Completion wins over process-death observation: it proves normal return was observed.
   if(completion){Note(ExternalTrace.Stage.Completion,ExternalTrace.Result.Success);Finish("normal");return;}
   if(!controllerAlive||clock()>=deadline){Refusal(!controllerAlive?103:104);Unknown();}
  }
 }
 internal void Unknown(){Owner();if(State==Phase.Disconnected||State==Phase.Terminal)return;Guard.NoteExternalUnknown();State=Phase.UnknownProtected;Note(ExternalTrace.Stage.Terminal,ExternalTrace.Result.Unknown);}
 internal void Disconnect(){Owner();Finish("disconnect");}
 internal void CapabilityFailure(Exception error){Owner();if(Telemetry!=null)Telemetry.Error(error,false);errors.Observe(error,true);if(Guard.Attempted){Unknown();return;}Guard.NoteExternalUnknown();Finish("before");}
 void Finish(string reason){
  Owner();if(reason=="disconnect")disconnected=true;
  if(State==Phase.Terminal||State==Phase.Disconnected)return;
  if(prepareDepth!=0){State=Phase.Completing;deferredFinish=disconnected?"disconnect":reason;if(disconnected)Guard.Disconnect();return;}
  if(finishing){if(disconnected)Guard.Disconnect();return;}
  finishing=true;Note(ExternalTrace.Stage.ProtocolCleanup,ExternalTrace.Result.Entered,reason=="normal"?1:reason=="disconnect"?2:0);
  State=Phase.Completing;
  try{
   if(reason=="disconnect")Guard.Disconnect();
   else if(reason=="normal")Guard.CompleteExternalNormal();
   else Guard.CancelExternalBeforeStart();
  }catch(Exception error){errors.Observe(error,false);CleanupFailed=true;Guard.NoteExternalUnknown();}finally{
   // Lifecycle and normal completion use this same idempotent owner path.
   if(!released){released=true;ReleaseCount++;try{release();}catch(Exception error){errors.Observe(error,false);CleanupFailed=true;Guard.NoteExternalUnknown();}}
   State=disconnected?Phase.Disconnected:Phase.Terminal;
   if(Telemetry!=null)Telemetry.Errors(errors);Note(ExternalTrace.Stage.Terminal,errors.Unknown?ExternalTrace.Result.Unknown:ExternalTrace.Result.Success,(int)State,ReleaseCount,CleanupFailed?1:0);
   finishing=false;
  }
 }
}

#endif
