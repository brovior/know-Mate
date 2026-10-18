#if AEGIS_PPT_DEFERRED_DIAGNOSTIC
using System;
using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Threading;
namespace AegisDesk.PptReader {
/// <summary>Opt-in diagnostic interface; existing dual interfaces are unchanged.</summary>
[ComVisible(true),Guid("FA64BE16-CB3D-48EB-B9FC-090A6F662A54"),InterfaceType(ComInterfaceType.InterfaceIsIDispatch)]
public interface IPptReaderDeferredDiagnostic {
 /// <summary>Preserve normal dispatch methods in the explicit diagnostic build.</summary>
 [DispId(1)] int ProtocolVersion();
 /// <summary>Read through the unchanged reader implementation.</summary>
 [DispId(2)] string ExtractSlide([MarshalAs(UnmanagedType.BStr)] string presentationPath,int slide);
 /// <summary>Return the current reader version.</summary>
 [DispId(3)] string ReaderVersion();
 /// <summary>Use the existing synchronous guard entry.</summary>
 [DispId(4)] string TryQuitIfIdle();
 /// <summary>Arm once for the exact capability-bearing host. No handle arguments.</summary>
 [DispId(5)] [return:MarshalAs(UnmanagedType.BStr)] string ArmDeferredQuit(int pid,
  [MarshalAs(UnmanagedType.BStr)] string creation,[MarshalAs(UnmanagedType.BStr)] string nonce);
}
internal sealed class PptDeferredDiagnostic {
 static int bootstrapConsumed;
#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
 internal int DiagnosticDepth {get{return gate.Depth;}}
#endif
 readonly PptDeferredGate gate;readonly int pid;readonly long creation;readonly string nonce;
 readonly uint owner;readonly IntPtr signal,controller;
 readonly System.Windows.Forms.Timer timer;
 internal static PptDeferredDiagnostic Capture(Func<string> invoke,Func<bool> valid,Action<string> record) {
  if(Environment.GetEnvironmentVariable("AEGIS_PPT_DEFERRED_DIAGNOSTIC")!="1")return null;
  // Never reinterpret stale/recycled startup numbers after a reconnect/failure.
  if(Interlocked.Exchange(ref bootstrapConsumed,1)!=0)return null;
  long eventValue,controllerValue;Guid token;
  string nonce=Environment.GetEnvironmentVariable("AEGIS_PPT_DEFERRED_NONCE");
  if(!Guid.TryParseExact(nonce,"N",out token)||token==Guid.Empty||
   !Int64.TryParse(Environment.GetEnvironmentVariable("AEGIS_PPT_DEFERRED_EVENT"),NumberStyles.None,CultureInfo.InvariantCulture,out eventValue)||
   !Int64.TryParse(Environment.GetEnvironmentVariable("AEGIS_PPT_DEFERRED_CONTROLLER"),NumberStyles.None,CultureInfo.InvariantCulture,out controllerValue))return null;
  IntPtr e,p;try{e=new IntPtr(eventValue);p=new IntPtr(controllerValue);}catch{return null;}
  // No close on any failed validation: untrusted numbers are not owned handles.
  if(!PptDeferredNative.CanAdopt(e,p))return null;
  return new PptDeferredDiagnostic(e,p,nonce,invoke,valid,record);
 }
 PptDeferredDiagnostic(IntPtr e,IntPtr p,string token,Func<string> invoke,Func<bool> valid,Action<string> record) {
  signal=e;controller=p;nonce=token;owner=PptDeferredNative.ThreadId();
  System.Windows.Forms.Timer created=null;
  var resources=new PptDeferredResources(e,p,delegate{if(created!=null){try{created.Stop();}finally{created.Dispose();}}},PptDeferredNative.Close,record);
  try {
   pid=Process.GetCurrentProcess().Id;creation=PptDeferredNative.Creation();
   created=new System.Windows.Forms.Timer();timer=created;
   gate=new PptDeferredGate(()=>PptDeferredNative.ThreadId()==owner,
    ()=>PptDeferredNative.Wait(signal)==0,()=>PptDeferredNative.Wait(controller)==258,
    ()=>valid()&&Process.GetCurrentProcess().Id==pid&&PptDeferredNative.Creation()==creation,
    invoke,resources.Dispose,record);
   timer.Interval=20;timer.Tick+=delegate{gate.Tick();};timer.Start();
  }catch{resources.Dispose();throw;}
 }
 internal string Arm(int hostPid,string hostCreation,string token) {
  gate.Enter(true);
  try {
   if(hostPid!=pid||hostCreation!=creation.ToString(CultureInfo.InvariantCulture)||token!=nonce){gate.Cancel("binding_mismatch");return "not_supported";}
   return gate.Arm();
  }finally{gate.Leave();}
 }
 internal void Enter(){gate.Enter(false);}
 internal void Leave(){gate.Leave();}
 internal void Cancel(){gate.Cancel("disconnect");}
}
internal static class PptDeferredNative {
 [DllImport("kernel32.dll")]internal static extern uint GetCurrentThreadId();
 [DllImport("kernel32.dll")]static extern IntPtr GetCurrentProcess();
 [DllImport("kernel32.dll",SetLastError=true)]static extern bool GetProcessTimes(IntPtr h,out long c,out long x,out long k,out long u);
 [DllImport("kernel32.dll",SetLastError=true)]static extern bool CloseHandle(IntPtr h);
 [DllImport("kernel32.dll",SetLastError=true)]static extern uint WaitForSingleObject(IntPtr h,uint ms);
 [DllImport("kernel32.dll",SetLastError=true)]static extern bool GetHandleInformation(IntPtr h,out uint flags);
 [DllImport("ntdll.dll")]static extern int NtQueryObject(IntPtr h,int kind,IntPtr data,int length,out int needed);
 internal static uint ThreadId(){return GetCurrentThreadId();}
 internal static long Creation(){long c,x,k,u;if(!GetProcessTimes(GetCurrentProcess(),out c,out x,out k,out u))throw new InvalidOperationException("deferred_identity");return c;}
 internal static uint Wait(IntPtr h){uint value=WaitForSingleObject(h,0);if(value!=0&&value!=258)throw new InvalidOperationException("deferred_wait");return value;}
 internal static void Close(IntPtr h){if(!CloseHandle(h))throw new InvalidOperationException("deferred_close");}
 static string Text(IntPtr h,int kind){IntPtr b=Marshal.AllocHGlobal(2048);try{int needed;if(NtQueryObject(h,kind,b,2048,out needed)<0)return null;int n=(ushort)Marshal.ReadInt16(b);return n==0?"":Marshal.PtrToStringUni(Marshal.ReadIntPtr(b,IntPtr.Size==8?8:4),n/2);}finally{Marshal.FreeHGlobal(b);}}
 static bool SyncOnly(IntPtr h,string type){
  uint flags;if(h==IntPtr.Zero||!GetHandleInformation(h,out flags)||(flags&1)==0||Text(h,2)!=type)return false;
  IntPtr b=Marshal.AllocHGlobal(56);try{int needed;return NtQueryObject(h,0,b,56,out needed)>=0&&Marshal.ReadInt32(b,4)==0x100000;}finally{Marshal.FreeHGlobal(b);}
 }
 internal static bool CanAdopt(IntPtr e,IntPtr p){
  try{return e!=p&&SyncOnly(e,"Event")&&Text(e,1)==""&&SyncOnly(p,"Process");}catch{return false;}
 }
}
}
#endif
