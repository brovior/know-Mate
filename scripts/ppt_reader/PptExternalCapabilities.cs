#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
using System;
using System.Runtime.InteropServices;
internal sealed class NativeCapabilities:IDisposable {
 [DllImport("kernel32.dll",SetLastError=true)]static extern bool GetHandleInformation(IntPtr h,out uint flags);
 [DllImport("kernel32.dll",SetLastError=true)]static extern uint WaitForSingleObject(IntPtr h,uint ms);
 [DllImport("kernel32.dll",SetLastError=true)]static extern bool SetEvent(IntPtr h);
 [DllImport("kernel32.dll",SetLastError=true)]static extern bool CloseHandle(IntPtr h);
 [DllImport("kernelbase.dll")]static extern bool CompareObjectHandles(IntPtr a,IntPtr b);
 [DllImport("ntdll.dll")]static extern int NtQueryObject(IntPtr h,int kind,IntPtr data,int size,out int required);
 readonly AegisDesk.PptReader.ExternalTrace trace;
 IntPtr[] owned;internal int CloseAttempts,Closed,Acks;internal Exception CleanupError;
 internal bool IsClosed {get{return owned==null;}}
 NativeCapabilities(IntPtr[] handles,AegisDesk.PptReader.ExternalTrace telemetry){owned=handles;trace=telemetry;}
 static void Note(AegisDesk.PptReader.ExternalTrace trace,int role,int step,int error=0){if(trace!=null){trace.Refuse(AegisDesk.PptReader.ExternalTrace.Stage.Capabilities,step*10+role);trace.Record(AegisDesk.PptReader.ExternalTrace.Stage.Capabilities,AegisDesk.PptReader.ExternalTrace.Result.Refused,role,step,error);}}
 static bool Valid(IntPtr h,string type,int rights,AegisDesk.PptReader.ExternalTrace trace,int role){
  uint flags;if(h==IntPtr.Zero){Note(trace,role,1);return false;}
  if(!GetHandleInformation(h,out flags)){int error=Marshal.GetLastWin32Error();Note(trace,role,2,error);return false;}
  if((flags&1)==0){Note(trace,role,3);return false;}
  IntPtr b=Marshal.AllocHGlobal(2048);try{int n;
   int status=NtQueryObject(h,0,b,56,out n);if(status!=0){Note(trace,role,4,status);return false;}
   if(Marshal.ReadInt32(b,4)!=rights){Note(trace,role,5);return false;}
   status=NtQueryObject(h,2,b,2048,out n);if(status!=0){Note(trace,role,6,status);return false;}
   if(Marshal.PtrToStringUni(Marshal.ReadIntPtr(b,IntPtr.Size==8?8:4),(ushort)Marshal.ReadInt16(b)/2)!=type){Note(trace,role,7);return false;}
   if(type!="Event")return true;
   status=NtQueryObject(h,1,b,2048,out n);if(status!=0){Note(trace,role,8,status);return false;}
   if(Marshal.ReadInt16(b)!=0){Note(trace,role,9);return false;}return true;
  }finally{Marshal.FreeHGlobal(b);}
 }
 static bool Signal(IntPtr h){uint wait=WaitForSingleObject(h,0);if(wait!=0&&wait!=258)throw new InvalidOperationException("capability_wait_failed");return wait==0;}
 internal static NativeCapabilities Capture(out string reason,AegisDesk.PptReader.ExternalTrace trace=null){
  reason="capability_invalid";var handles=new IntPtr[4];string[] names={"START","ACK","COMPLETE","CONTROLLER"};
  try{
   for(int i=0;i<4;i++){long value;if(!Int64.TryParse(Environment.GetEnvironmentVariable("AEGIS_TEST_CAP_"+names[i]),out value)){Note(trace,i,0);return null;}handles[i]=new IntPtr(value);if(!Valid(handles[i],i==3?"Process":"Event",i==1?0x100002:0x100000,trace,i))return null;}
   for(int i=0;i<3;i++)for(int j=0;j<i;j++)if(CompareObjectHandles(handles[i],handles[j])){Note(trace,i,10,j);return null;}
   for(int i=0;i<4;i++)if(Signal(handles[i])){Note(trace,i,11);return null;}
   reason="adopted";if(trace!=null)trace.Record(AegisDesk.PptReader.ExternalTrace.Stage.Capabilities,AegisDesk.PptReader.ExternalTrace.Result.Success,4);return new NativeCapabilities(handles,trace);
  }catch(Exception error){if(trace!=null)trace.Error(error,false);reason="capability_failure";return null;}
  // Invalid input has no owner and is never explicitly closed here.
 }
 internal bool PreStart(){if(IsClosed){Note(trace,-1,12);return false;}for(int i=0;i<4;i++)if(Signal(owned[i])){Note(trace,i,13);return false;}return true;}
 internal bool Start {get{return Signal(owned[0]);}}
 internal bool Complete {get{return Signal(owned[2]);}}
 internal bool ControllerAlive {get{return !Signal(owned[3]);}}
 internal void Ack(){if(IsClosed||!SetEvent(owned[1]))throw new InvalidOperationException("ack_failed");Acks++;}
 public void Dispose(){
  var handles=owned;if(handles==null)return;owned=null;
  foreach(IntPtr h in handles){CloseAttempts++;if(trace!=null)trace.Record(AegisDesk.PptReader.ExternalTrace.Stage.CapabilityClose,AegisDesk.PptReader.ExternalTrace.Result.Entered,CloseAttempts);try{if(!CloseHandle(h))throw new InvalidOperationException("close_failed");Closed++;if(trace!=null)trace.Record(AegisDesk.PptReader.ExternalTrace.Stage.CapabilityClose,AegisDesk.PptReader.ExternalTrace.Result.Success,CloseAttempts);}catch(Exception e){if(trace!=null)trace.Error(e,true);if(CleanupError==null)CleanupError=e;}}
  if(CleanupError!=null)throw CleanupError;
 }
}

#endif
