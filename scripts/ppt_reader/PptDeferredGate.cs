#if AEGIS_PPT_DEFERRED_DIAGNOSTIC
using System;
using System.Diagnostics;
using System.Threading;
namespace AegisDesk.PptReader {
// Diagnostic-only one-shot owner-STA gate. No Office references are stored here.
internal sealed class PptDeferredGate {
 internal enum Phase { New, Pending, Consumed, Terminal }
 internal Phase State {get;private set;}
 internal int Depth {get;private set;}
 internal string Outcome {get;private set;}
 readonly int owner=Thread.CurrentThread.ManagedThreadId;
 readonly Func<bool> ownerCheck,signal,alive,valid;
 readonly Func<string> invoke;
 readonly Action cleanup;
 readonly Action<string> record;
 readonly Stopwatch deadline=new Stopwatch();
 bool recording;
 internal PptDeferredGate(Func<bool> sameOwner,Func<bool> signalled,Func<bool> controllerAlive,
  Func<bool> bindingValid,Func<string> guard,Action release,Action<string> logger) {
  ownerCheck=sameOwner;signal=signalled;alive=controllerAlive;valid=bindingValid;
  invoke=guard;cleanup=release;record=logger;
 }
 bool Owner(){return owner==Thread.CurrentThread.ManagedThreadId&&Thread.CurrentThread.GetApartmentState()==ApartmentState.STA&&ownerCheck();}
 void Record(string message){if(recording)return;recording=true;try{record(message);}catch{/* Diagnostics never alter the guard outcome. */}finally{recording=false;}}
 internal void Enter(bool arm) {
  if(!Owner())throw new InvalidOperationException("deferred_wrong_owner");
  Depth++;
  // Preparation services are allowed before Arm; invalidate only a pending request.
  if(!arm&&State==Phase.Pending)Cancel("other_service");
 }
 internal void Leave(){Depth--;}
 internal string Arm() {
  if(!Owner()||Depth!=1||State!=Phase.New){Cancel("invalid_arm");return "not_supported";}
  deadline.Start();
  try {
   if(!valid()||!alive()||signal()){Cancel("invalid_capability_or_presignaled");return "not_supported";}
   State=Phase.Pending;Record("armed");return "armed";
  }catch{Cancel("arm_failed");return "not_supported";}
 }
 internal void Cancel(string reason) {
  if(!Owner())return; // Native lifecycle/COM entries are required to marshal to owner.
  if(State==Phase.Terminal)return;
  if(State==Phase.Consumed){Record("cancel_deferred_"+reason);return;}
  State=Phase.Terminal;Outcome="cancelled";
  try{Record("cancel_"+reason);}finally{Cleanup();}
 }
 void Cleanup(){try{cleanup();}catch{Record("cleanup_failed");}}
 internal void Tick() {
  if(!Owner()||State!=Phase.Pending||Depth!=0)return;
  try {
   if(deadline.ElapsedMilliseconds>=5000||!valid()||!alive()){Cancel("expired_or_disconnected");return;}
   if(!signal())return;
  }catch{Cancel("capability_observation_failed");return;}
  State=Phase.Consumed;
  // Covers guard invocation AND result/exception logging, including reentrancy.
  try {
   Record("callback_entry");
   try {Outcome=invoke();Record("guard_result_"+Outcome);}
   catch(Exception e){Outcome="unknown";Record("guard_exception_"+e.HResult.ToString("X8"));}
  }finally{State=Phase.Terminal;Cleanup();}
 }
}
// Adopted resources only. Never query handle type or retry a CloseHandle here.
internal sealed class PptDeferredResources {
 IntPtr signal,controller;Action stopTimer;readonly Action<IntPtr> close;readonly Action<string> record;
 internal PptDeferredResources(IntPtr eventHandle,IntPtr processHandle,Action stop,Action<IntPtr> release,Action<string> logger) {
  signal=eventHandle;controller=processHandle;stopTimer=stop;close=release;record=logger;
 }
 internal bool Empty {get{return signal==IntPtr.Zero&&controller==IntPtr.Zero&&stopTimer==null;}}
 void Failed(string stage){try{record(stage);}catch{}}
 internal void Dispose() {
  // Detach every capability before any action can throw or reenter.
  IntPtr first=signal,second=controller;Action stop=stopTimer;
  signal=controller=IntPtr.Zero;stopTimer=null;
  try{if(stop!=null)stop();}catch{Failed("timer_cleanup_failed");}
  try{if(first!=IntPtr.Zero)close(first);}catch{Failed("event_cleanup_failed");}
  try{if(second!=IntPtr.Zero&&second!=first)close(second);}catch{Failed("controller_cleanup_failed");}
 }
}
}
#endif
