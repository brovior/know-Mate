#if !AEGIS_PPT_EXTERNAL_DIAGNOSTIC
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
 internal PptShutdownGuard(object host,int thread,Func<object,IConnectionPoint> resolver):this(host,thread,resolver,PptSinkLoader.Create,null){}
 internal PptShutdownGuard(object host,int thread,Func<object,IConnectionPoint> resolver,Func<PptShutdownState,object> factory):this(host,thread,resolver,factory,null){}
 internal PptShutdownGuard(object host,int thread,Func<object,IConnectionPoint> resolver,Func<PptShutdownState,object> factory,PptLifecycleTrace trace){application=host;mainThread=thread;resolvePoint=resolver;createSink=factory;lifecycle=trace;}
 internal void Disconnect(){disconnected=true;application=null;}
 internal bool Entered {get{return entered;}}
 void Trace(int stage,PptShutdownState state,int detail){if(lifecycle!=null)lifecycle.Record(stage,(state.Cancelled?1:0)|(state.Broken?2:0)|(disconnected?4:0)|(attempted?8:0)|(state.Active?16:0)|(broken?32:0),detail);}
 internal string TryQuitIfIdle(bool reading){int ignored;return TryQuitIfIdleTracked(reading,out ignored);}
 internal string TryQuitIfIdleTracked(bool reading,out int completedToken){
  completedToken=0;
  if(disconnected||broken)return "not_supported";
  if(entered||attempted||reading)return "protected";
  if(Thread.CurrentThread.ManagedThreadId!=mainThread||Thread.CurrentThread.GetApartmentState()!=ApartmentState.STA)return "not_supported";
  int invocation=lifecycle==null?0:lifecycle.Begin();entered=true;
  object host=application,sink=null;var state=new PptShutdownState();var session=new PptConnectionLease(state,()=>disconnected);
  bool setup=true,quitStarted=false,publish=false;string result="failed";
  try{
   result=Execute(host,state,session,ref sink,ref setup,ref quitStarted);
   publish=quitStarted;
  }catch(CountInterrupted exc){result=exc.Outcome;broken|=state.Broken;}
  catch(Exception exc){
   session.Errors.Observe(exc,true);
   if(quitStarted||exc is CountCleanupFailed||exc is PptCleanupOutcomeUnknown)session.Errors.Unknown=true;
   broken|=setup||session.Errors.Poison;
   result=setup?"not_supported":"failed";publish=!setup&&!quitStarted;
  }finally{
   state.Active=false;
   if(session.Subscription==SubscriptionPhase.Confirmed&&!session.Errors.Poison&&!state.Broken&&!disconnected)Trace(12,state,0);
   else Trace(15,state,(session.Subscription!=SubscriptionPhase.Confirmed?1:0)|(session.Errors.Poison?2:0)|(disconnected?4:0));
   session.Complete();broken|=state.Broken||session.Errors.Poison;
   if(session.UnadviseAttempts!=0){if(session.Subscription==SubscriptionPhase.Removed)Trace(13,state,0);else Trace(14,state,session.Errors.Chosen==null?0:session.Errors.Chosen.HResult);}
   Trace(16,state,0);GC.KeepAlive(sink);GC.KeepAlive(host);entered=false;
   if(lifecycle!=null)lifecycle.End(invocation);
  }
  COMException fatal=FatalTransport(session.Errors.StickyFatal);
  if(fatal!=null)throw new COMException("ppt_transport_failed",fatal.ErrorCode);
  if(session.Errors.Unknown)throw new COMException(quitStarted?"ppt_quit_outcome_unknown":"ppt_shutdown_outcome_unknown",unchecked((int)0x80004005));
  if(publish&&lifecycle!=null)completedToken=lifecycle.Complete(invocation);
  return result;
 }
 string Execute(object host,PptShutdownState state,PptConnectionLease session,ref object sink,ref bool setup,ref bool quitStarted){
  // Null resolver is the production route: native metadata, real validator and CP.
  if(resolvePoint==null)session.Resolve(host);else session.AcquireExisting(resolvePoint(host));
  sink=createSink(state);
  if(state.Broken||disconnected){broken|=state.Broken;return "not_supported";}
  session.Advise(sink);setup=false;
  if(disconnected||state.Broken){broken|=state.Broken;return "not_supported";}
  ((dynamic)host).DisplayAlerts=2;
  if(disconnected||state.Broken){broken|=state.Broken;return "not_supported";}
  int alerts=(int)((dynamic)host).DisplayAlerts;
  if(disconnected||state.Broken){broken|=state.Broken;return "not_supported";}
  if(alerts!=2)return "failed";
  state.Active=true;
  Action check=()=>{if(state.Broken||disconnected||!session.OperationsAllowed)throw new CountInterrupted("not_supported");if(state.Cancelled)throw new CountInterrupted("protected");};
  foreach(string collection in new[]{"Presentations","ProtectedViewWindows","SlideShowWindows"})if(RawCount.Read(host,collection,check)!=0)return "protected";
  if(state.Broken){broken=true;return "not_supported";}
  if(disconnected||state.Cancelled)return "protected";
  attempted=true;quitStarted=true;Trace(10,state,0);((dynamic)host).Quit();Trace(11,state,0);
  if(state.Broken){session.Errors.Unknown=true;throw new COMException("ppt_quit_outcome_unknown",unchecked((int)0x80004005));}
  return state.Cancelled?"protected":"quit_requested";
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
