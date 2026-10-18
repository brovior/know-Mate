// Explicitly owned event connection references; borrowed host RCWs are never released.
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Runtime.ExceptionServices;
namespace AegisDesk.PptReader {
enum CompletionPhase { Open, Requested, Running, Done }
enum SubscriptionPhase { None, AdviseStarted, Confirmed, Refused, CleanupUnconfirmed, OutcomeUnknown, UnadviseStarted, Removed }
enum PointerPhase { Owned, ReleaseStarted, Returned, OutcomeUnknown }
enum PointerKind { HostUnknown, Container, ConnectionPoint, SinkUnknown }

sealed class PptConnectionLease {
 internal static readonly Guid CpcIid=new Guid("B196B284-BAB4-101A-B69C-00AA00341D07"),EventIid=new Guid("914934C2-5A91-11CF-8700-00AA0060263B");
 [UnmanagedFunctionPointer(CallingConvention.StdCall)]public delegate int FindProc(IntPtr p,ref Guid iid,out IntPtr result);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)]public delegate int AdviseProc(IntPtr p,IntPtr sink,out int cookie);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)]public delegate int UnadviseProc(IntPtr p,int cookie);
 internal sealed class Acquisition {
  internal readonly IntPtr Pointer;internal readonly PointerKind Kind;
  internal PointerPhase Phase;internal int Attempts;
  internal Acquisition(IntPtr p,PointerKind k){Pointer=p;Kind=k;}
 }
 readonly List<Acquisition> owned=new List<Acquisition>();
 readonly PptShutdownState state;
 readonly Func<bool> disconnected;
 internal readonly PptCleanupErrors Errors=new PptCleanupErrors();
 internal CompletionPhase Completion {get;private set;}
 internal SubscriptionPhase Subscription {get;private set;}
 internal int Cookie {get;private set;}
 // Diagnostic output is never used as an Unadvise capability.
 internal int ObservedCookie {get;private set;}
 internal int? AdviseResult {get;private set;}
 internal int UnadviseAttempts {get;private set;}
 internal int AdviseAttempts {get;private set;}
 internal IEnumerable<Acquisition> Acquisitions {get{return owned.AsReadOnly();}}
 Acquisition point;object sink;int nativeDepth;
#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
 internal ExternalTrace Telemetry;
 void TraceStep(int step,int result,int detail=0){if(Telemetry!=null)Telemetry.Record(ExternalTrace.Stage.Resolve,(ExternalTrace.Result)result,step,detail);}
