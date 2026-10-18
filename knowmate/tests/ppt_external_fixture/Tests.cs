using System;
using System.Diagnostics;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Threading;
using System.Windows.Forms;
using AegisDesk.PptReader;
public sealed class ProbePoint:IConnectionPoint {
 internal int Advises,Unadvises;internal object Sink;internal Action Advising;
 public void Advise(object sink,out int cookie){Sink=sink;cookie=7;Advises++;if(Advising!=null)Advising();}
 public void Unadvise(int cookie){Unadvises++;}
 public void GetConnectionInterface(out Guid id){id=Guid.Empty;}
 public void GetConnectionPointContainer(out IConnectionPointContainer c){c=null;}
 public void EnumConnections(out IEnumConnections e){e=null;}
 internal bool Cancel(){object[] args={null,false};Sink.GetType().GetMethod("PresentationBeforeClose").Invoke(Sink,args);return (bool)args[1];}
}
internal sealed class Fixture:IDisposable {
 internal readonly Connect Connect=new Connect();internal readonly Host Host=new Host{FailWindow=true};
 internal readonly ProbePoint Point=new ProbePoint();internal readonly Capabilities Caps=new Capabilities();internal readonly IdentityProbe Identity=new IdentityProbe();
 internal PptShutdownGuard Guard;internal long Now;internal IntPtr Window;
 internal Fixture(IntPtr window){Window=window;Reconnect();}
 internal void Reconnect(){Array custom=null;Connect.OnConnection(Host,1,new Addin(),ref custom);Guard=(PptShutdownGuard)Tests.Field(Connect,"shutdown");typeof(PptShutdownGuard).GetField("resolvePoint",BindingFlags.NonPublic|BindingFlags.Instance).SetValue(Guard,new Func<object,IConnectionPoint>(h=>Point));typeof(PptShutdownGuard).GetField("createSink",BindingFlags.NonPublic|BindingFlags.Instance).SetValue(Guard,new Func<PptShutdownState,object>(s=>Activator.CreateInstance(Tests.Events.GetType("AegisDesk.PptEvents.PptEventSink"),new object[]{s})));}
 ExternalDiagnosticServices services;
 internal void Bind(bool valid=true){if(services==null)services=new ExternalDiagnosticServices(new ProcessFence(Window,Process.GetCurrentProcess().Id,ProcessFence.Creation()),()=>valid&&Caps.Validate(),()=>Now,Caps.Ack,Caps.Release,Identity);Connect.BindExternalDiagnostic(services);}
 internal string Begin(){return Connect.BeginExternalDiagnostic(Host);}
 internal ExternalProtocol Session {get{return (ExternalProtocol)Tests.Field(Connect,"external");}}
 internal void Ready(){Tests.Check(Begin()=="ready","ready");Tests.Check(Object.ReferenceEquals(Session.Guard,Guard),"same_onconnection_guard");}
 internal void Commit(){Connect.PollExternalDiagnostic(true,false,true);Tests.Check(Caps.AckCalls==1&&Guard.Attempted,"commit_ack");}
 internal void Complete(){Host.Quit();Connect.PollExternalDiagnostic(true,true,true);}
 internal int Quits {get{return Host.Calls.FindAll(s=>s=="quit").Count;}}
 internal bool Read(){try{return Connect.ExtractSlide("synthetic",1)=="";}catch(COMException error){Tests.Check(error.Message=="external_reader_protected","exact_reader_gate");return false;}}
 public void Dispose(){Array custom=null;Connect.OnDisconnection(1,ref custom);Caps.Dispose();AegisDesk.PptShared.PptExtractionCore.OnExtract=null;RawCount.Before=null;}
}
public sealed class DomainReader:MarshalByRefObject {
 public override object InitializeLifetimeService(){return null;}
 public bool Read(){var c=new Connect();Array custom=null;c.OnConnection(new Host(),1,new Addin(),ref custom);try{Tests.Check(c.ProtocolVersion()==3,"new_domain_connected");return c.ExtractSlide("synthetic",1)=="";}catch(COMException error){Tests.Check(error.Message=="external_reader_protected","new_domain_exact_reader_gate");return false;}finally{c.OnDisconnection(1,ref custom);}}
 public string Quit(){var c=new Connect();Array custom=null;c.OnConnection(new Host(),1,new Addin(),ref custom);try{return c.TryQuitIfIdle();}finally{c.OnDisconnection(1,ref custom);}}
}
static class Tests {
 internal static Assembly Events;
 [DllImport("ole32.dll")]static extern int CoInitializeEx(IntPtr p,uint flags);
 [DllImport("ole32.dll")]static extern void CoUninitialize();
 internal static object Field(object o,string name){return o.GetType().GetField(name,BindingFlags.NonPublic|BindingFlags.Instance).GetValue(o);}
 internal static void Check(bool value,string label){if(!value)throw new Exception(label);}
 static void Reading(Connect c,bool busy){typeof(Connect).GetField("reading",BindingFlags.NonPublic|BindingFlags.Instance).SetValue(c,busy);}
 static void NoClaim(){string s;Check(new NativeEnvironment().Read(out s)==EnvRead.Missing,"no_diagnostic_claim");}
 static void Used(){string s;Check(new NativeEnvironment().Read(out s)==EnvRead.Present,"claim_kept");}
 static void Run(string mode,IntPtr window){
  using(var f=new Fixture(window)){
   if(mode=="bounded_records"){for(int i=0;i<40;i++)f.Reconnect();Check(f.Connect.DiagnosticOperations.Count==16,"bounded_16");return;}
   if(mode=="ordinary_normal"){Check(f.Connect.TryQuitIfIdle()=="quit_requested"&&f.Quits==1,"ordinary_quit");Check(f.Connect.TryQuitIfIdle()=="protected"&&f.Read(),"ordinary_sticky_read");NoClaim();return;}
   if(mode=="ordinary_reading"){Reading(f.Connect,true);Check(f.Connect.TryQuitIfIdle()=="protected"&&f.Quits==0,"ordinary_reading");Reading(f.Connect,false);Check(f.Read(),"reader");NoClaim();return;}
   if(mode=="ordinary_external_disabled"){f.Bind();Check(f.Begin()=="diagnostic_disabled"&&f.Connect.TryQuitIfIdle()=="quit_requested","disabled_no_claim_requirement");NoClaim();return;}
   if(mode=="ordinary_reconnect"){Check(f.Connect.TryQuitIfIdle()=="quit_requested","first");f.Reconnect();Check(f.Connect.TryQuitIfIdle()=="quit_requested"&&f.Quits==2,"ordinary_behavior_preserved");NoClaim();return;}
   if(mode=="diagnostic_missing_binding"){Check(f.Connect.TryQuitIfIdle()=="diagnostic_binding_missing"&&f.Begin()=="diagnostic_binding_missing"&&f.Quits==0&&f.Read(),"no_diagnostic_fallback");NoClaim();return;}
   f.Bind(mode!="invalid_capability");
   if(mode.StartsWith("decision_")){
    int generation=1;bool presignaled=mode=="decision_presignaled";
    var identity=new IdentityProbe();
    Check(identity.Result==IdentityProbe.Comparison.NotChecked,"identity_initial_not_checked");
    if(mode=="decision_generation")identity.Acquire=o=>{generation=2;return Marshal.GetIUnknownForObject(o);};
    if(mode=="decision_release_failed")identity.Release=p=>{Marshal.Release(p);throw new InvalidOperationException("after_actual_release");};
    if(mode=="decision_second_failed")identity.Acquire=o=>{if(identity.Acquired==1)throw new InvalidOperationException("second_failed");return Marshal.GetIUnknownForObject(o);};
    var session=new ExternalProtocol(f.Guard,1,()=>0,()=>{throw new Exception("unexpected_ack");},()=>{},()=>generation);
    Check(!session.Prepare(f.Host,f.Host,identity,false,presignaled),"decision_refused");
    var expected=presignaled?ExternalProtocol.PrepareDecision.Presignaled:mode=="decision_generation"?ExternalProtocol.PrepareDecision.GenerationOrStateChange:ExternalProtocol.PrepareDecision.Failed;
    Check(session.Decision==expected&&!session.GuardReached,"exact_decision_before_guard");
    Check(session.IdentityResult==(mode=="decision_second_failed"?IdentityProbe.Comparison.Failed:IdentityProbe.Comparison.Same),"comparison_separate_from_cleanup");
    Check(f.Quits==0&&f.Point.Advises==0,"decision_no_host_actions");
    Console.WriteLine("decision="+session.Decision+" identity="+session.IdentityResult);return;
   }
   if(mode.StartsWith("status_")){
    typeof(Connect).GetField("bootstrapIdentity",BindingFlags.NonPublic|BindingFlags.Instance).SetValue(f.Connect,f.Identity);
    string expectedIdentity="Same",expectedGuard="NotRun",expectedResult="external_refused";
    object candidate=f.Host;
    if(mode=="status_different"){candidate=new Host();expectedIdentity="Different";}
    else if(mode=="status_unknown"){f.Identity.Acquire=o=>{throw new InvalidOperationException("identity_unavailable");};expectedIdentity="Failed";expectedResult="external_failed";}
    else if(mode=="status_ready"){expectedGuard="Passed";expectedResult="ready";}
    else if(mode=="status_body_refused"){f.Host.Docs.Count=1;expectedGuard="Passed";}
    else {
     string fields=mode.Substring(7);
     if(fields=="combined")fields="entered,attempted,reading";
     if(fields=="priority")fields="disconnected,broken,entered,attempted,reading";
     foreach(string field in fields.Split(',')){
      if(field=="reading")Reading(f.Connect,true);
      else typeof(PptShutdownGuard).GetField(field,BindingFlags.NonPublic|BindingFlags.Instance).SetValue(f.Guard,true);
     }
     expectedGuard=mode=="status_priority"?"Disconnected, Broken":mode=="status_combined"?"Entered, Attempted, Reading":Char.ToUpper(fields[0])+fields.Substring(1);
    }
    Check(f.Connect.BeginExternalDiagnostic(candidate)==expectedResult,"status_original_outcome");
    Check(f.Session.IdentityResult.ToString()==expectedIdentity&&f.Session.GuardPrecheck.ToString()==expectedGuard,"primitive_snapshots");
    string before="no_followup_status_after_unknown";
    if(expectedResult!="external_failed"){
     before=f.Connect.ExternalBootstrapStatus();string after=f.Connect.ExternalBootstrapStatus();
     Check(before==after&&before.Contains(" identity_result="+expectedIdentity+" ")&&before.Contains(" guard_precheck="+expectedGuard+" "),"readonly_status_exact");
     Check(f.Session.Decision==(mode=="status_different"?ExternalProtocol.PrepareDecision.IdentityMismatch:expectedResult=="ready"?ExternalProtocol.PrepareDecision.Ready:ExternalProtocol.PrepareDecision.GuardResult),"exact_prepare_decision");
    }
    Check(f.Quits==0&&f.Caps.AckCalls==0,"status_no_quit_ack");
    Check(f.Identity.Acquired==f.Identity.Released,"status_identity_balanced");
    if(expectedGuard!="Passed")Check(f.Point.Advises==0,"early_refusal_no_sink");
    if(expectedResult=="external_refused")Check(f.Caps.ReleaseCalls==1&&f.Session.State==ExternalProtocol.Phase.Terminal,"refusal_cleanup_unchanged");
    if(mode=="status_body_refused")Check(f.Guard.ExternalOutcome=="protected","body_refusal_distinct");
    Console.WriteLine(before);return;
   }
   if(mode=="diagnostic_owner_reject"){Check(f.Connect.TryQuitIfIdle()=="owner_rejected"&&f.Begin()=="owner_rejected"&&f.Read()&&f.Quits==0,"diagnostic_nonmain_reader");NoClaim();return;}
   if(mode=="diagnostic_legacy_normal"){Check(f.Connect.TryQuitIfIdle()=="quit_requested"&&f.Quits==1,"legacy_diagnostic_claim");Used();Environment.SetEnvironmentVariable("AEGIS_PPT_EXTERNAL_DIAGNOSTIC","1");f.Reconnect();f.Bind();Check(f.Begin()=="already_claimed","legacy_to_external_blocked");return;}
   if(mode=="invalid_capability"){Check(f.Begin()=="diagnostic_capability_rejected"&&f.Caps.AckCalls==0&&f.Quits==0,"invalid_no_prepare");Used();Check(f.Connect.TryQuitIfIdle()=="diagnostic_fence_used","no_retry");return;}
   if(mode=="diagnostic_normal"||mode=="diagnostic_deferred"){
    string result=null;if(mode=="diagnostic_normal")result=f.Connect.TryQuitIfIdle();else{bool signal=false;var gate=new PptDeferredGate(()=>true,()=>signal,()=>true,()=>true,()=>{result=(string)typeof(Connect).GetMethod("InvokeExistingShutdown",BindingFlags.NonPublic|BindingFlags.Instance).Invoke(f.Connect,null);return result;},()=>{},s=>{});gate.Enter(true);gate.Arm();gate.Leave();signal=true;gate.Tick();}
    Check(result=="quit_requested"&&f.Quits==1,"diagnostic_other_path");Used();f.Reconnect();f.Bind();Check(f.Begin()=="already_claimed"&&f.Connect.TryQuitIfIdle()=="diagnostic_fence_used"&&f.Read(),"diagnostic_reconnect_no_bypass");return;
   }
   if(mode=="reading_at_admission"){string result=null;AegisDesk.PptShared.PptExtractionCore.OnExtract=()=>{result=f.Begin();};Check(f.Read()&&result=="external_refused"&&f.Point.Advises==0,"current_reading_used");Used();return;}
   if(mode=="reading_during_identity"){f.Identity.Acquire=o=>{Reading(f.Connect,true);return Marshal.GetIUnknownForObject(o);};Check(f.Begin()=="external_refused"&&f.Point.Advises==0,"live_reading_rechecked");Reading(f.Connect,false);Check(f.Read(),"reader_after_clean_refusal");return;}
   if(mode=="reconnect_during_claim"){f.Caps.Validating=()=>f.Reconnect();Check(f.Begin()=="diagnostic_generation_changed"&&f.Point.Advises==0,"claim_generation");Used();return;}
   if(mode=="disconnect_during_prepare"){f.Point.Advising=()=>{Array c=null;f.Connect.OnDisconnection(1,ref c);};Check(f.Begin()!="ready"&&f.Caps.AckCalls==0&&f.Caps.ReleaseCalls==1,"prepare_disconnect");f.Reconnect();Check(!f.Read()&&f.Connect.TryQuitIfIdle()=="diagnostic_fence_used","unconfirmed_protects_new_reader");return;}
   f.Ready();
   Check(f.Connect.TryQuitIfIdle()=="diagnostic_fence_used"&&!f.Read(),"ready_blocks_other_entry");
   if(mode=="reading_before_commit"){Reading(f.Connect,true);f.Connect.PollExternalDiagnostic(true,false,true);Reading(f.Connect,false);Check(f.Caps.AckCalls==0&&f.Caps.ReleaseCalls==1&&f.Read(),"busy_commit_refused");return;}
   if(mode=="reconnect_ready"){var old=f.Session;f.Reconnect();old.Poll(true,false,true,999);Check(f.Caps.AckCalls==0&&!f.Read()&&f.Connect.TryQuitIfIdle()=="diagnostic_fence_used","stale_session_denied");return;}
   f.Commit();
   if(mode=="external_normal"||mode=="external_cancelled"){
    if(mode=="external_cancelled")Check(f.Point.Cancel(),"real_sink_cancel");f.Complete();Check(f.Guard.ExternalOutcome==(mode=="external_cancelled"?"cancelled_return":"normal_return")&&f.Caps.ReleaseCalls==1&&f.Point.Unadvises==1&&f.Read(),"normal_completion");f.Reconnect();Check(f.Read()&&f.Connect.TryQuitIfIdle()=="diagnostic_fence_used","normal_reader_only");return;
   }
   f.Now=6000;f.Connect.PollExternalDiagnostic(true,false,true);Check(f.Session.State==ExternalProtocol.Phase.UnknownProtected&&f.Guard.ExternalState.Active&&f.Point.Cancel(),"unknown_active_sink");
   if(mode=="unknown_late_completion"){f.Connect.PollExternalDiagnostic(true,true,true);Check(!f.Read()&&f.Session.State==ExternalProtocol.Phase.UnknownProtected,"late_completion_no_clear");return;}
   if(mode=="unknown_new_domain"){
    var domain=AppDomain.CreateDomain("external-reader",null,new AppDomainSetup{ApplicationBase=AppDomain.CurrentDomain.BaseDirectory});
    try{var d=(DomainReader)domain.CreateInstanceFromAndUnwrap(Assembly.GetExecutingAssembly().Location,typeof(DomainReader).FullName);Check(!d.Read()&&d.Quit()=="diagnostic_fence_used","new_domain_blocked");}finally{AppDomain.Unload(domain);}return;
   }
   if(mode=="unknown_new_connect"||mode=="unknown_optout_bypass"){if(mode=="unknown_optout_bypass")Environment.SetEnvironmentVariable("AEGIS_PPT_EXTERNAL_DIAGNOSTIC",null);using(var next=new Fixture(window)){Check(!next.Read()&&next.Connect.TryQuitIfIdle()=="diagnostic_fence_used","new_connect_blocked");}return;}
   if(mode=="unknown_reconnect"){f.Reconnect();Check(!f.Read()&&f.Connect.TryQuitIfIdle()=="diagnostic_fence_used","reconnect_blocked");return;}
   throw new Exception("unknown_case");
  }
 }
 [STAThread]static int Main(string[] args){int hr=CoInitializeEx(IntPtr.Zero,2);var w=new NativeWindow();try{Check(hr>=0,"com_init");Events=Assembly.LoadFrom(args[1]);w.CreateHandle(new CreateParams{Caption="ExternalIntegrationFixture"});if(args[0].EndsWith("secondary_sta")){Exception failure=null;IntPtr window=w.Handle;var t=new Thread(()=>{int init=CoInitializeEx(IntPtr.Zero,2);try{Run(args[0].StartsWith("ordinary")?"ordinary_normal":"diagnostic_owner_reject",window);}catch(Exception error){failure=error;}finally{if(init>=0)CoUninitialize();}});t.SetApartmentState(ApartmentState.STA);t.Start();t.Join();if(failure!=null)throw failure;}else Run(args[0],w.Handle);Console.WriteLine("PASS "+args[0]);return 0;}catch(Exception e){Console.WriteLine(e);return 1;}finally{w.DestroyHandle();if(hr>=0)CoUninitialize();}}
}
