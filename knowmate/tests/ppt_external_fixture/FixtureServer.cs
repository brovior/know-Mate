using System;
using System.Diagnostics;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Threading;
using System.Windows.Forms;
using AegisDesk.PptReader;
[ComVisible(true),Guid("EF079FC2-AB65-4F09-A4CC-8D102AB7CA9C"),InterfaceType(ComInterfaceType.InterfaceIsIDispatch)]
public interface IFixtureControl {
 [DispId(1)][return:MarshalAs(UnmanagedType.IDispatch)]object Reader();
 [DispId(2)][return:MarshalAs(UnmanagedType.IDispatch)]object Host(bool different);
 [DispId(3)]string Info();[DispId(4)]void Stop();[DispId(5)]void Reconnect();
 [DispId(6)]void Busy(bool value);[DispId(7)]string Deferred();
}
[ComVisible(true),ClassInterface(ClassInterfaceType.None),ComDefaultInterface(typeof(IFixtureControl))]
public sealed class Control:StandardOleMarshalObject,IFixtureControl {
 internal readonly Connect Connect=new Connect();internal readonly Host Application=new Host(),Other=new Host();
 readonly IntPtr window;internal bool Stopping;internal int Advises,Unadvises;
 internal Control(IntPtr hwnd){window=hwnd;Reconnect();}
 public object Reader(){return Connect;}public object Host(bool different){return different?Other:Application;}
 public string Info(){return Process.GetCurrentProcess().Id+"|"+ProcessFence.Creation()+"|"+window.ToInt64()+"|"+ProcessFence.GetCurrentThreadId();}
 public void Stop(){Stopping=true;}
 public void Busy(bool value){typeof(Connect).GetField("reading",BindingFlags.NonPublic|BindingFlags.Instance).SetValue(Connect,value);}
 public void Reconnect(){Array c=null;Connect.OnDisconnection(1,ref c);Connect.OnConnection(Application,1,new Addin(),ref c);var guard=(PptShutdownGuard)Program.Field(Connect,"shutdown");var point=new FixturePoint(this);typeof(PptShutdownGuard).GetField("resolvePoint",BindingFlags.NonPublic|BindingFlags.Instance).SetValue(guard,new Func<object,IConnectionPoint>(h=>point));typeof(PptShutdownGuard).GetField("createSink",BindingFlags.NonPublic|BindingFlags.Instance).SetValue(guard,new Func<PptShutdownState,object>(s=>Activator.CreateInstance(Program.Events.GetType("AegisDesk.PptEvents.PptEventSink"),new object[]{s})));}
 public string Deferred(){string result=null;bool signal=false;var gate=new PptDeferredGate(()=>true,()=>signal,()=>true,()=>true,()=>{result=(string)typeof(Connect).GetMethod("InvokeExistingShutdown",BindingFlags.NonPublic|BindingFlags.Instance).Invoke(Connect,null);return result;},()=>{},s=>{});gate.Enter(true);gate.Arm();gate.Leave();signal=true;gate.Tick();return result;}
 internal string Audit(){var caps=(NativeCapabilities)Program.Field(Connect,"bootstrapCaps");return "adopt="+(caps==null?0:1)+" close="+(caps==null?0:caps.CloseAttempts)+" closed="+(caps==null?0:caps.Closed)+" ack="+(caps==null?0:caps.Acks)+" quit="+Application.Calls.FindAll(s=>s=="quit").Count+" advise="+Advises+" unadvise="+Unadvises;}
}
public sealed class FixturePoint:IConnectionPoint {
 readonly Control owner;internal FixturePoint(Control value){owner=value;}
 public void Advise(object sink,out int cookie){owner.Advises++;cookie=7;}
 public void Unadvise(int cookie){owner.Unadvises++;}
 public void GetConnectionInterface(out Guid id){id=Guid.Empty;}
 public void GetConnectionPointContainer(out IConnectionPointContainer c){c=null;}
 public void EnumConnections(out IEnumConnections e){e=null;}
}
static class Program {
 internal static Assembly Events;
 [DllImport("ole32.dll")]static extern int GetRunningObjectTable(int reserved,out IRunningObjectTable table);
 [DllImport("ole32.dll",CharSet=CharSet.Unicode)]static extern int CreateItemMoniker(string delimiter,string item,out IMoniker moniker);
 [DllImport("ole32.dll")]static extern int CoInitializeEx(IntPtr p,uint flags);
 [DllImport("ole32.dll")]static extern void CoUninitialize();
 [DllImport("kernel32.dll")]static extern bool GetHandleInformation(IntPtr h,out uint flags);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)]delegate int ProtocolCall(IntPtr self,out int value);
 internal static object Field(object o,string name){return o.GetType().GetField(name,BindingFlags.NonPublic|BindingFlags.Instance).GetValue(o);}
 static void Legacy(Connect connect){IntPtr unknown=Marshal.GetIUnknownForObject(connect);try{foreach(Type face in new[]{typeof(IPptReader),typeof(IPptReaderLifecycle)}){Guid iid=face.GUID;IntPtr p;Marshal.ThrowExceptionForHR(Marshal.QueryInterface(unknown,ref iid,out p));try{var call=(ProtocolCall)Marshal.GetDelegateForFunctionPointer(Marshal.ReadIntPtr(Marshal.ReadIntPtr(p),7*IntPtr.Size),typeof(ProtocolCall));int protocol;Marshal.ThrowExceptionForHR(call(p,out protocol));if(protocol!=3)throw new Exception("dual_protocol");}finally{Marshal.Release(p);}}}finally{Marshal.Release(unknown);}if(typeof(IPptReaderLifecycle).GetMethods().Length!=4||typeof(IPptReader).GetMethods().Length!=3)throw new Exception("dual_shape");Console.WriteLine("LEGACY_DUAL PASS");}
 [STAThread]static int Main(string[] args){int init=CoInitializeEx(IntPtr.Zero,2);IRunningObjectTable rot=null;IMoniker moniker=null;int cookie=0;Control control=null;var window=new NativeWindow();try{
  Events=Assembly.LoadFrom(args[1]);window.CreateHandle(new CreateParams{Caption="BootstrapFixture"});control=new Control(window.Handle);
  if(args[2]=="normal_fatal")control.Application.OnQuit=()=>{control.Stopping=true;throw new COMException("synthetic_failure",unchecked((int)0x80010108));};
  if(typeof(Connect).Assembly.GetType("BootstrapCleanupFaults")!=null||!typeof(IdentityProbe).GetField("Acquire",BindingFlags.NonPublic|BindingFlags.Instance).IsInitOnly||!typeof(IdentityProbe).GetField("Release",BindingFlags.NonPublic|BindingFlags.Instance).IsInitOnly)throw new Exception("mutable_fixture_seam_leaked");
  Legacy(control.Connect);Marshal.ThrowExceptionForHR(GetRunningObjectTable(0,out rot));Marshal.ThrowExceptionForHR(CreateItemMoniker("!",args[0],out moniker));cookie=rot.Register(1,control,moniker);Console.WriteLine("READY "+control.Info());
  var timer=Stopwatch.StartNew();while(!control.Stopping&&timer.ElapsedMilliseconds<20000){Application.DoEvents();Thread.Sleep(1);}if(!control.Stopping)throw new Exception("fixture_timeout");return 0;
 }catch(Exception e){Console.WriteLine(e);return 1;}finally{
  if(control!=null){Array custom=null;try{control.Connect.OnDisconnection(1,ref custom);control.Connect.OnDisconnection(1,ref custom);}finally{Console.WriteLine("AUDIT "+control.Audit());if(Field(control.Connect,"bootstrapCaps")==null){bool valid=true;foreach(string name in new[]{"START","ACK","COMPLETE","CONTROLLER"}){uint flags;valid&=GetHandleInformation(new IntPtr(Int64.Parse(Environment.GetEnvironmentVariable("AEGIS_TEST_CAP_"+name))),out flags);}Console.WriteLine("NO_ADOPTION_PRESERVED "+valid);}}}
  if(cookie!=0){rot.Revoke(cookie);Console.WriteLine("ROT_REVOKED 1");}if(moniker!=null)Marshal.ReleaseComObject(moniker);if(rot!=null)Marshal.ReleaseComObject(rot);window.DestroyHandle();if(init>=0)CoUninitialize();
 }}
}
