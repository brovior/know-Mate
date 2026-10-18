#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
using System;
using System.Runtime.InteropServices;
using System.Text;
// Monotonic lifecycle record: missing -> BLOCKED -> RESOLVED. Never delete/reset.
// Unknown or unconfirmed disconnect remains BLOCKED even when the old domain disappears.
internal static class ReaderFence {
 const string Key="AEGIS_PPT_EXTERNAL_READER_V1";
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]static extern uint GetEnvironmentVariable(string name,StringBuilder value,uint size);
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]static extern bool SetEnvironmentVariable(string name,string value);
 [DllImport("kernel32.dll")]static extern void SetLastError(uint error);
 static string Read(){var b=new StringBuilder(32);SetLastError(0);uint n=GetEnvironmentVariable(Key,b,32);int e=Marshal.GetLastWin32Error();if(n==0&&e==203)return null;if(n==0||n>=32)return "INVALID";return b.ToString();}
 internal static bool AllowsRead(){if(!AegisDesk.PptReader.ObservationFence.Open)return false;try{string state=Read();return state==null||state=="RESOLVED";}catch{return false;}}
 internal static bool Begin(){try{return Read()==null&&SetEnvironmentVariable(Key,"BLOCKED")&&Read()=="BLOCKED";}catch{return false;}}
 internal static void Resolve(ExternalProtocol session){
  // Called only by the private session created after this admission's TryClaim success.
  if(session.State!=ExternalProtocol.Phase.Terminal||session.Error!=null||session.CleanupFailed)return;
  var state=session.Guard.ExternalState;string outcome=session.Guard.ExternalOutcome;
  if(state!=null&&state.Active||outcome=="unknown"||outcome=="disconnect_cleanup_unconfirmed")return;
  try{if(Read()=="BLOCKED")SetEnvironmentVariable(Key,"RESOLVED");}catch{/* Read gate remains blocked on uncertainty. */}
 }
}

#endif
