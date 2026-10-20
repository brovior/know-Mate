#if AEGIS_PPT_CAPTURE_DIAGNOSTIC
using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using Microsoft.Win32.SafeHandles;
using AegisDesk.PptReader;

// Launched before Office. This process never loads COM/PIA or invokes Office.
internal static class PptCaptureCollector {
 [DllImport("kernel32.dll")]static extern IntPtr GetStdHandle(int id);
 [DllImport("kernel32.dll",SetLastError=true)]static extern uint WaitForSingleObject(IntPtr handle,uint ms);
 static int Read(IntPtr view,int offset){int result=Marshal.ReadInt32(view,offset);Thread.MemoryBarrier();return result;}
 static void Header(StreamWriter writer,IntPtr view,Guid run){
  writer.WriteLine("{\"kind\":\"header\",\"schema\":3,\"layout\":1,\"run_id\":\""+run.ToString("N")+"\",\"pid\":"+Read(view,48)+",\"creation\":"+Marshal.ReadInt64(view,56)+",\"frequency\":"+Marshal.ReadInt64(view,64)+",\"owner_native_thread\":"+Read(view,52)+",\"published_seq\":"+Read(view,72)+",\"dropped\":"+Read(view,76)+",\"offthread\":"+Read(view,80)+",\"append_reentry\":"+Read(view,84)+",\"capture_start_ticks\":"+Marshal.ReadInt64(view,96)+",\"pre_attach_records\":"+Read(view,104)+",\"owner_managed_thread\":"+Read(view,112)+",\"apartment\":"+Read(view,116)+",\"qualifier\":"+Read(view,120)+",\"capture_scope\":\"bind_to_capture_end\",\"counters_are_lower_bounds\":true}");
 }
 static void Slot(StreamWriter writer,IntPtr slot,int seq){
  int actual=Marshal.ReadInt32(slot,0);if(actual!=seq)throw new InvalidDataException("slot_sequence");
  string[] names={"stage_id","outcome_id","a","b","c","native_thread","managed_thread","service_depth","event_depth","generation","apartment","qualifier"};
  int[] offsets={4,16,20,24,28,32,36,40,44,48,52,56};
  var text=new StringBuilder("{\"kind\":\"slot\",\"seq\":"+seq+",\"qpc_ticks\":"+Marshal.ReadInt64(slot,8));
  for(int i=0;i<names.Length;i++)text.Append(",\""+names[i]+"\":"+Marshal.ReadInt32(slot,offsets[i]));
  for(int i=60;i<PptCaptureBuffer.SlotSize;i++)if(Marshal.ReadByte(slot,i)!=0)throw new InvalidDataException("slot_reserved");
  text.Append("}");writer.WriteLine(text.ToString());
 }
 static int Main(string[] args){
  IntPtr view=IntPtr.Zero;StreamWriter writer=null;FileStream output=null;
  try{
   if(args.Length!=4)return 2;
   long mappingNumber,stopNumber,parentNumber;Guid run;
   if(!Int64.TryParse(args[0],out mappingNumber)||!Int64.TryParse(args[1],out stopNumber)||!Int64.TryParse(args[2],out parentNumber)||mappingNumber<=0||stopNumber<=0||parentNumber<=0||!Guid.TryParseExact(args[3],"N",out run))return 2;
   IntPtr mapping=new IntPtr(mappingNumber),stop=new IntPtr(stopNumber),parent=new IntPtr(parentNumber);
   if(!PptCaptureBuffer.IsSection(mapping))return 3;
   view=PptCaptureBuffer.MapViewOfFile(mapping,4,0,0,new UIntPtr((uint)PptCaptureBuffer.Size));if(view==IntPtr.Zero||!PptCaptureBuffer.HeaderValid(view,run,false))return 3;
   output=new FileStream(new SafeFileHandle(GetStdHandle(-11),false),FileAccess.Write);writer=new StreamWriter(output,new UTF8Encoding(false));
   writer.WriteLine("{\"kind\":\"collector_ready\",\"run_id\":\""+run.ToString("N")+"\"}");writer.Flush();output.Flush(true);
   int copied=0;bool ready=false;long lastFlush=Stopwatch.GetTimestamp();bool stopNow=false;
   while(true){
    uint stopped=WaitForSingleObject(stop,0),gone=WaitForSingleObject(parent,0);
    if((stopped!=0&&stopped!=258)||(gone!=0&&gone!=258))throw new InvalidDataException("wait_failed");
    stopNow=stopped==0||gone==0;
    if(Read(view,88)==1){
     if(!ready){Header(writer,view,run);ready=true;}
     int count=Read(view,72);if(count<copied||count>PptCaptureBuffer.Capacity)throw new InvalidDataException("published_sequence");
     while(copied<count){Slot(writer,IntPtr.Add(view,PptCaptureBuffer.HeaderSize+copied*PptCaptureBuffer.SlotSize),copied+1);copied++;}
    }
    if(stopNow)break;
    long now=Stopwatch.GetTimestamp();if(now-lastFlush>=Stopwatch.Frequency){writer.Flush();output.Flush(true);lastFlush=now;}
    Thread.Sleep(20);
   }
   if(ready)Header(writer,view,run);
   writer.WriteLine("{\"kind\":\"collector_end\",\"persisted_seq\":"+copied+",\"producer_ready\":"+(ready?"true":"false")+"}");writer.Flush();output.Flush(true);return 0;
  }catch{try{if(writer!=null){writer.WriteLine("{\"kind\":\"collector_error\",\"reason\":\"collection_failed\"}");writer.Flush();if(output!=null)output.Flush(true);}}catch{}return 4;}
  finally{if(writer!=null)writer.Dispose();else if(output!=null)output.Dispose();if(view!=IntPtr.Zero)PptCaptureBuffer.UnmapViewOfFile(view);}
 }
}
#endif
