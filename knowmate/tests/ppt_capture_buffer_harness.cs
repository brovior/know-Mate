using System;
using System.Diagnostics;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Threading;
using AegisDesk.PptReader;

// Office-free native transport tests. Each execution owns only its anonymous mapping.
internal static class CaptureBufferHarness {
 [DllImport("kernel32.dll",SetLastError=true)]static extern IntPtr CreateFileMapping(IntPtr file,IntPtr security,uint protect,uint high,uint low,string name);
 [DllImport("kernel32.dll")]static extern IntPtr GetCurrentProcess();
 [DllImport("kernel32.dll",SetLastError=true)]static extern bool GetProcessTimes(IntPtr process,out long born,out long exit,out long kernel,out long user);
 [DllImport("kernel32.dll",SetLastError=true)]static extern bool DuplicateHandle(IntPtr source,IntPtr handle,IntPtr target,out IntPtr copy,uint access,bool inherit,uint options);
 [DllImport("kernel32.dll",SetLastError=true)]static extern bool GetHandleInformation(IntPtr handle,out uint flags);
 [DllImport("kernel32.dll",SetLastError=true)]static extern IntPtr CreateEvent(IntPtr security,bool manual,bool initial,string name);
 [DllImport("ole32.dll")]static extern int CoInitializeEx(IntPtr reserved,uint model);
 [DllImport("ole32.dll")]static extern void CoUninitialize();
 static IntPtr mapping,view;static Guid run=Guid.NewGuid();
 internal static long Creation(){long born,exit,kernel,user;if(!GetProcessTimes(GetCurrentProcess(),out born,out exit,out kernel,out user))throw new Exception("creation");return born;}
 static void Require(bool valid,string reason){if(!valid)throw new Exception(reason);}
 static void Header(){
  for(int i=0;i<PptCaptureBuffer.HeaderSize;i++)Marshal.WriteByte(view,i,0);
  Marshal.Copy(PptCaptureBuffer.Magic,0,view,8);
  int[] offsets={8,12,16,20,24,28,48,52};int[] values={3,1,PptCaptureBuffer.Capacity,PptCaptureBuffer.SlotSize,PptCaptureBuffer.HeaderSize,1,Process.GetCurrentProcess().Id,(int)PptCaptureBuffer.GetCurrentThreadId()};
  for(int i=0;i<offsets.Length;i++)Marshal.WriteInt32(view,offsets[i],values[i]);
  Marshal.Copy(run.ToByteArray(),0,IntPtr.Add(view,32),16);Marshal.WriteInt64(view,56,Creation());Marshal.WriteInt64(view,64,Stopwatch.Frequency);
 }
 static IntPtr Supplied(){IntPtr result;Require(DuplicateHandle(GetCurrentProcess(),mapping,GetCurrentProcess(),out result,0,false,2),"duplicate");Environment.SetEnvironmentVariable("AEGIS_PPT_CAPTURE_MAPPING",result.ToInt64().ToString());return result;}
 static PptCaptureBuffer Adopt(){Supplied();var buffer=PptCaptureBuffer.TryAdopt(3);Require(buffer!=null,"adopt");return buffer;}
 static void Reject(string reason){IntPtr supplied=Supplied();Require(PptCaptureBuffer.TryAdopt()==null,reason);uint flags;Require(GetHandleInformation(supplied,out flags),"rejected_handle_closed");PptCaptureBuffer.CloseHandle(supplied);}
 static void RejectBindings(){
  Header();Marshal.WriteInt32(view,12,2);Reject("layout_accepted");
  Header();Marshal.WriteInt32(view,48,1);Reject("pid_accepted");
  Header();Marshal.WriteInt32(view,52,0);Reject("thread_accepted");
  Header();Marshal.WriteInt64(view,56,0);Reject("creation_accepted");
  Header();Marshal.WriteInt64(view,64,1);Reject("frequency_accepted");
  Header();Marshal.WriteByte(view,32,(byte)(Marshal.ReadByte(view,32)^255));Reject("run_accepted");
  Header();Marshal.WriteInt32(view,72,1);Reject("reused_accepted");
  Header();Marshal.WriteInt32(view,88,1);Reject("ready_accepted");
  IntPtr signal=CreateEvent(IntPtr.Zero,true,false,null);Require(signal!=IntPtr.Zero,"event");
  try{Environment.SetEnvironmentVariable("AEGIS_PPT_CAPTURE_MAPPING",signal.ToInt64().ToString());Require(PptCaptureBuffer.TryAdopt()==null,"wrong_type_accepted");uint flags;Require(GetHandleInformation(signal,out flags),"event_closed");}finally{PptCaptureBuffer.CloseHandle(signal);}
  IntPtr readOnly;Require(DuplicateHandle(GetCurrentProcess(),mapping,GetCurrentProcess(),out readOnly,4,false,0),"readonly_duplicate");
  try{Environment.SetEnvironmentVariable("AEGIS_PPT_CAPTURE_MAPPING",readOnly.ToInt64().ToString());Require(PptCaptureBuffer.TryAdopt()==null,"readonly_accepted");uint flags;Require(GetHandleInformation(readOnly,out flags),"readonly_closed");}finally{PptCaptureBuffer.CloseHandle(readOnly);}
  IntPtr originalMapping=mapping,originalView=view;
  mapping=CreateFileMapping(new IntPtr(-1),IntPtr.Zero,4,0,256,null);Require(mapping!=IntPtr.Zero,"small_mapping");
  view=PptCaptureBuffer.MapViewOfFile(mapping,2,0,0,new UIntPtr(256));Require(view!=IntPtr.Zero,"small_view");
  try{Header();Reject("small_mapping_accepted");}finally{PptCaptureBuffer.UnmapViewOfFile(view);PptCaptureBuffer.CloseHandle(mapping);mapping=originalMapping;view=originalView;}
 }
 static void Publish(){
  Header();var buffer=Adopt();buffer.SetGeneration(7);buffer.EnterService();buffer.Record(35,1,0,0,0);buffer.EnterEvent();buffer.Record(37,1,5,1,0);buffer.Record(38,1,5,3,0);buffer.LeaveEvent();buffer.Record(36,1,0,0,0);buffer.LeaveService();
  Require(Marshal.ReadInt32(view,72)==4,"nested_count");Require(Marshal.ReadInt32(view,104)==3,"pre_attach");Require(Marshal.ReadInt64(view,96)>0,"attach_time");
  int[] depths={0,1,1,0};for(int i=0;i<4;i++){IntPtr slot=IntPtr.Add(view,PptCaptureBuffer.HeaderSize+i*PptCaptureBuffer.SlotSize);Require(Marshal.ReadInt32(slot)==i+1&&Marshal.ReadInt32(slot,40)==1&&Marshal.ReadInt32(slot,44)==depths[i]&&Marshal.ReadInt32(slot,48)==7,"nested_depth");}
  var worker=new Thread(delegate(){buffer.Record(0,0,0,0,0);});worker.Start();Require(worker.Join(2000),"offthread_blocked");Require(Marshal.ReadInt32(view,80)>0&&Marshal.ReadInt32(view,72)==4,"offthread_loss");
  typeof(PptCaptureBuffer).GetField("appending",BindingFlags.Instance|BindingFlags.NonPublic).SetValue(buffer,1);buffer.Record(0,0,0,0,0);typeof(PptCaptureBuffer).GetField("appending",BindingFlags.Instance|BindingFlags.NonPublic).SetValue(buffer,0);Require(Marshal.ReadInt32(view,84)>0,"reentry_loss");
  bool done=false;Exception failed=null;var reader=new Thread(delegate(){try{int prior=0;while(!Volatile.Read(ref done)){int count=Marshal.ReadInt32(view,72);Thread.MemoryBarrier();Require(count>=prior&&count<=PptCaptureBuffer.Capacity,"count_order");for(int i=prior;i<count;i++){IntPtr slot=IntPtr.Add(view,PptCaptureBuffer.HeaderSize+i*PptCaptureBuffer.SlotSize);Require(Marshal.ReadInt32(slot)==i+1&&Marshal.ReadInt64(slot,8)>0,"partial_slot");for(int reserved=60;reserved<128;reserved++)Require(Marshal.ReadByte(slot,reserved)==0,"reserved");}prior=count;}}catch(Exception error){failed=error;}});reader.Start();
  for(int i=4;i<PptCaptureBuffer.Capacity+10;i++)buffer.Record(14,1,i,0,0);
  Volatile.Write(ref done,true);Require(reader.Join(2000),"reader_blocked");if(failed!=null)throw failed;
  Require(Marshal.ReadInt32(view,72)==PptCaptureBuffer.Capacity&&Marshal.ReadInt32(view,76)>0&&!buffer.Healthy,"overflow");
  Marshal.WriteInt32(view,76,int.MaxValue);buffer.Record(14,1,0,0,0);Require(Marshal.ReadInt32(view,76)==int.MaxValue,"counter_wrap");
 }
 static void LazyAttach(){
  Header();Marshal.WriteInt32(view,48,0);Supplied();var trace=new ExternalTrace();trace.Generation(9);
  // Connect may be constructed before the parent has bound the target header.
  Header();trace.Start();trace.Record(ExternalTrace.Stage.Bind,ExternalTrace.Result.Success);
  Require(trace.CaptureHealthy&&Marshal.ReadInt32(view,72)==1,"lazy_attach");Require(Marshal.ReadInt32(IntPtr.Add(view,PptCaptureBuffer.HeaderSize),48)==9,"lazy_generation");
  trace.Start();trace.Record(ExternalTrace.Stage.Quit,ExternalTrace.Result.Entered);Require(Marshal.ReadInt32(view,72)==2,"attach_once");
 }
 [STAThread]static int Main(string[] args){bool initialized=false;
  try{
   int apartment=CoInitializeEx(IntPtr.Zero,2);Require(apartment==0||apartment==1,"sta_initialize");initialized=true;
   Environment.SetEnvironmentVariable("AEGIS_PPT_CAPTURE_DIAGNOSTIC","1");Environment.SetEnvironmentVariable("AEGIS_PPT_EXTERNAL_DIAGNOSTIC","1");Environment.SetEnvironmentVariable("AEGIS_PPT_LIFECYCLE_TRACE","1");Environment.SetEnvironmentVariable("AEGIS_PPT_TRACE_RUN",run.ToString("N"));
   mapping=CreateFileMapping(new IntPtr(-1),IntPtr.Zero,4,0,(uint)PptCaptureBuffer.Size,null);Require(mapping!=IntPtr.Zero,"mapping");view=PptCaptureBuffer.MapViewOfFile(mapping,2,0,0,new UIntPtr((uint)PptCaptureBuffer.Size));Require(view!=IntPtr.Zero,"view");
   if(args.Length==1&&args[0]=="lazy")LazyAttach();else{RejectBindings();Publish();}
   Console.WriteLine("{\"result\":\"PASS\",\"office_started\":false}");return 0;
  }catch(Exception error){Console.WriteLine("{\"result\":\"FAIL\",\"reason\":\""+error.GetType().Name+"\"}");return 1;}
  finally{if(view!=IntPtr.Zero)PptCaptureBuffer.UnmapViewOfFile(view);if(mapping!=IntPtr.Zero)PptCaptureBuffer.CloseHandle(mapping);if(initialized)CoUninitialize();}
 }
}
namespace AegisDesk.PptReader {
 internal static class ProcessFence {internal static long Creation(){return CaptureBufferHarness.Creation();}}
 internal static class PptLifecycleFile {internal static string CaptureDirectory(string value){throw new Exception("disk_path_not_allowed");}internal static bool WriteExternalSnapshot(string value,string directory,int slot){throw new Exception("disk_write_not_allowed");}}
 internal sealed class PptCleanupErrors {internal Exception Primary,Cleanup,StickyFatal;}
}
