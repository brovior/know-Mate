using System;
using System.IO;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
using AegisDesk.PptReader;
public static class NativeRaceHook {
 public static string Root, Mode; public static int RejectedRename, RejectedReplacement, Failures, Validated, Opens, Denials;
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
 static extern bool MoveFileEx(string from,string to,uint flags);
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true,EntryPoint="CreateFileW")]
 static extern SafeFileHandle Open(string p,uint a,uint s,IntPtr sec,uint disp,uint flags,IntPtr template);
 [DllImport("kernel32.dll")] static extern void SetLastError(uint value);
 public static SafeFileHandle Create(string p,uint a,uint s,IntPtr sec,uint disp,uint flags,IntPtr template) {
  Opens++;
  if(disp==3&&a!=0x81) {Failures++;throw new Exception("attributes_only_fallback");}
  if(Mode=="accessdenied"&&p.TrimEnd('\\').EndsWith("ppt-lifecycle")) {Denials++;SetLastError(5);return new SafeFileHandle(new IntPtr(-1),false);}
  return Open(p,a,s,sec,disp,flags,template);
 }
 static void Within(string p){if(!Path.GetFullPath(p).StartsWith(Path.GetFullPath(Root)+Path.DirectorySeparatorChar))throw new Exception("fixture_escape");}
 public static void AfterValidate(string directory) {
  Validated++;
  if(Mode!="race")return;
  string middle=Path.GetDirectoryName(directory);
  foreach(string target in new[]{directory,middle}) {
   Within(target);string renamed=target+"-renamed";Within(renamed);
   bool ok=MoveFileEx(target,renamed,0);int err=Marshal.GetLastWin32Error();
   if(ok){MoveFileEx(renamed,target,0);Failures++;}else if(err==32)RejectedRename++;else Failures++;
   // Two-step namespace replacement must first detach the existing target.
   string backup=target+"-replacement-backup";Within(backup);
   bool detached=MoveFileEx(target,backup,0);err=Marshal.GetLastWin32Error();
   if(detached){MoveFileEx(backup,target,0);Failures++;}else if(err==32)RejectedReplacement++;else Failures++;
  }
 }
 public static void RestoreCheck(string target){Within(target);string other=target+"-after";Within(other);if(!MoveFileEx(target,other,0)||!MoveFileEx(other,target,0))throw new Exception("handle_not_released");}
}
class NativeRaceHarness {
 static void Check(bool yes,string code){if(!yes)throw new Exception(code);}
 static int Main(string[] args){try{
  NativeRaceHook.Root=Path.GetFullPath(args[0]);Directory.CreateDirectory(NativeRaceHook.Root);
  string app=Path.Combine(NativeRaceHook.Root,"local");string middle=Path.Combine(app,"AegisDesk");string dir=Path.Combine(middle,"ppt-lifecycle");Directory.CreateDirectory(dir);
  string file=Path.Combine(dir,"host-"+System.Diagnostics.Process.GetCurrentProcess().Id+".log");
  NativeRaceHook.Mode="race";PptLifecycleFile.WriteOnce("schema=1\n0 10 8 0 1\n",dir);
  Check(NativeRaceHook.Failures==0&&NativeRaceHook.Validated==1&&NativeRaceHook.RejectedRename==2&&NativeRaceHook.RejectedReplacement==2,"race_not_blocked");
  Check(File.Exists(file)&&File.ReadAllText(file)=="schema=1\n0 10 8 0 1\n","wrong_output");
  NativeRaceHook.RestoreCheck(dir);NativeRaceHook.RestoreCheck(middle);
  Console.WriteLine("PASS leaf_middle_rename_denied32 replacement_detach_denied32 original_output_only release_restore");
  string deny=Path.Combine(NativeRaceHook.Root,"denied");string deniedDir=Path.Combine(deny,"AegisDesk","ppt-lifecycle");Directory.CreateDirectory(deniedDir);
  NativeRaceHook.Mode="accessdenied";int before=NativeRaceHook.Validated;
  PptLifecycleFile.WriteOnce("schema=1",deniedDir);Check(NativeRaceHook.Denials==1&&NativeRaceHook.Validated==before&&Directory.GetFiles(deniedDir).Length==0,"denial_not_suppressed");
  Check(NativeRaceHook.Failures==0,"fallback");NativeRaceHook.RestoreCheck(deniedDir);
  Console.WriteLine("PASS injected_access_denied_no_fallback_no_output handles_released");return 0;
 }catch(Exception e){Console.WriteLine("FAIL "+e.GetType().Name+" "+e.Message);return 1;}}
}
