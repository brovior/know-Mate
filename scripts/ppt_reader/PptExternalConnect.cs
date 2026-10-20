#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
using System;
namespace AegisDesk.PptReader {
// Isolated diagnostic dependencies. Not a new public COM interface.
internal sealed class ExternalDiagnosticServices {
 internal readonly ProcessFence Fence;internal readonly Func<bool> ValidateCapabilities;
 internal readonly Func<long> Clock;internal readonly Action Ack;private readonly DiagnosticResources resources;
 internal void Release(){resources.Dispose();}
 internal readonly IdentityProbe Identity;
 internal ExternalDiagnosticServices(ProcessFence fence,Func<bool> validate,Func<long> clock,Action ack,Action release,IdentityProbe identity){Fence=fence;ValidateCapabilities=validate;Clock=clock;Ack=ack;resources=new DiagnosticResources(release);Identity=identity;}
}
public sealed partial class Connect {
 internal readonly System.Collections.Generic.List<DiagnosticOperation> DiagnosticOperations=new System.Collections.Generic.List<DiagnosticOperation>();
 DiagnosticOperation NewDiagnosticOperation(){var op=new DiagnosticOperation();if(DiagnosticOperations.Count==16)DiagnosticOperations.RemoveAt(0);DiagnosticOperations.Add(op);return op;}
 const string ExternalOptIn="AEGIS_PPT_EXTERNAL_DIAGNOSTIC";
 private readonly ExternalTrace externalTrace=new ExternalTrace();
 private ExternalDiagnosticServices diagnostic;
 private ExternalProtocol external;
 private int externalGeneration;
 // Bootstrap binds once after native capabilities are adopted.
 internal void BindExternalDiagnostic(ExternalDiagnosticServices services){RequireHostThread();externalTrace.Start();diagnostic=services;services.Fence.Telemetry=externalTrace;services.Identity.Trace=externalTrace;if(shutdown!=null)shutdown.Telemetry=externalTrace;}
 static bool NormalFenceOpen(out string reason){
  reason="diagnostic_fence_used";string value;
  try{EnvRead read=new NativeEnvironment().Read(out value);if(read==EnvRead.Missing)return true;if(read==EnvRead.Failure)reason="diagnostic_fence_unavailable";return false;}
  catch{reason="diagnostic_fence_unavailable";return false;}
 }
 bool ClaimDiagnostic(ExternalDiagnosticServices services,int generation,PptShutdownGuard guard,out string reason){
  reason="capture_transport_unavailable";if(!externalTrace.CaptureHealthy)return false;
  reason="diagnostic_binding_missing";
  if(services==null)return false;
  // Only the success of this call authorizes its continuation; no Claimed field/token lookup.
  if(!services.Fence.TryClaim(out reason))return false;
  bool valid;
  try{valid=services.ValidateCapabilities();}
  catch{reason=generation!=externalGeneration?"diagnostic_generation_changed":"diagnostic_capability_failed";return false;}
  if(generation!=externalGeneration||application==null||!Object.ReferenceEquals(shutdown,guard)||!Object.ReferenceEquals(diagnostic,services)){reason="diagnostic_generation_changed";return false;}
  if(!valid){reason="diagnostic_capability_rejected";return false;}
  return true;
 }
 bool AllowNormalShutdown(out string reason,out ExternalDiagnosticServices owned){
  owned=null;
  if(!ObservationFence.Open){reason="identity_observation_protected";return false;}
  if(!NormalFenceOpen(out reason)){if(external==null)owned=diagnostic;return false;}
  string optIn=Environment.GetEnvironmentVariable(ExternalOptIn);
  string deferredOptIn=Environment.GetEnvironmentVariable("AEGIS_PPT_DEFERRED_DIAGNOSTIC");
  // Ordinary reader hosts never claim or require VerifiedWindow/PID/creation.
  bool ordinary=String.IsNullOrEmpty(optIn)||optIn=="0",ordinaryDeferred=String.IsNullOrEmpty(deferredOptIn)||deferredOptIn=="0";
  if(ordinary&&ordinaryDeferred){if(bootstrapCaps==null)return true;owned=diagnostic;reason="diagnostic_disabled";return false;}
  owned=diagnostic;
  if(!ordinary&&optIn!="1"||!ordinaryDeferred&&deferredOptIn!="1"){reason="diagnostic_optin_invalid";return false;}
  owned=diagnostic;return ClaimDiagnostic(diagnostic,externalGeneration,shutdown,out reason);
 }
 internal string BeginExternalDiagnostic(object candidate){
  RequireHostThread();
  if(!ShutdownBuildPolicy.Enabled||Environment.GetEnvironmentVariable(ExternalOptIn)!="1")return "diagnostic_disabled";
  int generation=externalGeneration;var guard=shutdown;var services=diagnostic;
  if(guard==null)return "not_supported";string reason;
  if(external!=null)return "external_session_exists";
  bool handedOff=false;return NewDiagnosticOperation().Run(()=>{
  if(!ClaimDiagnostic(services,generation,guard,out reason)){externalTrace.Refuse(ExternalTrace.Stage.Admission,TraceReason(reason));return reason;}
  externalTrace.Record(ExternalTrace.Stage.Admission,ExternalTrace.Result.Success,generation);
  if(!ReaderFence.Begin()){externalTrace.Refuse(ExternalTrace.Stage.Reading,1);return "external_reader_fence_failed";}
  // The session is created only by the private continuation of this successful claim.
  var session=new ExternalProtocol(guard,generation,services.Clock,services.Ack,services.Release,()=>externalGeneration,()=>reading);
  session.Telemetry=externalTrace;external=session;handedOff=true;
  bool ready=session.Prepare(application,candidate,services.Identity,false,false,reading);
  ReaderFence.Resolve(session);
  if(!ready)return session.Error==null?"external_refused":"external_failed";
  if(generation!=externalGeneration||!Object.ReferenceEquals(external,session)){session.Disconnect();return "external_disconnected";}
  return "ready";
  },()=>{if(!handedOff&&services!=null)services.Release();});
 }
 internal void PollExternalDiagnostic(bool start,bool completion,bool controllerAlive){
  RequireHostThread();var session=external;if(session!=null){session.Poll(start,completion,controllerAlive,externalGeneration);ReaderFence.Resolve(session);}
 }
 void InvalidateExternalSession(){
  externalGeneration++;externalTrace.Generation(externalGeneration);
  var session=external;var services=diagnostic;var guard=shutdown;
  var timer=bootstrapTimer;
  external=null;diagnostic=null;shutdown=null;application=null;mainThread=0;bootstrapTimer=null;
#if AEGIS_PPT_DEFERRED_DIAGNOSTIC
  var pending=deferred;deferred=null;connectionGeneration++;
#endif
  var operation=NewDiagnosticOperation();
  operation.Cleanup(()=>DisposeBootstrapTimer(timer));
  if(session!=null)operation.Cleanup(()=>{session.Disconnect();if(session.Error!=null)throw session.Error;});
  else if(services!=null)operation.Cleanup(services.Release);
#if AEGIS_PPT_DEFERRED_DIAGNOSTIC
  if(pending!=null)operation.Cleanup(pending.Cancel);
#endif
  if(guard!=null)operation.Cleanup(()=>{guard.Disconnect();
#if AEGIS_BOOTSTRAP_CLEANUP_TEST
   BootstrapCleanupFaults.AfterGuardDisconnect();
#endif
  });
  externalTrace.Errors(operation.Errors);externalTrace.Record(ExternalTrace.Stage.Disconnect,ExternalTrace.Result.Success,externalGeneration);
  // Lifecycle cleanup records failures but must never abort the caller's remaining work.
 }
}
}

#endif