#endif
 internal PptConnectionLease(PptShutdownState s,Func<bool> gone){state=s;disconnected=gone;}
 internal bool OperationsAllowed {get{return Completion==CompletionPhase.Open&&!Errors.Poison&&!state.Broken&&!disconnected();}}
 static T Slot<T>(IntPtr p,int index)where T:class {
  var d=Marshal.GetDelegateForFunctionPointer(Marshal.ReadIntPtr(Marshal.ReadIntPtr(p),index*IntPtr.Size),typeof(T));
  if(d.GetType()!=typeof(T))d=Delegate.CreateDelegate(typeof(T),d.Target,d.Method);
  return d as T;
 }
 Acquisition Own(IntPtr p,PointerKind kind){if(p==IntPtr.Zero)return null;var a=new Acquisition(p,kind);owned.Add(a);return a;}
 void RequireOperation(){if(!OperationsAllowed)throw new InvalidOperationException("operation_after_completion_or_failure");}
 void ExitNative(){nativeDepth--;if(nativeDepth==0&&Completion==CompletionPhase.Requested)Drain();}
 void Clean(Acquisition a){
  if(a==null||a.Phase!=PointerPhase.Owned)return;
  a.Phase=PointerPhase.ReleaseStarted;a.Attempts++;nativeDepth++;
  try{
#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
   if(Telemetry!=null)Telemetry.Record(ExternalTrace.Stage.Release,ExternalTrace.Result.Entered,(int)a.Kind,a.Attempts);
#endif
   Marshal.Release(a.Pointer);a.Phase=PointerPhase.Returned;
#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
   if(Telemetry!=null)Telemetry.Record(ExternalTrace.Stage.Release,ExternalTrace.Result.Success,(int)a.Kind,a.Attempts);
#endif
  }
  catch(Exception e){
#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
   if(Telemetry!=null){Telemetry.Record(ExternalTrace.Stage.Release,ExternalTrace.Result.Failed,(int)a.Kind,e.HResult);Telemetry.Error(e,true);}
#endif
   a.Phase=PointerPhase.OutcomeUnknown;Errors.Unknown=true;Errors.Observe(e,false);state.MarkBroken();}
  finally{ExitNative();}
 }
 internal void Resolve(object host){
  RequireOperation();
#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
  TraceStep(1,0);
#endif
  PptMetadataScope.Validate(host);RequireOperation();
#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
  TraceStep(1,1);
#endif
  Acquisition unknown=null,container=null;nativeDepth++;
  try{
   unknown=Own(Marshal.GetIUnknownForObject(host),PointerKind.HostUnknown);RequireOperation();
   Guid iid=CpcIid;IntPtr p;
#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
   TraceStep(2,0);
#endif
   int h=Marshal.QueryInterface(unknown.Pointer,ref iid,out p);

#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
   TraceStep(2,h<0?3:1,h);
#endif
   container=Own(p,PointerKind.Container);Marshal.ThrowExceptionForHR(h);RequireOperation();

#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
   TraceStep(3,0);
#endif
   iid=EventIid;h=Slot<FindProc>(container.Pointer,4)(container.Pointer,ref iid,out p);

#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
   TraceStep(3,h<0?3:1,h);
#endif
   point=Own(p,PointerKind.ConnectionPoint);Marshal.ThrowExceptionForHR(h);
   if(point==null)throw new COMException("ppt_event_connection_missing");
  }catch(Exception e){Errors.Observe(e,true);throw;}
  finally{Clean(container);Clean(unknown);ExitNative();}
  Errors.Throw();RequireOperation();
 }
 // The resolver overload borrows an existing interface and owns only this new acquisition.
 internal void AcquireExisting(System.Runtime.InteropServices.ComTypes.IConnectionPoint value){
  RequireOperation();if(point!=null)throw new InvalidOperationException("point_already_acquired");
  point=Own(Marshal.GetComInterfaceForObject(value,typeof(System.Runtime.InteropServices.ComTypes.IConnectionPoint)),PointerKind.ConnectionPoint);
  GC.KeepAlive(value);
 }
 internal void Advise(object target){
  RequireOperation();
  if(point==null||point.Phase!=PointerPhase.Owned||Subscription!=SubscriptionPhase.None)
   throw new InvalidOperationException("advise_requires_owned_point_and_no_prior_attempt");
  sink=target;Acquisition temp=null;int cookie=0;nativeDepth++;
  try{
   temp=Own(Marshal.GetIUnknownForObject(target),PointerKind.SinkUnknown);RequireOperation();
   Subscription=SubscriptionPhase.AdviseStarted;AdviseAttempts++;
   int h=Slot<AdviseProc>(point.Pointer,5)(point.Pointer,temp.Pointer,out cookie);
   AdviseResult=h;ObservedCookie=cookie;
#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
   if(Telemetry!=null)Telemetry.Record(ExternalTrace.Stage.Advise,h==0&&cookie!=0?ExternalTrace.Result.Success:cookie==0&&DocumentedRefusal(h)?ExternalTrace.Result.Refused:ExternalTrace.Result.Unknown,h,cookie==0?0:1);
#endif
   // Only exact S_OK plus a nonzero DWORD cookie grants an Unadvise capability.
   if(h==0&&cookie!=0){Cookie=cookie;Subscription=SubscriptionPhase.Confirmed;}
   else if(cookie==0&&DocumentedRefusal(h)){
    Subscription=SubscriptionPhase.Refused;
    throw new COMException("ppt_advise_refused",h);
   }
   else{Subscription=SubscriptionPhase.OutcomeUnknown;Errors.Unknown=true;state.MarkBroken();
    throw new COMException("ppt_advise_outcome_unknown",h<0?h:unchecked((int)0x80004005));}
  }catch(Exception e){
   ObservedCookie=cookie;
   if(Subscription==SubscriptionPhase.AdviseStarted){Subscription=SubscriptionPhase.OutcomeUnknown;Errors.Unknown=true;state.MarkBroken();}
   Errors.Observe(e,true);
  }finally{Clean(temp);ExitNative();GC.KeepAlive(target);}
  Errors.Throw();RequireOperation();
 }
 static bool DocumentedRefusal(int h){
  // IConnectionPoint::Advise documented failure returns, with a zero cookie.
  // Transport failures, call exceptions and inconsistent outputs remain unknown.
  switch(unchecked((uint)h)){
   case 0x80040201:case 0x80040202:case 0x80004003:case 0x8007000E:case 0x8000FFFF:return true;
   default:return false;
  }
 }
 // Owner STA only. A reentrant caller sees Requested/Running before any native cleanup.
 internal void Complete(){
  if(Completion!=CompletionPhase.Open)return;
  Completion=CompletionPhase.Requested;state.Active=false;
  if(nativeDepth==0)Drain();
 }
 void Drain(){
  if(Completion!=CompletionPhase.Requested||nativeDepth!=0)return;
  Completion=CompletionPhase.Running;
  try{
   if(Subscription==SubscriptionPhase.Confirmed&&!Errors.Poison&&!state.Broken&&!disconnected()){
    Subscription=SubscriptionPhase.UnadviseStarted;UnadviseAttempts++;nativeDepth++;
    try{
#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
     if(Telemetry!=null)Telemetry.Record(ExternalTrace.Stage.Unadvise,ExternalTrace.Result.Entered);
#endif
     int h=Slot<UnadviseProc>(point.Pointer,6)(point.Pointer,Cookie);
     if(h!=0)throw new COMException("ppt_unadvise_outcome_unknown",h<0?h:unchecked((int)0x80004005));
     Subscription=SubscriptionPhase.Removed;
    }catch(Exception e){
#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
     if(Telemetry!=null){Telemetry.Record(ExternalTrace.Stage.Unadvise,ExternalTrace.Result.Failed,e.HResult);Telemetry.Error(e,true);}
#endif
     Subscription=SubscriptionPhase.OutcomeUnknown;Errors.Unknown=true;Errors.Observe(e,false);state.MarkBroken();}
    finally{ExitNative();}
   }
   if(Subscription==SubscriptionPhase.Confirmed&&disconnected()&&!Errors.Poison&&!state.Broken)
    Subscription=SubscriptionPhase.CleanupUnconfirmed;
   if(Subscription==SubscriptionPhase.Confirmed||Subscription==SubscriptionPhase.AdviseStarted||
      Subscription==SubscriptionPhase.UnadviseStarted||Subscription==SubscriptionPhase.OutcomeUnknown){Errors.Unknown=true;state.MarkBroken();}
   for(int i=owned.Count-1;i>=0;i--)Clean(owned[i]);
  }finally{state.Active=false;GC.KeepAlive(sink);Completion=CompletionPhase.Done;}
 }
}

sealed class PptCleanupErrors {
 internal Exception Primary,Cleanup,StickyFatal;internal bool Unknown;
 internal void Observe(Exception e,bool primary){if(e==null)return;
#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
 if(ExternalTrace.Current!=null)ExternalTrace.Current.Error(e,!primary);
#endif
if(PptNativeMetadata.Fatal(e)&&StickyFatal==null)StickyFatal=e;if(primary){if(Primary==null)Primary=e;}else if(Cleanup==null||PptNativeMetadata.Fatal(e)&&!PptNativeMetadata.Fatal(Cleanup))Cleanup=e;}
 internal Exception Chosen {get{return StickyFatal??Primary??Cleanup;}}
 internal bool Poison {get{return StickyFatal!=null||Unknown;}}
 internal void Throw(){if(Unknown&&StickyFatal==null)throw new PptCleanupOutcomeUnknown(Chosen);if(Chosen!=null)ExceptionDispatchInfo.Capture(Chosen).Throw();}
}
class PptCleanupOutcomeUnknown:Exception {internal PptCleanupOutcomeUnknown(Exception cause):base("owned_cleanup_outcome_unknown",cause){}}

}
