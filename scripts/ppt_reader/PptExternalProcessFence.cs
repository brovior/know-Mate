#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
using System;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;

internal enum EnvRead { Missing, Present, Failure }
internal interface IProcessEnvironment {
 EnvRead Read(out string value);
 bool Write(string value);
}
internal sealed class NativeEnvironment:IProcessEnvironment {
 internal const string Key="AEGIS_PPT_DIAGNOSTIC_ADMISSION_V1";
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]static extern uint GetEnvironmentVariable(string name,StringBuilder value,uint size);
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]static extern bool SetEnvironmentVariable(string name,string value);
 [DllImport("kernel32.dll")]static extern void SetLastError(uint error);
 public EnvRead Read(out string value){
  var buffer=new StringBuilder(256);SetLastError(0);
  uint size=GetEnvironmentVariable(Key,buffer,(uint)buffer.Capacity);int error=Marshal.GetLastWin32Error();
  value=null;
  if(size==0&&error==203)return EnvRead.Missing;
  if(size==0&&error!=0||size>=buffer.Capacity)return EnvRead.Failure;
  value=buffer.ToString();return EnvRead.Present;
 }
 public bool Write(string value){if(String.IsNullOrEmpty(value))throw new InvalidOperationException("never_clear_fence");return SetEnvironmentVariable(Key,value);}
}
internal sealed class ProcessFence {
 [DllImport("ole32.dll")]internal static extern int CoGetApartmentType(out int apartment,out int qualifier);
 [DllImport("kernel32.dll")]internal static extern uint GetCurrentThreadId();
 [DllImport("kernel32.dll")]static extern IntPtr GetCurrentProcess();
 [DllImport("kernel32.dll",SetLastError=true)]static extern bool GetProcessTimes(IntPtr process,out long created,out long exited,out long kernel,out long user);
 [DllImport("user32.dll",SetLastError=true)]static extern uint GetWindowThreadProcessId(IntPtr window,out uint pid);
 internal AegisDesk.PptReader.ExternalTrace Telemetry;
 readonly NativeEnvironment Environment=new NativeEnvironment();
 readonly IntPtr VerifiedWindow;readonly int VerifiedPid;readonly long VerifiedCreation;
 internal ProcessFence(IntPtr window,int pid,long creation){VerifiedWindow=window;VerifiedPid=pid;VerifiedCreation=creation;}
 internal static long Creation(){long c,e,k,u;if(!GetProcessTimes(GetCurrentProcess(),out c,out e,out k,out u))throw new InvalidOperationException("process_identity_unavailable");return c;}
 bool Binding(uint owner,int pid){uint actualPid;return VerifiedPid==pid&&VerifiedCreation==Creation()&&VerifiedWindow!=IntPtr.Zero&&GetWindowThreadProcessId(VerifiedWindow,out actualPid)==owner&&actualPid==(uint)pid;}
 static bool WellFormed(string text){
  if(text==null)return false;string[] p=text.Split('|');uint pid,tid;Guid nonce;
  return p.Length==4&&p[0]=="CLAIMED"&&UInt32.TryParse(p[1],out pid)&&pid!=0&&UInt32.TryParse(p[2],out tid)&&tid!=0&&Guid.TryParseExact(p[3],"N",out nonce)&&nonce!=Guid.Empty;
 }
 internal bool TryClaim(out string result){
  result=null;
  try{bool accepted=TryClaimCore(out result);if(Telemetry!=null){if(accepted)Telemetry.Record(AegisDesk.PptReader.ExternalTrace.Stage.Admission,AegisDesk.PptReader.ExternalTrace.Result.Success);else Telemetry.Refuse(AegisDesk.PptReader.ExternalTrace.Stage.Admission,AegisDesk.PptReader.Connect.TraceReason(result));}return accepted;}
  catch(Exception error){if(Telemetry!=null)Telemetry.Error(error,false);throw;}
 }
 private bool TryClaimCore(out string result){
  result="identity_observation_protected";if(!AegisDesk.PptReader.ObservationFence.Open)return false;
  result="owner_rejected";int apartment,qualifier;
  if(CoGetApartmentType(out apartment,out qualifier)!=0||apartment!=3||qualifier!=0)return false;
  uint owner=GetCurrentThreadId();int pid=Process.GetCurrentProcess().Id;if(Telemetry!=null)Telemetry.Record(AegisDesk.PptReader.ExternalTrace.Stage.Owner,AegisDesk.PptReader.ExternalTrace.Result.Success,(int)owner,System.Threading.Thread.CurrentThread.ManagedThreadId,apartment);
  try{if(!Binding(owner,pid)){result="owner_binding_rejected";return false;}}catch{result="owner_binding_rejected";return false;}
  string record="CLAIMED|"+pid+"|"+owner+"|"+Guid.NewGuid().ToString("N"),current;
  // No Office call, user callback, logging or message pump inside read/write/read.
  try{
   EnvRead read=Environment.Read(out current);
   if(read==EnvRead.Failure){result="environment_read_failed";return false;}
   if(read==EnvRead.Present){result=WellFormed(current)?"already_claimed":"malformed_fence";return false;}
   if(!Environment.Write(record)){result="environment_write_failed";return false;}
   if(Environment.Read(out current)!=EnvRead.Present||current!=record){result="environment_readback_failed";return false;}
  }catch(Exception){result="environment_api_exception";return false;}
  try{
   if(!Binding(owner,pid)||CoGetApartmentType(out apartment,out qualifier)!=0||apartment!=3||qualifier!=0||GetCurrentThreadId()!=owner||Environment.Read(out current)!=EnvRead.Present||current!=record){result="claim_recheck_failed";return false;}
   result="admitted";return true;
  }catch(Exception){result="binding_or_callback_failed";return false;}
  // No path clears or downgrades the process record. It is a retry fence, not authentication.
 }
}

#endif
