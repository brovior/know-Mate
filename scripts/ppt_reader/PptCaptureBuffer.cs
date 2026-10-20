#if AEGIS_PPT_CAPTURE_DIAGNOSTIC
using System;
using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Threading;

namespace AegisDesk.PptReader {
// The mapping is telemetry only. It never changes shutdown admission or owns COM objects.
internal sealed class PptCaptureBuffer {
 internal const int HeaderSize=256,Capacity=4096,SlotSize=128,Size=HeaderSize+Capacity*SlotSize;
 internal static readonly byte[] Magic={65,69,80,67,65,80,51,0};
 readonly IntPtr view,owned;readonly int owner,managed,apartment,qualifier;
 int appending,serviceDepth,eventDepth,generation;
 [DllImport("kernel32.dll")]internal static extern uint GetCurrentThreadId();
 [DllImport("kernel32.dll")]static extern IntPtr GetCurrentProcess();
 [DllImport("kernel32.dll",SetLastError=true)]static extern bool GetHandleInformation(IntPtr h,out uint flags);
 [DllImport("kernel32.dll",SetLastError=true)]static extern bool GetProcessTimes(IntPtr p,out long c,out long e,out long k,out long u);
 [DllImport("kernel32.dll",SetLastError=true)]internal static extern IntPtr MapViewOfFile(IntPtr h,uint access,uint high,uint low,UIntPtr length);
 [DllImport("kernel32.dll",SetLastError=true)]internal static extern bool UnmapViewOfFile(IntPtr p);
 [DllImport("kernel32.dll",SetLastError=true)]internal static extern bool CloseHandle(IntPtr h);
 [DllImport("kernel32.dll",SetLastError=true)]static extern bool DuplicateHandle(IntPtr source,IntPtr h,IntPtr target,out IntPtr duplicate,uint rights,bool inherit,uint options);
 [DllImport("ntdll.dll")]static extern int NtQueryObject(IntPtr handle,int kind,IntPtr data,int length,out int needed);
 [DllImport("ole32.dll")]static extern int CoGetApartmentType(out int apartment,out int qualifier);

