#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
// A synchronous owner-STA request owns only its explicit native acquisitions.
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Threading;
namespace AegisDesk.PptReader {
internal sealed class PptShutdownGuard {
 object application;readonly PptLifecycleTrace lifecycle;readonly int mainThread;
 readonly Func<object,IConnectionPoint> resolvePoint;readonly Func<PptShutdownState,object> createSink;
 bool disconnected,entered,attempted,broken;
 internal ExternalTrace Telemetry;
 void Note(ExternalTrace.Stage stage,ExternalTrace.Result result,int a=0,int b=0,int c=0){if(Telemetry!=null)Telemetry.Record(stage,result,a,b,c);}
 void Refuse(int reason,PptShutdownState state=null){if(Telemetry!=null)Telemetry.Refuse(ExternalTrace.Stage.Guard,reason,(disconnected?1:0)|(broken?2:0)|(entered?4:0)|(attempted?8:0)|(state!=null&&state.Cancelled?16:0)|(state!=null&&state.Broken?32:0),externalPrepareDepth);}
 void LeaseSnapshot(PptConnectionLease session){try{if(Telemetry==null)return;Telemetry.Errors(session.Errors);Note(ExternalTrace.Stage.Unadvise,session.UnadviseAttempts==0?ExternalTrace.Result.Skipped:session.Subscription==SubscriptionPhase.Removed?ExternalTrace.Result.Success:ExternalTrace.Result.Unknown,(int)session.Subscription,session.UnadviseAttempts);foreach(var acquisition in session.Acquisitions)Note(ExternalTrace.Stage.Release,acquisition.Phase==PointerPhase.Returned?ExternalTrace.Result.Success:acquisition.Attempts==0?ExternalTrace.Result.Skipped:ExternalTrace.Result.Unknown,(int)acquisition.Kind,(int)acquisition.Phase,acquisition.Attempts);}catch{/* Telemetry failure cannot change cleanup. */}}
 internal PptShutdownGuard(object host,int thread,Func<object,IConnectionPoint> resolver):this(host,thread,resolver,PptSinkLoader.Create,null){}
 internal PptShutdownGuard(object host,int thread,Func<object,IConnectionPoint> resolver,Func<PptShutdownState,object> factory):this(host,thread,resolver,factory,null){}
 internal PptShutdownGuard(object host,int thread,Func<object,IConnectionPoint> resolver,Func<PptShutdownState,object> factory,PptLifecycleTrace trace){application=host;mainThread=thread;resolvePoint=resolver;createSink=factory;lifecycle=trace;}
 internal void Disconnect(){RequireExternalOwner();disconnected=true;application=null;if(externalSession!=null)FinishExternal();}
 internal bool Entered {get{return entered;}}
 void Trace(int stage,PptShutdownState state,int detail){if(lifecycle!=null)lifecycle.Record(stage,(state.Cancelled?1:0)|(state.Broken?2:0)|(disconnected?4:0)|(attempted?8:0)|(state.Active?16:0)|(broken?32:0),detail);}
 internal string TryQuitIfIdle(bool reading){int ignored;return TryQuitIfIdleTracked(reading,out ignored);}
 internal string TryQuitIfIdleTracked(bool reading,out int completedToken){
  completedToken=0;
  if(disconnected||broken)return "not_supported";
  if(entered||attempted||reading)return "protected";
  if(Thread.CurrentThread.ManagedThreadId!=mainThread||Thread.CurrentThread.GetApartmentState()!=ApartmentState.STA)return "not_supported";
  int invocation=lifecycle==null?0:lifecycle.Begin();entered=true;
  object host=application,sink=null;var state=new PptShutdownState();state.Telemetry=Telemetry;var session=new PptConnectionLease(state,()=>disconnected);session.Telemetry=Telemetry;
  bool setup=true,quitStarted=false,publish=false;string result="failed";
  try{
   result=Execute(host,state,session,ref sink,ref setup,ref quitStarted,true);
   publish=quitStarted;
  }catch(CountInterrupted exc){result=exc.Outcome;broken|=state.Broken;}
  catch(Exception exc){
   session.Errors.Observe(exc,true);
   if(quitStarted||exc is CountCleanupFailed||exc is PptCleanupOutcomeUnknown)session.Errors.Unknown=true;
   broken|=setup||session.Errors.Poison;
   result=setup?"not_supported":"failed";publish=!setup&&!quitStarted;
  }finally{
   CompleteSession(host,sink,state,session,invocation);
  }
  COMException fatal=FatalTransport(session.Errors.StickyFatal);
  if(fatal!=null)throw new COMException("ppt_transport_failed",fatal.ErrorCode);
  if(session.Errors.Unknown)throw new COMException(quitStarted?"ppt_quit_outcome_unknown":"ppt_shutdown_outcome_unknown",unchecked((int)0x80004005));
  if(publish&&lifecycle!=null)completedToken=lifecycle.Complete(invocation);
  return result;
 }
 string Execute(object host,PptShutdownState state,PptConnectionLease session,ref object sink,ref bool setup,ref bool quitStarted,bool performQuit){
  var previous=ExternalTrace.Current;ExternalTrace.Current=Telemetry;
  try{return ExecuteCore(host,state,session,ref sink,ref setup,ref quitStarted,performQuit);}finally{ExternalTrace.Current=previous;}
 }
 string ExecuteCore(object host,PptShutdownState state,PptConnectionLease session,ref object sink,ref bool setup,ref bool quitStarted,bool performQuit){
  // Null resolver is the production route: native metadata, real validator and CP.
  Note(ExternalTrace.Stage.Resolve,ExternalTrace.Result.Entered);if(resolvePoint==null)session.Resolve(host);else session.AcquireExisting(resolvePoint(host));Note(ExternalTrace.Stage.Resolve,ExternalTrace.Result.Success);
  Note(ExternalTrace.Stage.Sink,ExternalTrace.Result.Entered);sink=createSink(state);Note(ExternalTrace.Stage.Sink,ExternalTrace.Result.Success);
  if(state.Broken||disconnected){Refuse(202,state);broken|=state.Broken;return "not_supported";}
  Note(ExternalTrace.Stage.Advise,ExternalTrace.Result.Entered);session.Advise(sink);setup=false;Note(ExternalTrace.Stage.Advise,ExternalTrace.Result.Success,(int)session.Subscription,session.AdviseResult??0);
  if(disconnected||state.Broken){Refuse(203,state);broken|=state.Broken;return "not_supported";}
  Note(ExternalTrace.Stage.AlertsSet,ExternalTrace.Result.Entered);((dynamic)host).DisplayAlerts=2;Note(ExternalTrace.Stage.AlertsSet,ExternalTrace.Result.Success,2);
  if(disconnected||state.Broken){Refuse(203,state);broken|=state.Broken;return "not_supported";}
  Note(ExternalTrace.Stage.AlertsRead,ExternalTrace.Result.Entered);int alerts=(int)((dynamic)host).DisplayAlerts;Note(ExternalTrace.Stage.AlertsRead,ExternalTrace.Result.Success,alerts);
  if(disconnected||state.Broken){Refuse(203,state);broken|=state.Broken;return "not_supported";}
  if(alerts!=2){Refuse(201,state);return "failed";}
  state.Active=true;
  Action check=()=>{if(state.Broken||disconnected||!session.OperationsAllowed)throw new CountInterrupted("not_supported");if(state.Cancelled)throw new CountInterrupted("protected");};
  int index=0;foreach(string collection in new[]{"Presentations","ProtectedViewWindows","SlideShowWindows"}){var stage=(ExternalTrace.Stage)((int)ExternalTrace.Stage.Presentations+index++);Note(stage,ExternalTrace.Result.Entered);int count=RawCount.Read(host,collection,check);Note(stage,ExternalTrace.Result.Success,count);if(count!=0){Refuse(210+index,state);return "protected";}}
  if(state.Broken){Refuse(204,state);broken=true;return "not_supported";}
  if(disconnected||state.Cancelled){Refuse(205,state);return "protected";}
  if(!performQuit)return "ready";
  attempted=true;quitStarted=true;Trace(10,state,0);Note(ExternalTrace.Stage.Quit,ExternalTrace.Result.Entered);((dynamic)host).Quit();Note(ExternalTrace.Stage.Quit,ExternalTrace.Result.Success);Trace(11,state,0);
  if(state.Broken){session.Errors.Unknown=true;throw new COMException("ppt_quit_outcome_unknown",unchecked((int)0x80004005));}
  return state.Cancelled?"protected":"quit_requested";
 }

