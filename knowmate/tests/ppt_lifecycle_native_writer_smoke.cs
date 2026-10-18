using System;
using System.IO;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
using AegisDesk.PptReader;
class NativeWriterSmoke {
 static int failures;
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true,EntryPoint="CreateFileW")]
 static extern SafeFileHandle OpenDirectory(string path,uint access,uint share,IntPtr security,uint disposition,uint flags,IntPtr template);
 static void Check(bool condition,string reason){if(!condition)throw new Exception(reason);}
 static string SetRoot(string root,string name,bool create){string p=Path.Combine(root,name);if(create)Directory.CreateDirectory(Path.Combine(p,"AegisDesk","ppt-lifecycle"));return Path.Combine(p,"AegisDesk","ppt-lifecycle");}
 static string Output(string dir){return Path.Combine(dir,"host-"+System.Diagnostics.Process.GetCurrentProcess().Id+".log");}
 static void Released(string directory,string root){string to=directory+"-moved";Check(Path.GetFullPath(directory).StartsWith(Path.GetFullPath(root)+Path.DirectorySeparatorChar),"unsafe_fixture_path");Directory.Move(directory,to);Directory.Move(to,directory);}
 static int Main(string[] args){try{
  string root=Path.GetFullPath(args[0]);Directory.CreateDirectory(root);
  string dir=SetRoot(root,"success",true),file=Output(dir);
  PptLifecycleFile.WriteOnce("schema=1\n0 10 8 0 1\n",dir);Check(File.Exists(file),"native_success_no_output");byte[] first=File.ReadAllBytes(file);
  PptLifecycleFile.WriteOnce("different",dir);Check(Convert.ToBase64String(first)==Convert.ToBase64String(File.ReadAllBytes(file)),"existing_changed");Released(dir,root);Console.WriteLine("PASS native_success existing_unchanged handles_released");
  dir=SetRoot(root,"missing",false);PptLifecycleFile.WriteOnce("schema=1",dir);Check(!Directory.Exists(dir),"missing_created");Console.WriteLine("PASS missing_directory");
  dir=SetRoot(root,"sharing",true);
  using(var held=OpenDirectory(dir,0x40000000,7,IntPtr.Zero,3,0x02000000,IntPtr.Zero)){
   Check(!held.IsInvalid,"sharing_fixture_unavailable");PptLifecycleFile.WriteOnce("schema=1",dir);if(File.Exists(Output(dir))){Console.WriteLine("FAIL sharing_conflict_wrote");failures++;}
  }
  Released(dir,root);Console.WriteLine("OBSERVED sharing_handle_released");
  if(args.Length>1){dir=Path.Combine(args[1],"AegisDesk","ppt-lifecycle");PptLifecycleFile.WriteOnce("schema=1",dir);Check(!File.Exists(Output(Path.Combine(args[2],"AegisDesk","ppt-lifecycle"))),"reparse_followed");Console.WriteLine("PASS junction_rejected");}
  return failures==0?0:1;
 }catch(Exception e){Console.WriteLine("FAIL "+e.GetType().Name+" "+e.Message);return 1;}}
}
