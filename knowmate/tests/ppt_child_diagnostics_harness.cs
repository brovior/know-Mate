using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Threading;
using AegisDesk.PptSetup;

internal static class ChildFixture
{
    internal static string Mode;
    internal static bool ImpersonationDenied;
    internal static int Completions;
    internal static string Install {get{return Path.Combine(FixturePaths.Root,Mode,"install");}}
    internal static string Data {get{return Path.Combine(FixturePaths.Root,Mode,"data");}}
    internal static int Bitness(){if(Mode=="machine_check")throw new NativeFailure("secret body",unchecked((int)0x80070020));return 64;}
    private sealed class NativeFailure:IOException {internal NativeFailure(string message,int code):base(message){HResult=code;}}
    private static void Require(bool value,string reason){if(!value)throw new Exception(reason);}
    private static DirectorySecurity DirectoryAcl(bool denyCreate)
    {
        var acl=new DirectorySecurity();acl.SetAccessRuleProtection(true,false);var sid=WindowsIdentity.GetCurrent().User;acl.SetOwner(sid);
        if(denyCreate)acl.AddAccessRule(new FileSystemAccessRule(sid,FileSystemRights.CreateDirectories,AccessControlType.Deny));
        foreach(var principal in new[]{sid,new SecurityIdentifier("S-1-5-32-544"),new SecurityIdentifier("S-1-5-18")})acl.AddAccessRule(new FileSystemAccessRule(principal,FileSystemRights.FullControl,InheritanceFlags.ContainerInherit|InheritanceFlags.ObjectInherit,PropagationFlags.None,AccessControlType.Allow));
        return acl;
    }
    private static int Action()
    {
        if(Mode=="impersonation"||Mode=="first_instance"||Mode=="peer_mismatch"||Mode=="success"||Mode=="never_connect"||Mode=="server_mismatch"||Mode=="wrong_code"||Mode=="wrong_nonce"||Mode=="trailing"||Mode=="partial"||Mode=="oversize")return 0;
        if(Mode=="handled")
        {
            ChildDiagnostics.Mark("mutation_ownership_check","record");ChildDiagnostics.Pending(new IOException("must not leak"));
            ChildDiagnostics.Mark("child_complete","none");return 0;
        }
        if(Mode=="uac_impersonation")
        {using(var identity=WindowsIdentity.GetCurrent())Require(new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator),"actual_admin_child_required");return 0;}
        if(Mode=="uac_root_write")
        {
            using(var identity=WindowsIdentity.GetCurrent())Require(new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator),"actual_admin_child_required");
            Directory.CreateDirectory(Machine.Root);var acl=DirectoryAcl(false);acl.SetOwner(new SecurityIdentifier("S-1-5-32-544"));
            acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier("S-1-5-32-545"),FileSystemRights.Write,InheritanceFlags.ContainerInherit,PropagationFlags.None,AccessControlType.Allow));
            Directory.SetAccessControl(Path.GetDirectoryName(Machine.Root),acl);
            try{Mutation.Protect(Machine.Root);}catch(IOException error){ChildDiagnostics.Capture(error);return Program.FailureCode(error);}
            throw new Exception("root_write_expected_failure");
        }
        if(Mode=="root_owner")
        {
            Directory.CreateDirectory(Machine.Root);Directory.SetAccessControl(Path.GetDirectoryName(Machine.Root),DirectoryAcl(false));
            string before=Directory.GetAccessControl(Path.GetDirectoryName(Machine.Root)).GetSecurityDescriptorSddlForm(AccessControlSections.All);
            try{Mutation.Protect(Machine.Root);}catch(Exception error){ChildDiagnostics.Capture(error);Require(before==Directory.GetAccessControl(Path.GetDirectoryName(Machine.Root)).GetSecurityDescriptorSddlForm(AccessControlSections.All),"root_changed");return Program.FailureCode((IOException)error);}
            throw new Exception("root_expected_failure");
        }
        if(Mode=="native_denied"||Mode=="native_wrapped")
        {
            string parent=Path.Combine(FixturePaths.Root,Mode,"denied");Directory.CreateDirectory(parent);Directory.SetAccessControl(parent,DirectoryAcl(true));
            ChildDiagnostics.Mark("install_root_create","reader_root");
            try{RequestDirectory.Create(Path.Combine(parent,"new"),DirectoryAcl(false));}
            catch(Exception error){if(Mode=="native_wrapped"){Require(error.Data.Contains("aegis_child_origin"),"native_origin_attached");error=SetupFailure.From("install_root_create",error);Require(error.Data.Contains("aegis_child_origin"),"wrapped_origin_attached");}ChildDiagnostics.Capture(error);return 13;}
            finally{Directory.SetAccessControl(parent,DirectoryAcl(false));}
            throw new Exception("denied_expected_failure");
        }
        if(Mode=="machine_check")
        {
            ChildDiagnostics.Mark("mutation_machine_check","none");Result value=Machine.Check("check",true,false,"own");
            Require(value.status=="unknown"&&value.office_bitness==null,"check_gate_fixture");ChildDiagnostics.Gate(value);return 13;
        }
        if(Mode=="native_direct"||Mode=="native_repeated"||Mode=="native_refined")
        {
            ChildDiagnostics.Mark("request_commit","request");
            SetupFailure native=Mode=="native_refined"?SetupFailure.WithNative(new NativeFailure("hidden",unchecked((int)0x80070020)),"commit_verify",0,0):SetupFailure.Native("commit_return",0xC0000043,null);
            if(Mode=="native_repeated")native=SetupFailure.From("request_create",SetupFailure.From("lease_recheck",native));
            ChildDiagnostics.Capture(native);return 13;
        }
        ChildDiagnostics.Mark("child_lease_acquire","request");
        var failure=new NativeFailure("PRIVATE MESSAGE must never leave process",Mode=="hresult_one"?unchecked((int)0x80004005):unchecked((int)0x80040154));
        Exception captured=SetupFailure.From("child_lease_acquire",failure);captured=SetupFailure.From("request_create",captured);ChildDiagnostics.Capture(captured);
        if(Mode=="rollback") {ChildDiagnostics.Mark("install_rollback","journal");ChildDiagnostics.CaptureSecondary(new UnauthorizedAccessException("PRIVATE SECONDARY"));}
        return 13;
    }
    private static int Child(string[] args)
    {
        Mode=args[1];string[] cli={"/elevated-install",args[2],args[3],args[4],args[5],args[6]};
        if(Mode=="main_payload"||Mode=="main_admin")return Program.Main(cli);
        string[] normalized;var session=ChildDiagnosticSession.Begin(cli,out normalized);Require(session!=null,"session");int code=13;
        try
        {
            if(Mode=="never_connect"){session.Dispose();return 0;}
            if(Mode=="server_mismatch")cli[4]=(Int32.Parse(cli[4])+1).ToString();
            code=Action();
            if(Mode=="wrong_code"||Mode=="wrong_nonce"||Mode=="trailing"||Mode=="partial"||Mode=="oversize"||Mode=="server_mismatch")
            {
                ChildDiagnostics.Complete(code==0?0U:13U);
                var value=new ChildDiagnosticObservation{Phase="install",ExitCode=0,LastStage="child_complete",LastTarget="none"};
                if(Mode=="wrong_code"){value.ExitCode=13;value.Primary=new ChildDiagnosticError{Stage="install_root_create",Target="reader_root",ErrorType="IOException",HResult="80004005"};}
                byte[] frame=ChildDiagnosticWire.Encode(new Guid(args[2]),Mode=="wrong_nonce"?Guid.NewGuid():new Guid(args[4]),(uint)Process.GetCurrentProcess().Id,value);
                if(Mode=="trailing"){Array.Resize(ref frame,frame.Length+1);frame[frame.Length-1]=1;}
                if(Mode=="partial")frame=new byte[]{32,0,0,0};
                if(Mode=="oversize")frame=BitConverter.GetBytes(ChildDiagnosticWire.Maximum+1);
                ChildDiagnosticClient.Send(new Guid(args[4]),Int32.Parse(cli[4]),args[6],frame);session.Dispose();return code;
            }
            session.Finish(code);return code;
        }
        finally{session.Dispose();}
    }
    private static int Parent(string mode)
    {
        Mode=mode;string id=Guid.NewGuid().ToString("D");
        using(var server=ChildDiagnosticServer.Start(id,"install"))
        {
            string suffix=server.Arguments;Require(suffix.Length!=0,"server_create");
            if(mode=="first_instance") {bool rejected=false;try {using(var collision=ChildDiagnosticPipe.Create(server.Nonce)) {}}catch(Win32Exception){rejected=true;}Require(rejected,"first_instance");}
            if(mode=="stall")
            {
                using(var client=new NamedPipeClientStream(".",ChildDiagnosticPipe.Prefix+server.Nonce.ToString("N"),PipeDirection.Out,PipeOptions.Asynchronous,TokenImpersonationLevel.Anonymous))
                {client.Connect(500);var clock=Stopwatch.StartNew();using(var self=Process.GetCurrentProcess()){var stalled=server.Finish(self.Handle,0);Require(stalled.Transport=="timeout"&&clock.ElapsedMilliseconds<3000,"bounded_stall");Console.WriteLine(Contract.Json.Serialize(stalled.Values()));return 0;}}
            }
            if(mode=="cancel_accept"||mode=="cancel_read")
            {
                int before;using(var self=Process.GetCurrentProcess())before=self.HandleCount;
                var clock=Stopwatch.StartNew();int previous=before;
                for(int batch=0;batch<3;batch++)
                {
                    int target=Completions+30;
                    for(int number=0;number<30;number++)using(var observer=ChildDiagnosticServer.Start(Guid.NewGuid().ToString("D"),"install"))
                    {
                        if(mode=="cancel_read")using(var client=new NamedPipeClientStream(".",ChildDiagnosticPipe.Prefix+observer.Nonce.ToString("N"),PipeDirection.Out,PipeOptions.Asynchronous,TokenImpersonationLevel.Anonymous)){client.Connect(500);Thread.Sleep(2);observer.Dispose();}
                    }
                    var cleanup=Stopwatch.StartNew();while(Completions<target&&cleanup.ElapsedMilliseconds<1000)Thread.Sleep(5);
                    Thread.Sleep(100);GC.Collect();GC.WaitForPendingFinalizers();GC.Collect();int after,threads;
                    using(var self=Process.GetCurrentProcess()){after=self.HandleCount;threads=self.Threads.Count;}
                    Console.WriteLine("cancel_batch="+batch+" initial="+before+" previous="+previous+" handles="+after+" threads="+threads+" completions="+Completions);
                    Require(Completions>=target&&(batch==0||after-previous<10),"cancel_lifetime");previous=after;
                }
                Require(clock.ElapsedMilliseconds<8000,"cancel_deadline");
                Console.WriteLine(Contract.Json.Serialize(new ChildDiagnosticObservation{Phase="install",Transport="unavailable"}.Values()));return 0;
            }
            bool uac=mode.StartsWith("uac_",StringComparison.Ordinal);
            if(uac)using(var identity=WindowsIdentity.GetCurrent())Require(!new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator),"ordinary_parent_required");
            var info=new ProcessStartInfo(System.Reflection.Assembly.GetExecutingAssembly().Location,"child "+mode+" "+id+suffix){UseShellExecute=uac,CreateNoWindow=!uac,RedirectStandardOutput=!uac,RedirectStandardError=!uac};
            if(uac){info.Verb="runas";info.WindowStyle=ProcessWindowStyle.Hidden;}
            using(var process=Process.Start(info))
            {
                Require(process.WaitForExit(uac?30000:8000),"child_timeout");
                if(!uac){Console.Write(process.StandardOutput.ReadToEnd());Console.Write(process.StandardError.ReadToEnd());}
                ChildDiagnosticObservation observation;
                if(mode=="peer_mismatch")using(var self=Process.GetCurrentProcess())observation=server.Finish(self.Handle,unchecked((uint)process.ExitCode));
                else observation=server.Finish(process.Handle,unchecked((uint)process.ExitCode));
                string expected=mode=="peer_mismatch"?"peer_mismatch":mode=="never_connect"?"timeout":mode=="server_mismatch"||mode=="wrong_code"||mode=="wrong_nonce"||mode=="trailing"||mode=="partial"||mode=="oversize"?"malformed":"received";
                Require(observation.Transport==expected,"transport_"+mode+"_"+observation.Transport);
                if(expected=="received")
                {
                    if(mode=="native_direct"||mode=="native_repeated")Require(observation.Primary.Stage=="request_commit"&&observation.Primary.NativeStage=="commit_return"&&observation.Primary.NtStatus=="C0000043","native_direct");
                    if(mode=="native_refined")Require(observation.Primary.HResult=="80070020"&&observation.Primary.Stage=="request_commit"&&observation.Primary.NativeStage=="commit_verify"&&observation.Primary.NtStatus=="00000000","native_refined");
                    if(mode=="native_denied"||mode=="native_wrapped")Require(observation.Primary.ErrorType=="Win32Exception"&&observation.Primary.WinError==5&&observation.Primary.HResult==null,"native_origin");
                    if(mode=="root_owner")Require(observation.Primary.ErrorType=="policy"&&observation.Primary.PolicyReason=="root_owner_untrusted"&&observation.Primary.Target=="program_files_parent"&&observation.Primary.HResult==null,"root_policy");
                    if(mode=="hresult_one")Require(observation.Primary.HResult=="80004005","hresult_one");
                    if(mode=="hresult_two"||mode=="rollback")Require(observation.Primary.HResult=="80040154","hresult_two");
                    if(mode=="machine_check")Require(observation.Primary.HResult=="80070020"&&observation.Primary.Stage=="machine_office_check"&&observation.Primary.CheckStatus=="unknown"&&observation.Primary.CheckReason=="office_unknown","check_origin");
                    if(mode=="rollback")Require(observation.Secondary!=null&&observation.Secondary.Stage=="install_rollback","secondary");
                    if(mode=="uac_root_write")Require(observation.Primary.ErrorType=="policy"&&observation.Primary.PolicyReason=="root_write_untrusted"&&observation.Primary.Target=="program_files_parent"&&observation.Primary.HResult==null,"uac_root_write");
                    if(mode=="impersonation"||mode=="uac_impersonation")Require(ImpersonationDenied,"anonymous_sqos");
                    if(mode=="handled")Require(observation.Primary==null&&observation.ExitCode==0,"handled_false_positive");
                    if(mode=="main_payload")Require(observation.Primary.Stage=="child_payload_check"&&observation.ExitCode==13,"main_payload");
                    if(mode=="main_admin")Require(observation.Primary.Stage=="child_admin_check"&&observation.ExitCode==13,"main_admin");
                }
                Console.WriteLine(Contract.Json.Serialize(observation.Values()));
            }
        }
        return 0;
    }
    public static int Main(string[] args)
    {
        try{return args.Length>0&&args[0]=="child"?Child(args):Parent(args[0]);}
        catch(Exception error){Console.WriteLine("FAIL "+error.GetType().Name+" "+error.Message);return 1;}
    }
}

