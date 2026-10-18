#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
using System;
using System.Diagnostics;
using System.Runtime.InteropServices;
namespace AegisDesk.PptReader {
[ComVisible(true),Guid("E6FE750F-7FE5-43DF-B409-B866555C9299"),InterfaceType(ComInterfaceType.InterfaceIsIDispatch)]
public interface IPptExternalBootstrap {
 [DispId(1)]int ProtocolVersion();
 [DispId(2)]string ExtractSlide(string path,int slide);
 [DispId(3)]string ReaderVersion();
 [DispId(4)]string TryQuitIfIdle();
 [DispId(5)]string ArmDeferredQuit(int pid,string creation,string nonce);
 [DispId(6)]string BindExternalBootstrap(int pid,string creation,string nonce,long window);
 [DispId(7)]string PrepareExternalBootstrap(string nonce,[MarshalAs(UnmanagedType.IDispatch)]object candidate);
 [DispId(8)]string ExternalBootstrapStatus();
 [DispId(9)]string ObserveExternalIdentity(string nonce,[MarshalAs(UnmanagedType.IDispatch)]object candidate);
}
public sealed partial class Connect {
 const string BootstrapKey="AEGIS_PPT_BOOTSTRAP_USED_V1";
 private NativeCapabilities bootstrapCaps;
 private IdentityProbe bootstrapIdentity;
 private string bootstrapNonce;
 private int bootstrapGeneration;
 private System.Windows.Forms.Timer bootstrapTimer;
 private readonly Stopwatch bootstrapClock=Stopwatch.StartNew();
internal static int TraceReason(string reason){try{
  string[] names={"bound","ready","diagnostic_disabled","owner_rejected","binding_rejected","bootstrap_consumed","bootstrap_latch_failed","capability_invalid","capability_failure","bootstrap_missing","external_session_exists","bootstrap_binding_rejected","not_supported","diagnostic_binding_missing","owner_binding_rejected","environment_read_failed","already_claimed","malformed_fence","environment_write_failed","environment_readback_failed","environment_api_exception","claim_recheck_failed","binding_or_callback_failed","diagnostic_generation_changed","diagnostic_capability_failed","diagnostic_capability_rejected","external_reader_fence_failed","external_refused","external_failed","external_disconnected","diagnostic_fence_used","protected","failed","quit_requested"};
  return Array.IndexOf(names,reason);}catch{return -1;}
 }
 int externalEntryDepth;bool externalExportPending;
 void ExportLifecycleBoundary(){if(externalEntryDepth!=0){externalExportPending=true;return;}externalTrace.ExportTerminal(false);}
 void ExportExternalBoundary(){
  var session=external;
  if(session!=null&&(session.State==ExternalProtocol.Phase.Terminal||session.State==ExternalProtocol.Phase.Disconnected||session.State==ExternalProtocol.Phase.UnknownProtected))externalTrace.ExportTerminal(session.State==ExternalProtocol.Phase.UnknownProtected);
 }
 public string BindExternalBootstrap(int pid,string creation,string nonce,long window){
  externalTrace.Start();externalEntryDepth++;string result=null;externalTrace.Record(ExternalTrace.Stage.Bind,ExternalTrace.Result.Entered);
  try{result=BindExternalBootstrapCore(pid,creation,nonce,window);if(result!="bound")externalTrace.Refuse(ExternalTrace.Stage.Bind,TraceReason(result));else externalTrace.Record(ExternalTrace.Stage.Bind,ExternalTrace.Result.Success);return result;}
  catch(Exception error){externalTrace.Error(error,false);throw;}
  finally{externalEntryDepth--;if(externalExportPending&&externalEntryDepth==0){externalExportPending=false;externalTrace.ExportTerminal(false);}else if(result!="bound"&&bootstrapCaps==null)externalTrace.ExportTerminal(false);}
 }
 private string BindExternalBootstrapCore(int pid,string creation,string nonce,long window){
  RequireHostThread();int apartment,qualifier;Guid token;long stamp;
  if(Environment.GetEnvironmentVariable(ExternalOptIn)!="1")return "diagnostic_disabled";
  int apartmentHr=ProcessFence.CoGetApartmentType(out apartment,out qualifier);externalTrace.Record(ExternalTrace.Stage.Owner,ExternalTrace.Result.Entered,apartmentHr,apartment,qualifier);if(apartmentHr!=0||apartment!=3||qualifier!=0)return "owner_rejected";
  if(!Guid.TryParseExact(nonce,"N",out token)||token==Guid.Empty){externalTrace.Refuse(ExternalTrace.Stage.Bind,301);return "binding_rejected";}
  if(nonce!=Environment.GetEnvironmentVariable("AEGIS_TEST_NONCE")){externalTrace.Refuse(ExternalTrace.Stage.Bind,302);return "binding_rejected";}
  if(pid!=Process.GetCurrentProcess().Id){externalTrace.Refuse(ExternalTrace.Stage.Bind,303);return "binding_rejected";}
  if(!Int64.TryParse(creation,out stamp)){externalTrace.Refuse(ExternalTrace.Stage.Bind,304);return "binding_rejected";}
  if(stamp!=ProcessFence.Creation()){externalTrace.Refuse(ExternalTrace.Stage.Bind,305);return "binding_rejected";}
  if(Environment.GetEnvironmentVariable(BootstrapKey)!=null)return "bootstrap_consumed";
  // MAINSTA-only capture latch prevents stale handle-number readoption across domains/reconnects.
  string record=pid+"|"+nonce;Environment.SetEnvironmentVariable(BootstrapKey,record);
  if(Environment.GetEnvironmentVariable(BootstrapKey)!=record)return "bootstrap_latch_failed";
  externalTrace.Record(ExternalTrace.Stage.Owner,ExternalTrace.Result.Success,apartment,qualifier,externalGeneration);
  string reason;var caps=NativeCapabilities.Capture(out reason,externalTrace);if(caps==null)return reason;
  bootstrapCaps=caps;bootstrapNonce=nonce;bootstrapGeneration=externalGeneration;
  bool bound=false;return NewDiagnosticOperation().Run(()=>{
   bootstrapIdentity=new IdentityProbe();
   BindExternalDiagnostic(new ExternalDiagnosticServices(new ProcessFence(new IntPtr(window),pid,stamp),caps.PreStart,()=>bootstrapClock.ElapsedMilliseconds,caps.Ack,()=>ReleaseBootstrapResources(caps),bootstrapIdentity));
   bound=true;return "bound";
  },()=>{if(!bound)caps.Dispose();});
 }
 public string PrepareExternalBootstrap(string nonce,object candidate){
  externalTrace.Start();externalEntryDepth++;string result=null;externalTrace.Record(ExternalTrace.Stage.Prepare,ExternalTrace.Result.Entered);
  try{result=PrepareExternalBootstrapCore(nonce,candidate);if(result!="ready")externalTrace.Refuse(ExternalTrace.Stage.Prepare,TraceReason(result));return result;}
  catch(Exception error){externalTrace.Error(error,false);throw;}
  finally{externalEntryDepth--;if(externalEntryDepth==0){ExportExternalBoundary();if(externalExportPending||external==null&&result!="ready"){externalExportPending=false;externalTrace.ExportTerminal(false);}}}
 }
 private string PrepareExternalBootstrapCore(string nonce,object candidate){
  RequireHostThread();
#if AEGIS_PPT_DEFERRED_DIAGNOSTIC
  externalTrace.Record(ExternalTrace.Stage.DeferredDepth,ExternalTrace.Result.Success,deferred==null?-1:deferred.DiagnosticDepth);
#endif
  externalTrace.Record(ExternalTrace.Stage.Reading,ExternalTrace.Result.Success,reading?1:0);
  if(bootstrapCaps==null)return "bootstrap_missing";
  if(external!=null)return "external_session_exists";
  if(nonce!=bootstrapNonce||bootstrapGeneration!=externalGeneration||bootstrapCaps.IsClosed){externalTrace.Refuse(ExternalTrace.Stage.Prepare,401,(nonce!=bootstrapNonce?1:0)|(bootstrapGeneration!=externalGeneration?2:0)|(bootstrapCaps.IsClosed?4:0));return NewDiagnosticOperation().Run(()=>"bootstrap_binding_rejected",()=>{if(diagnostic!=null)diagnostic.Release();});}
  string result;
  result=NewDiagnosticOperation().Run(()=>BeginExternalDiagnostic(candidate),()=>{if(external==null&&diagnostic!=null)diagnostic.Release();});
  if(result=="ready"){
   bool started=false;NewDiagnosticOperation().Run(()=>{var timer=new System.Windows.Forms.Timer();bootstrapTimer=timer;timer.Interval=10;timer.Tick+=delegate{
    var session=external;if(session==null||bootstrapCaps.IsClosed||session.State==ExternalProtocol.Phase.UnknownProtected)return;
    externalEntryDepth++;try{PollExternalDiagnostic(bootstrapCaps.Start,bootstrapCaps.Complete,bootstrapCaps.ControllerAlive);}
    catch(Exception error){session.CapabilityFailure(error);}
    finally{externalEntryDepth--;if(externalEntryDepth==0){ExportExternalBoundary();if(externalExportPending){externalExportPending=false;externalTrace.ExportTerminal(false);}}}
   };timer.Start();started=true;return true;},()=>{if(!started&&external!=null){var session=external;session.Disconnect();if(session.Error!=null)throw session.Error;}});
  }
  return result;
 }
 private void DisposeBootstrapTimer(System.Windows.Forms.Timer timer){
  if(timer==null)return;
  var operation=NewDiagnosticOperation();externalTrace.Record(ExternalTrace.Stage.TimerCleanup,ExternalTrace.Result.Entered);
  operation.Cleanup(()=>{timer.Stop();
#if AEGIS_BOOTSTRAP_CLEANUP_TEST
   BootstrapCleanupFaults.AfterTimerStop();
#endif
  });
  operation.Cleanup(()=>{timer.Dispose();
#if AEGIS_BOOTSTRAP_CLEANUP_TEST
   BootstrapCleanupFaults.AfterTimerDispose();
#endif
  });externalTrace.Errors(operation.Errors);externalTrace.Record(ExternalTrace.Stage.TimerCleanup,ExternalTrace.Result.Success);operation.Errors.Throw();
 }
 private void StopBootstrapTimer(){var timer=bootstrapTimer;bootstrapTimer=null;DisposeBootstrapTimer(timer);}
 private void ReleaseBootstrapResources(NativeCapabilities caps){
  var operation=NewDiagnosticOperation();operation.Cleanup(StopBootstrapTimer);operation.Cleanup(caps.Dispose);externalTrace.Errors(operation.Errors);operation.Errors.Throw();
 }
 public string ExternalBootstrapStatus(){
  RequireHostThread();return "adopt="+(bootstrapCaps==null?0:1)+" close="+(bootstrapCaps==null?0:bootstrapCaps.CloseAttempts)+" closed="+(bootstrapCaps==null?0:bootstrapCaps.Closed)+" ack="+(bootstrapCaps==null?0:bootstrapCaps.Acks)+" phase="+(external==null?"none":external.State.ToString())+" outcome="+(shutdown==null?"none":shutdown.ExternalOutcome)+" reader="+ReaderFence.AllowsRead()+" identity="+(bootstrapIdentity==null?"0/0":bootstrapIdentity.Acquired+"/"+bootstrapIdentity.Released)+" identity_result="+(external==null?"NotChecked":external.IdentityResult.ToString())+" guard_prepare="+(external==null||!external.GuardReached?"NotReached":"Entered")+" guard_precheck="+(external==null?"NotRun":external.GuardPrecheck.ToString())+" prepare_decision="+(external==null?"NotReached":external.Decision.ToString())+" guard_result="+(external==null?"":external.GuardResult)+" "+externalTrace.Status;
 }
}
}

#endif
