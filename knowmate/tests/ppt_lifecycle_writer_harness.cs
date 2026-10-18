using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;
namespace AegisDesk.PptReader {
 internal static partial class PptLifecycleFile {
  internal static string Mode, Output; internal static int At, Opens, Directories, InfoCalls, PathCalls;
  internal static List<SafeFileHandle> Handles=new List<SafeFileHandle>();
  private static Dictionary<IntPtr,string> Paths=new Dictionary<IntPtr,string>();
  private static IntPtr FileHandle;
  [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true,EntryPoint="CreateFileW")]
  private static extern SafeFileHandle NativeCreate(string path,uint access,uint share,IntPtr security,uint disposition,uint flags,IntPtr template);
  private static SafeFileHandle CreateFile(string path,uint access,uint share,IntPtr security,uint disposition,uint flags,IntPtr template) {
   Opens++;SafeFileHandle handle;
   if(disposition==3){Directories++;
    if((Mode=="open"&&Directories==At)||Mode=="missing"||Mode=="sharing")handle=new SafeFileHandle(new IntPtr(-1),false);
    else handle=new SafeFileHandle(new IntPtr(10000+Opens),false);
   } else {
    if(disposition!=1||share!=0||(flags&0x00200000)==0)throw new Exception("unsafe_create");
    if(Mode=="existing")handle=new SafeFileHandle(new IntPtr(-1),false);
    else handle=NativeCreate(Output,access,share,security,disposition,flags,template);
    FileHandle=handle.DangerousGetHandle();
   }
   Handles.Add(handle);Paths[handle.DangerousGetHandle()]=path;return handle;
  }
  private static bool GetFileInformationByHandle(SafeFileHandle handle,out Info info) {
   InfoCalls++;bool file=handle.DangerousGetHandle()==FileHandle;
   info=new Info {attributes=file?0u:16u,volume=42,links=1};
   if(!file){
    if(Mode=="info"&&InfoCalls==At)return false;
    if(Mode=="reparse"&&InfoCalls==At)info.attributes|=0x400;
    if(Mode=="notdir"&&InfoCalls==At)info.attributes=0;
    if(Mode=="volume"&&InfoCalls==At)info.volume=43;
   }else {
    if(Mode=="fileinfo")return false;
    if(Mode=="file_reparse")info.attributes=0x400;
    if(Mode=="file_directory")info.attributes=16;
    if(Mode=="links")info.links=2;
    if(Mode=="file_volume")info.volume=43;
   }
   return true;
  }
  private static uint GetFinalPathNameByHandle(SafeFileHandle handle,StringBuilder path,uint length,uint flags) {
   PathCalls++;if(Mode=="patherror"&&PathCalls==At)return 0;
   if(Mode=="pathlong"&&PathCalls==At)return length;
   path.Append(@"\\?\"+Paths[handle.DangerousGetHandle()]+(Mode=="pathmismatch"&&PathCalls==At?"wrong":""));return (uint)path.Length;
  }
  private static uint GetDriveType(string root){return Mode=="remote"?4u:3u;}
  private static bool GetVolumeInformationByHandleW(SafeFileHandle h,StringBuilder name,uint length,out uint serial,out uint max,out uint flags,StringBuilder fs,uint fsLength) {
   serial=Mode=="serial"?43u:42u;max=255;flags=0;fs.Append(Mode=="filesystem"?"FAT32":"NTFS");return Mode!="volumeapi";
  }
  internal static void Reset(string mode,int at,string output){Mode=mode;At=at;Output=output;Opens=Directories=InfoCalls=PathCalls=0;Handles.Clear();Paths.Clear();FileHandle=IntPtr.Zero;}
 }
}
class WriterHarness {
 static int cases;
 static void Check(bool b,string s){if(!b)throw new Exception(s);}
 static void Run(string mode,int at,string root) {
  string path=Path.Combine(root,"case-"+(cases++)+".log");AegisDesk.PptReader.PptLifecycleFile.Reset(mode,at,path);
  AegisDesk.PptReader.PptLifecycleFile.WriteOnce("fixed-numeric-trace", @"C:\one\two\three\four\five\six");
  foreach(var h in AegisDesk.PptReader.PptLifecycleFile.Handles)Check(h.IsClosed,"leaked_"+mode+"_"+at);
  if(mode=="success")Check(File.ReadAllText(path)=="fixed-numeric-trace","success_data");
  else Check(!File.Exists(path)||new FileInfo(path).Length==0,"failure_wrote_"+mode);
 }
 static int Main(string[] args){try{
  Environment.SetEnvironmentVariable("AEGIS_PPT_LIFECYCLE_TRACE",null);
  Environment.SetEnvironmentVariable("AEGIS_PPT_LIFECYCLE_DIR",@"C:\one\two");
  AegisDesk.PptReader.PptLifecycleFile.Reset("success",0,Path.Combine(args[0],"disabled.log"));
  var disabled=new AegisDesk.PptReader.PptLifecycleTrace();disabled.BindOwner(System.Threading.Thread.CurrentThread.ManagedThreadId);
  Check(disabled.Begin()==0&&AegisDesk.PptReader.PptLifecycleFile.Opens==0,"disabled_directory_io");
  Run("success",0,args[0]);int dirs=AegisDesk.PptReader.PptLifecycleFile.Directories;
  foreach(string mode in new[]{"open","info","reparse","notdir","patherror","pathlong","pathmismatch"})
   for(int i=1;i<=dirs;i++)Run(mode,i,args[0]);
  for(int i=2;i<=dirs;i++)Run("volume",i,args[0]);
  foreach(string mode in new[]{"missing","existing","sharing","remote","filesystem","serial","volumeapi","fileinfo","file_reparse","file_directory","links","file_volume","write"})Run(mode,0,args[0]);
  Console.WriteLine("PASS writer_cases="+cases+" directory_depth="+dirs+" all_handles_closed");return 0;
 }catch(Exception e){Console.WriteLine("FAIL "+e);return 1;}}
}