 internal static bool IsSection(IntPtr handle){
  uint flags;if(handle==IntPtr.Zero||!GetHandleInformation(handle,out flags))return false;
  IntPtr memory=Marshal.AllocHGlobal(1024);
  try{int needed;if(NtQueryObject(handle,2,memory,1024,out needed)<0)return false;
   int length=(ushort)Marshal.ReadInt16(memory);IntPtr text=Marshal.ReadIntPtr(memory,IntPtr.Size==8?8:4);
   long start=memory.ToInt64(),p=text.ToInt64();
   return length==14&&p>=start&&p+length<=start+1024&&Marshal.PtrToStringUni(text,length/2)=="Section";
  }finally{Marshal.FreeHGlobal(memory);}
 }
 internal static bool HeaderValid(IntPtr p,Guid run,bool requireTarget){
  if(p==IntPtr.Zero)return false;
  for(int i=0;i<Magic.Length;i++)if(Marshal.ReadByte(p,i)!=Magic[i])return false;
  if(Marshal.ReadInt32(p,8)!=3||Marshal.ReadInt32(p,12)!=1||Marshal.ReadInt32(p,16)!=Capacity||Marshal.ReadInt32(p,20)!=SlotSize||Marshal.ReadInt32(p,24)!=HeaderSize||Marshal.ReadInt32(p,28)!=1)return false;
  byte[] id=new byte[16];Marshal.Copy(IntPtr.Add(p,32),id,0,16);if(new Guid(id)!=run)return false;
  int published=Marshal.ReadInt32(p,72);if(published<0||published>Capacity)return false;
  for(int i=92;i<96;i++)if(Marshal.ReadByte(p,i)!=0)return false;
  for(int i=108;i<112;i++)if(Marshal.ReadByte(p,i)!=0)return false;
  for(int i=124;i<HeaderSize;i++)if(Marshal.ReadByte(p,i)!=0)return false;
  if(requireTarget&&(Marshal.ReadInt32(p,48)!=Process.GetCurrentProcess().Id||Marshal.ReadInt32(p,52)!=(int)GetCurrentThreadId()))return false;
  return true;
 }
 internal static PptCaptureBuffer TryAdopt(int preAttachRecords=0){
  if(Environment.GetEnvironmentVariable("AEGIS_PPT_CAPTURE_DIAGNOSTIC")!="1")return null;
  long number;Guid run;
  if(!Int64.TryParse(Environment.GetEnvironmentVariable("AEGIS_PPT_CAPTURE_MAPPING"),NumberStyles.None,CultureInfo.InvariantCulture,out number)||number<=0||!Guid.TryParseExact(Environment.GetEnvironmentVariable("AEGIS_PPT_TRACE_RUN"),"N",out run)||run==Guid.Empty)return null;
  IntPtr supplied;try{supplied=new IntPtr(number);}catch{return null;}
  if(!IsSection(supplied))return null;
  IntPtr mapped=MapViewOfFile(supplied,2,0,0,new UIntPtr((uint)Size));if(mapped==IntPtr.Zero)return null;
  IntPtr adopted=IntPtr.Zero;
  try{
   if(!HeaderValid(mapped,run,true)||Marshal.ReadInt32(mapped,72)!=0||Marshal.ReadInt32(mapped,88)!=0||Marshal.ReadInt64(mapped,96)!=0||Marshal.ReadInt32(mapped,104)!=0||Marshal.ReadInt32(mapped,112)!=0||Marshal.ReadInt32(mapped,116)!=0||Marshal.ReadInt32(mapped,120)!=0)return null;
   long c,e,k,u;if(!GetProcessTimes(GetCurrentProcess(),out c,out e,out k,out u)||Marshal.ReadInt64(mapped,56)!=c||Marshal.ReadInt64(mapped,64)!=Stopwatch.Frequency)return null;
   int apartment,qualifier;if(CoGetApartmentType(out apartment,out qualifier)!=0||(apartment!=0&&apartment!=3))return null;
   if(!DuplicateHandle(GetCurrentProcess(),supplied,GetCurrentProcess(),out adopted,0,false,2))return null;
   var result=new PptCaptureBuffer(mapped,adopted,apartment,qualifier,preAttachRecords);
   mapped=IntPtr.Zero;adopted=IntPtr.Zero;
   // Only a validated supplied handle is closed. The view/duplicate remain alive
   // for the process lifetime so a late lifecycle callback cannot use freed memory.
   CloseHandle(supplied);return result;
  }catch{return null;}
  finally{if(mapped!=IntPtr.Zero)UnmapViewOfFile(mapped);if(adopted!=IntPtr.Zero)CloseHandle(adopted);}
 }
 PptCaptureBuffer(IntPtr p,IntPtr handle,int apt,int qual,int preAttachRecords){
  view=p;owned=handle;owner=(int)GetCurrentThreadId();managed=Thread.CurrentThread.ManagedThreadId;apartment=apt;qualifier=qual;
  Marshal.WriteInt32(view,112,managed);Marshal.WriteInt32(view,116,apartment);Marshal.WriteInt32(view,120,qualifier);
  Marshal.WriteInt64(view,96,Stopwatch.GetTimestamp());Marshal.WriteInt32(view,104,preAttachRecords);
  Thread.MemoryBarrier();Marshal.WriteInt32(view,88,1);
 }
 // One bounded CAS attempt is a lower-bound counter, never an unbounded callback spin.
 unsafe void Lost(int offset){
  int* target=(int*)((byte*)view.ToPointer()+offset);int old=Volatile.Read(ref *target);
  if(old<int.MaxValue)Interlocked.CompareExchange(ref *target,old+1,old);
 }
 internal bool Healthy {get{return Marshal.ReadInt32(view,76)==0&&Marshal.ReadInt32(view,80)==0&&Marshal.ReadInt32(view,84)==0;}}
 internal void SetGeneration(int value){if((int)GetCurrentThreadId()==owner)generation=value;}
 internal void EnterService(){if((int)GetCurrentThreadId()==owner)serviceDepth++;}
 internal void LeaveService(){if((int)GetCurrentThreadId()==owner&&serviceDepth>0)serviceDepth--;}
 internal void EnterEvent(){if((int)GetCurrentThreadId()==owner)eventDepth++;}
 internal void LeaveEvent(){if((int)GetCurrentThreadId()==owner&&eventDepth>0)eventDepth--;}
 internal void Record(int stage,int result,int a,int b,int c){
  try{
   if((int)GetCurrentThreadId()!=owner){Lost(80);return;}
   if(Interlocked.CompareExchange(ref appending,1,0)!=0){Lost(84);return;}
   try{
    int count=Marshal.ReadInt32(view,72);if(count<0||count>=Capacity){Lost(76);return;}
    IntPtr slot=IntPtr.Add(view,HeaderSize+count*SlotSize);
    Marshal.WriteInt32(slot,0,count+1);Marshal.WriteInt32(slot,4,stage);Marshal.WriteInt64(slot,8,Stopwatch.GetTimestamp());
    Marshal.WriteInt32(slot,16,result);Marshal.WriteInt32(slot,20,a);Marshal.WriteInt32(slot,24,b);Marshal.WriteInt32(slot,28,c);
    Marshal.WriteInt32(slot,32,owner);Marshal.WriteInt32(slot,36,managed);Marshal.WriteInt32(slot,40,serviceDepth);Marshal.WriteInt32(slot,44,eventDepth);
    Marshal.WriteInt32(slot,48,generation);Marshal.WriteInt32(slot,52,apartment);Marshal.WriteInt32(slot,56,qualifier);
    Thread.MemoryBarrier();Marshal.WriteInt32(view,72,count+1);
   }finally{Volatile.Write(ref appending,0);}
  }catch{try{Lost(76);}catch{/* Telemetry cannot poison an Office callback. */}}
 }
}
}
#endif
