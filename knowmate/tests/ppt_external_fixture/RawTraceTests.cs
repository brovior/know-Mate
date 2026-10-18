using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Threading;
using AegisDesk.PptReader;
static class RawTraceTests {
 [DllImport("oleaut32.dll",PreserveSig=false)]static extern void LoadRegTypeLib(ref Guid id,ushort major,ushort minor,int lcid,out ITypeLib lib);
 static ITypeInfo Find(ITypeLib lib,string wanted){for(int i=0;i<lib.GetTypeInfoCount();i++){string name,doc,file;int help;lib.GetDocumentation(i,out name,out doc,out help,out file);if(name==wanted){ITypeInfo info;lib.GetTypeInfo(i,out info);return info;}}throw new Exception("fixture_type_missing");}
 [STAThread]static int Main(){try{
  Guid id=new Guid("91493440-5A91-11CF-8700-00AA0060263B");ITypeLib lib;LoadRegTypeLib(ref id,2,12,0,out lib);
  var host=new NativeObject(Find(lib,"_Application"),false);var child=new NativeObject(Find(lib,"Presentations"),true);child.Mode="exception_nonfatal";host.Children["Presentations"]=child;object application=Marshal.GetObjectForIUnknown(host.Pointer);
  var trace=new ExternalTrace();trace.Start();string text=null;var done=new ManualResetEvent(false);
  trace.TestWriter=(value,slot)=>{text=value;done.Set();};ExternalTrace.Current=trace;
  try{
   try{RawCount.Read(application,"Presentations",()=>{});throw new Exception("expected_native_failure");}
   catch(COMException error){if(error.HResult!=unchecked((int)0x80004005))throw;}
   var errors=new RawCount.Errors();errors.Primary=new COMException("primary",unchecked((int)0x80004005));
   errors.Clean("synthetic_cleanup",()=>{throw new COMException("cleanup",unchecked((int)0x80010108));});
   try{errors.ThrowIfAny();throw new Exception("expected_cleanup_failure");}
   catch(CountCleanupFailed error){if(error.InnerException.HResult!=unchecked((int)0x80010108))throw;}
  }finally{ExternalTrace.Current=null;}
  trace.ExportTerminal(false);
  if(!done.WaitOne(3000)||!text.Contains("PrimaryError Failed -2147467259")||!text.Contains("CleanupError Failed -2147417848"))throw new Exception("raw_errors_not_separated");
  GC.KeepAlive(host);GC.KeepAlive(child);GC.KeepAlive(application);Console.WriteLine("PASS native_Count_primary_and_cleanup_trace_original_priority");return 0;
 }catch(Exception error){Console.WriteLine(error);return 1;}}
}
