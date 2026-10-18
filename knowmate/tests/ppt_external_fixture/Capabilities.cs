// Owned native capabilities for the offline integration fixture only.
using System;
using System.Runtime.InteropServices;
internal sealed class Capabilities:IDisposable {
 [DllImport("kernel32.dll",SetLastError=true)]static extern IntPtr CreateEvent(IntPtr sa,bool manual,bool initial,string name);
 [DllImport("kernel32.dll")]static extern IntPtr GetCurrentProcess();
 [DllImport("kernel32.dll",SetLastError=true)]static extern bool DuplicateHandle(IntPtr source,IntPtr value,IntPtr target,out IntPtr copy,uint access,bool inherit,uint options);
 [DllImport("kernel32.dll",SetLastError=true)]static extern bool GetHandleInformation(IntPtr h,out uint flags);
 [DllImport("kernel32.dll",SetLastError=true)]static extern uint WaitForSingleObject(IntPtr h,uint ms);
 [DllImport("kernel32.dll",SetLastError=true)]static extern bool SetEvent(IntPtr h);
 [DllImport("kernel32.dll",SetLastError=true)]static extern bool CloseHandle(IntPtr h);
 [DllImport("kernelbase.dll")]static extern bool CompareObjectHandles(IntPtr a,IntPtr b);
 [DllImport("ntdll.dll")]static extern int NtQueryObject(IntPtr h,int kind,IntPtr data,int length,out int needed);
 readonly IntPtr[] original=new IntPtr[3],handles=new IntPtr[4];bool closed;
 internal int AckCalls,ReleaseCalls;internal Action Validating;
 internal Capabilities(){for(int i=0;i<3;i++)original[i]=CreateEvent(IntPtr.Zero,true,false,null);for(int i=0;i<4;i++)if(!DuplicateHandle(GetCurrentProcess(),i==3?GetCurrentProcess():original[i],GetCurrentProcess(),out handles[i],i==1?0x100002u:0x100000u,true,0))throw new Exception("duplicate");}
 static bool Valid(IntPtr h,string expected,int rights){
  uint flags;if(!GetHandleInformation(h,out flags)||(flags&1)==0)return false;
  IntPtr b=Marshal.AllocHGlobal(2048);try{int n;if(NtQueryObject(h,0,b,56,out n)!=0||Marshal.ReadInt32(b,4)!=rights)return false;if(NtQueryObject(h,2,b,2048,out n)!=0)return false;string type=Marshal.PtrToStringUni(Marshal.ReadIntPtr(b,IntPtr.Size==8?8:4),(ushort)Marshal.ReadInt16(b)/2);if(type!=expected)return false;return expected!="Event"||NtQueryObject(h,1,b,2048,out n)==0&&Marshal.ReadInt16(b)==0;}finally{Marshal.FreeHGlobal(b);}
 }
 internal bool Validate(){
  if(Validating!=null)Validating();
  for(int i=0;i<4;i++)if(!Valid(handles[i],i==3?"Process":"Event",i==1?0x100002:0x100000)||WaitForSingleObject(handles[i],0)!=258)return false;
  for(int i=0;i<3;i++)for(int j=0;j<i;j++)if(CompareObjectHandles(handles[i],handles[j]))return false;
  return true;
 }
 internal void Ack(){AckCalls++;if(!SetEvent(handles[1]))throw new Exception("ack_failed");}
 internal void Release(){ReleaseCalls++;if(closed)throw new Exception("double_release");closed=true;foreach(IntPtr h in handles)if(!CloseHandle(h))throw new Exception("close_failed");}
 public void Dispose(){if(!closed){closed=true;foreach(IntPtr h in handles)CloseHandle(h);}foreach(IntPtr h in original)CloseHandle(h);}
}