 // Explicit external diagnostic build: the same attempted/entered fields reserve both paths.
 PptConnectionLease externalSession;PptShutdownState externalState;object externalHost,externalSink;int externalInvocation;
 int externalPrepareDepth;bool externalFinishPending;
 internal PptShutdownState ExternalState {get{return externalState;}}
 internal bool Attempted {get{return attempted;}}
 internal int ExternalCleanups {get;private set;}
 internal string ExternalOutcome {get;private set;}
 [Flags]internal enum PreparePrecheck { NotRun=0,Passed=1,Disconnected=2,Broken=4,Entered=8,Attempted=16,Reading=32 }
 internal PreparePrecheck ExternalPrecheck {get;private set;}
 void RequireExternalOwner(){if(Thread.CurrentThread.ManagedThreadId!=mainThread||Thread.CurrentThread.GetApartmentState()!=ApartmentState.STA)throw new InvalidOperationException("owner_sta");}
 internal string PrepareExternal(bool reading){
  RequireExternalOwner();Note(ExternalTrace.Stage.Guard,ExternalTrace.Result.Entered,(disconnected?1:0)|(broken?2:0)|(entered?4:0)|(attempted?8:0)|(reading?16:0));
  if(disconnected||broken){ExternalPrecheck=(disconnected?PreparePrecheck.Disconnected:0)|(broken?PreparePrecheck.Broken:0);Refuse((int)ExternalPrecheck);return "not_supported";}
  if(entered||attempted||reading){ExternalPrecheck=(entered?PreparePrecheck.Entered:0)|(attempted?PreparePrecheck.Attempted:0)|(reading?PreparePrecheck.Reading:0);Refuse((int)ExternalPrecheck);return "protected";}
  ExternalPrecheck=PreparePrecheck.Passed;
  entered=true;externalInvocation=lifecycle==null?0:lifecycle.Begin();
  object host=application,sink=null;var state=new PptShutdownState();state.Telemetry=Telemetry;var session=new PptConnectionLease(state,()=>disconnected);session.Telemetry=Telemetry;
  externalHost=host;externalState=state;externalSession=session;
  bool setup=true,started=false;string result="not_supported";externalPrepareDepth++;
  try{
   result=Execute(host,state,session,ref sink,ref setup,ref started,false);
  }catch(CountInterrupted error){Refuse(error.Outcome=="protected"?2061:2062,state);result=error.Outcome;broken|=state.Broken;}
  catch(Exception error){
   if(Telemetry!=null){Refuse(207,state);Telemetry.Error(error,false);}session.Errors.Observe(error,true);
   if(error is CountCleanupFailed||error is PptCleanupOutcomeUnknown)session.Errors.Unknown=true;
   broken|=setup||session.Errors.Poison;result=setup?"not_supported":"failed";
  }finally{
   externalSink=sink;externalPrepareDepth--;
   if(disconnected||externalFinishPending||result!="ready"){
    ExternalOutcome=result=="ready"?"not_supported":result;
    FinishExternal();
   }
  }
  LeaseSnapshot(session);Note(ExternalTrace.Stage.Guard,result=="ready"?ExternalTrace.Result.Success:ExternalTrace.Result.Refused,(int)ExternalPrecheck,state.Cancelled?1:0,state.Broken?1:0);ThrowExternalErrors(session);
  return disconnected||(result=="ready"&&externalSession!=session)?"not_supported":result;
 }
 internal bool CommitExternal(){
  RequireExternalOwner();
  Note(ExternalTrace.Stage.Commit,ExternalTrace.Result.Entered,(externalSession==null?1:0)|(!entered?2:0)|(attempted?4:0)|(broken?8:0)|(disconnected?16:0)|(externalState!=null&&externalState.Broken?32:0)|(externalState!=null&&externalState.Cancelled?64:0));
  if(externalSession==null||!entered||attempted||broken||disconnected||externalState.Broken||externalState.Cancelled)return false;
  attempted=true;return true;
 }
 internal void NoteExternalUnknown(){RequireExternalOwner();broken=true;ExternalOutcome="unknown";}
 internal void CompleteExternalNormal(){
  RequireExternalOwner();if(externalSession==null)return;
  ExternalOutcome=externalState.Cancelled?"cancelled_return":"normal_return";FinishExternal();
 }
 internal void CancelExternalBeforeStart(){RequireExternalOwner();if(externalSession==null)return;if(attempted)throw new InvalidOperationException("reserved_cannot_cancel");ExternalOutcome="cancelled_before_start";FinishExternal();}
 void FinishExternal(){
  RequireExternalOwner();if(externalSession==null)return;
  if(externalPrepareDepth!=0){externalFinishPending=true;return;}
  var session=externalSession;var state=externalState;object host=externalHost,sink=externalSink;int invocation=externalInvocation;
  // Detach before cleanup reentrancy, sharing the normal guard's cleanup implementation.
  externalSession=null;externalHost=externalSink=null;externalFinishPending=false;ExternalCleanups++;
  try{CompleteSession(host,sink,state,session,invocation);}
  catch(Exception error){session.Errors.Unknown=true;session.Errors.Observe(error,false);}
  finally{entered=false;}
  RecordExternalCleanupOutcome(session,state);
  ThrowExternalErrors(session);
 }
 internal void RecordExternalCleanupOutcome(PptConnectionLease session,PptShutdownState state){
  if(session.Errors.Poison||session.Errors.Unknown||state.Broken){broken=true;ExternalOutcome="unknown";}
  else if(disconnected&&session.Subscription==SubscriptionPhase.CleanupUnconfirmed)ExternalOutcome="disconnect_cleanup_unconfirmed";
 }
 static void ThrowExternalErrors(PptConnectionLease session){
  COMException fatal=FatalTransport(session.Errors.StickyFatal);
  if(fatal!=null)throw new COMException("ppt_transport_failed",fatal.ErrorCode);
  if(session.Errors.Unknown)throw new PptCleanupOutcomeUnknown(session.Errors.Chosen);
 }
 void CompleteSession(object host,object sink,PptShutdownState state,PptConnectionLease session,int invocation){
   state.Active=false;
   if(session.Subscription==SubscriptionPhase.Confirmed&&!session.Errors.Poison&&!state.Broken&&!disconnected)Trace(12,state,0);
   else Trace(15,state,(session.Subscription!=SubscriptionPhase.Confirmed?1:0)|(session.Errors.Poison?2:0)|(disconnected?4:0));
   session.Complete();LeaseSnapshot(session);Note(ExternalTrace.Stage.Cancel,ExternalTrace.Result.Success,state.Cancelled?1:0,state.Broken?1:0);broken|=state.Broken||session.Errors.Poison;
   if(session.UnadviseAttempts!=0){if(session.Subscription==SubscriptionPhase.Removed)Trace(13,state,0);else Trace(14,state,session.Errors.Chosen==null?0:session.Errors.Chosen.HResult);}
   Trace(16,state,0);GC.KeepAlive(sink);GC.KeepAlive(host);entered=false;
   if(lifecycle!=null)lifecycle.End(invocation);
 }
        private static COMException FatalTransport(Exception error)
        {
            for (int depth = 0; error != null && depth < PptNativeMetadata.FatalDepth; depth++, error = error.InnerException)
            {
                COMException com = error as COMException;
                if (com == null) continue;
                switch (unchecked((uint)com.ErrorCode))
                {
                    case 0x800706BA: case 0x800706BE: case 0x800706BF: case 0x80010006:
                    case 0x80010007: case 0x80010012: case 0x80010108: case 0x800401FD: return com;
                }
            }
            return null;
        }
}
}

#endif