namespace AegisDesk.PptSetup
{
    internal sealed partial class ChildDiagnosticServer
    {
        static partial void ObserveFinished(){Interlocked.Increment(ref ChildFixture.Completions);}
        [DllImport("advapi32.dll",SetLastError=true)] private static extern bool ImpersonateNamedPipeClient(Microsoft.Win32.SafeHandles.SafePipeHandle pipe);
        [DllImport("advapi32.dll")] private static extern bool RevertToSelf();
        [DllImport("advapi32.dll",SetLastError=true)] private static extern bool OpenThreadToken(IntPtr thread,uint access,bool self,out IntPtr token);
        [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr handle);
        static partial void ObservePeer(NamedPipeServerStream stream)
        {
            if(ChildFixture.Mode!="impersonation"&&ChildFixture.Mode!="uac_impersonation")return;
            bool impersonated=ImpersonateNamedPipeClient(stream.SafePipeHandle);int code=Marshal.GetLastWin32Error();
            try {
                if(!impersonated)ChildFixture.ImpersonationDenied=code==1368;
                else {IntPtr token;bool opened=OpenThreadToken(new IntPtr(-2),8,true,out token);int tokenError=Marshal.GetLastWin32Error();ChildFixture.ImpersonationDenied=!opened&&tokenError==1347;if(opened)CloseHandle(token);}
            }finally {if(impersonated)RevertToSelf();}
            Console.WriteLine("SQOS native_code="+code.ToString());
        }
    }
}
