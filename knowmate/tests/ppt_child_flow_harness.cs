using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using AegisDesk.PptSetup;
using Microsoft.Win32;

internal static class DiagnosticFlow
{
    internal static string Mode;
    internal static bool Worker, SuppressDiagnostics, RollingBack;
    private static int officeCalls, registryCalls;
    private static readonly List<Dictionary<string,object>> results=new List<Dictionary<string,object>>();
    private static readonly string[] cases={"root_owner","root_write","lease_error","load_error","enter_error","version_error","recovery_error","check_error","office_busy","journal_error","copy_error","registry_error","record_error","verify_error","rollback_secondary","rollback_1175","post_check_error","repair_error","repair_install_error"};
    [DllImport("kernel32.dll")] private static extern IntPtr GetCurrentProcess();
    [DllImport("kernel32.dll",SetLastError=true)] private static extern bool DuplicateHandle(IntPtr source,IntPtr handle,IntPtr target,out IntPtr result,uint access,bool inherit,uint flags);
    private static void Need(bool value,string reason){if(!value)throw new Exception(reason);}
    private sealed class FlowIOException:IOException {internal FlowIOException(int code):base("synthetic_flow_failure"){HResult=code;}}
    internal static int OfficeBitness()
    {
        officeCalls++;
        if(Worker&&Mode=="check_error")throw new Win32Exception(5);
        if(!Worker&&Mode=="post_check_error"&&officeCalls>=3)throw new UnauthorizedAccessException("synthetic_postcheck_failure");
        return 64;
    }
    internal static bool ProcessGone(int pid,string ticks)
    {
        try {using(var process=Process.GetProcessById(pid))return process.StartTime.ToUniversalTime().Ticks.ToString()==ticks&&process.HasExited;}
        catch(ArgumentException){return true;}catch{return false;}
    }
    internal static bool ShellExecute(ref Elevation.ExecuteInfo info)
    {
        var start=new ProcessStartInfo(info.file,"worker "+Mode+" "+RepairLifecycleFixture.Case+" "+(SuppressDiagnostics?"off":"on")+" "+info.parameters){UseShellExecute=false,CreateNoWindow=true};
        using(var process=Process.Start(start))
        {
            IntPtr retained;
            if(!DuplicateHandle(GetCurrentProcess(),process.Handle,GetCurrentProcess(),out retained,0,false,2))throw new Win32Exception(Marshal.GetLastWin32Error());
            info.process=retained;return true;
        }
    }
    internal static RequestDirectoryLease AcquireExisting()
    {if(Mode=="lease_error")throw new UnauthorizedAccessException("synthetic_lease_error");return RequestDirectoryLease.AcquireExisting();}
    internal static Reservation Load(string id,RequestDirectoryLease lease)
    {if(Mode=="load_error")throw new Win32Exception(32);return Reservation.Load(id,lease);}
    internal static void Enter(Reservation request)
    {if(Mode=="enter_error")throw new FlowIOException(unchecked((int)0x80040154));request.EnterChild();}
    private static void UserOwnedOuter()
    {
        var acl=new DirectorySecurity();acl.SetAccessRuleProtection(true,false);var user=WindowsIdentity.GetCurrent().User;acl.SetOwner(user);
        foreach(var sid in new[]{user,new SecurityIdentifier("S-1-5-32-544"),new SecurityIdentifier("S-1-5-18")})acl.AddAccessRule(new FileSystemAccessRule(sid,FileSystemRights.FullControl,InheritanceFlags.ContainerInherit|InheritanceFlags.ObjectInherit,PropagationFlags.None,AccessControlType.Allow));
        acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier("S-1-5-32-545"),FileSystemRights.ReadAndExecute,InheritanceFlags.ContainerInherit|InheritanceFlags.ObjectInherit,PropagationFlags.None,AccessControlType.Allow));
        Directory.SetAccessControl(Path.GetDirectoryName(Machine.Root),acl);
    }
    internal static void BeforeMutation()
    {
        if(Mode=="repair_install_error")UserOwnedOuter();
        if(Mode=="version_error")Contract.AtomicWrite(Machine.Record,new Dictionary<string,object>{{"schema",99}},RepairLifecycleFixture.ProtectedFile());
        if(Mode=="recovery_error")Contract.AtomicWrite(Machine.Journal,new Dictionary<string,object>{{"schema",99}},RepairLifecycleFixture.ProtectedFile());
    }
    internal static void MutationHook(string stage)
    {
        if(!Worker)return;
        if(stage=="install_rollback"){RollingBack=true;return;}
        if(stage=="install_registry")registryCalls++;
        if(Mode=="copy_error"&&stage=="install_payload_copy"||(Mode=="rollback_secondary"||Mode=="rollback_1175")&&stage=="install_payload_copy")throw new FlowIOException(unchecked((int)0x80070020));
        if(Mode=="journal_error"&&stage=="install_journal")throw new FlowIOException(unchecked((int)0x80070005));
        if(Mode=="registry_error"&&stage=="install_registry"&&registryCalls==2)throw new Win32Exception(5);
        if(Mode=="record_error"&&stage=="install_record")throw new UnauthorizedAccessException("synthetic_record_error");
        if(Mode=="verify_error"&&stage=="install_verify")throw new FlowIOException(unchecked((int)0x80004005));
    }
    internal static void AtomicHook(string stage)
    {
        if(!Worker)return;
        if(Mode=="rollback_1175"&&RollingBack&&stage=="atomic_replace_call")throw new FlowIOException(unchecked((int)0x80070497));
        if(stage!="atomic_acl_apply")return;
        if(Mode=="repair_error")throw new FlowIOException(unchecked((int)0x80070005));
        if(Mode=="rollback_secondary"&&RollingBack)throw new UnauthorizedAccessException("synthetic_rollback_error");
    }
    private static void Setup(string mode,bool diagnostics)
    {
        Mode=mode;Worker=false;SuppressDiagnostics=!diagnostics;RollingBack=false;officeCalls=registryCalls=0;
        RepairLifecycleFixture.Reset("flow_"+mode+(diagnostics?"_on":"_off"));RepairLifecycleFixture.Mode=mode;
        bool repair=mode=="repair_error"||mode=="repair_install_error";
        if(!repair)
        {
            var parent=RequestDirectory.Expected(true);
            Directory.SetAccessControl(Path.GetDirectoryName(Machine.Requests),parent);
        }
        string outer=Path.GetDirectoryName(Machine.Root);
        if(mode=="root_owner")UserOwnedOuter();else RepairLifecycleFixture.HistoricalProtect(outer);
        if(mode=="root_write")
        {
            var acl=Directory.GetAccessControl(outer);acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier("S-1-5-32-545"),FileSystemRights.Write,InheritanceFlags.ContainerInherit,PropagationFlags.None,AccessControlType.Allow));Directory.SetAccessControl(outer,acl);
        }
        Need(Contract.PayloadValid(),"payload_fixture");
    }
    private static Dictionary<string,object> State(Dictionary<string,object> value,int exit)
    {
        var state=new Dictionary<string,object>{{"exit",exit},{"status",value["status"]},{"reason",value["reason"]},{"machine_safe",value["machine_safe"]},{"journal",File.Exists(Machine.Journal)}};
        var reserved=new List<string>();foreach(string path in Directory.GetFiles(Machine.Requests,"*.json"))reserved.Add((string)Contract.Read(path)["state"]);reserved.Sort();state["reservations"]=String.Join(",",reserved.ToArray());
        state["new_files"]=Directory.Exists(Path.GetDirectoryName(Machine.InstalledDll(Contract.Version)))?Directory.GetFiles(Path.GetDirectoryName(Machine.InstalledDll(Contract.Version))).Length:0;
        return state;
    }
    private static Dictionary<string,object> One(string mode,bool diagnostics)
    {
        Setup(mode,diagnostics);var output=new StringWriter();TextWriter previous=Console.Out;int code;
        try {Console.SetOut(output);code=Program.Main(new[]{"/prepare"});}finally{Console.SetOut(previous);}
        string raw=output.ToString().Trim();
        File.AppendAllText(FixturePaths.Result+".parent.jsonl",Contract.Json.Serialize(new Dictionary<string,object>{{"case",mode},{"diagnostics",diagnostics},{"outer_exit",code},{"parent_json",raw}})+"\n");
        var value=Contract.Json.Deserialize<Dictionary<string,object>>(raw);
        var summary=State(value,code);var row=new Dictionary<string,object>{{"case",mode},{"diagnostics",diagnostics},{"outer_exit",code},{"result",value},{"state",summary}};
        File.AppendAllText(FixturePaths.Result+".observed.jsonl",Contract.Json.Serialize(row)+"\n");
        if(diagnostics)
        {
            Need(value.ContainsKey("child_diagnostics"),"diagnostic_missing_"+mode);
            var observations=(System.Collections.IEnumerable)value["child_diagnostics"];var children=new List<Dictionary<string,object>>();foreach(object child in observations)children.Add((Dictionary<string,object>)child);
            Need(children.Count==(mode=="repair_install_error"?2:1),"phase_count_"+mode);
            foreach(var child in children)Need((string)child["transport"]=="received","transport_"+mode+"_"+child["transport"]);
            if(mode=="post_check_error")Need(Convert.ToUInt32(children[0]["child_exit_code"])==0&&code==13,"child0_outer13");
            if(mode=="repair_error")Need((string)children[0]["phase"]=="parent_repair"&&Convert.ToUInt32(children[0]["child_exit_code"])==13&&code==12,"repair13_outer12");
            if(mode=="repair_install_error")Need((string)children[0]["phase"]=="parent_repair"&&Convert.ToUInt32(children[0]["child_exit_code"])==0&&(string)children[1]["phase"]=="install"&&Convert.ToUInt32(children[1]["child_exit_code"])==13,"two_phase");
            if(mode=="root_owner"||mode=="root_write")
            {
                var primary=(Dictionary<string,object>)children[0]["primary"];
                Need((string)primary["stage"]=="install_root_policy"&&(string)primary["target"]=="program_files_parent"&&(string)primary["policy_reason"]==(mode=="root_owner"?"root_owner_untrusted":"root_write_untrusted")&&primary["hresult"]==null,"root_policy_"+mode);
            }
            if(mode=="check_error")
            {
                var primary=(Dictionary<string,object>)children[0]["primary"];Need((string)primary["stage"]=="machine_office_check"&&Convert.ToInt32(primary["winerror"])==5&&(string)primary["check_status"]=="unknown","actual_check_origin");
            }
            if(mode=="rollback_secondary"||mode=="rollback_1175")
            {
                var primary=(Dictionary<string,object>)children[0]["primary"];var secondary=(Dictionary<string,object>)children[0]["secondary"];
                Need((string)primary["hresult"]=="80070020"&&secondary!=null&&(string)secondary["hresult"]==(mode=="rollback_1175"?"80070497":"80070005"),"rollback_primary_secondary");
                Need((string)primary["stage"]=="install_payload_copy"&&(string)primary["target"]=="payload"&&(string)secondary["target"]=="record","rollback_target_identity");
            }
        }
        results.Add(row);File.AppendAllText(FixturePaths.Result+".flow.jsonl",Contract.Json.Serialize(row)+"\n");return summary;
    }
    internal static int Run(string[] args)
    {
        try
        {
            if(args.Length>0&&args[0]=="worker")
            {
                Need((args.Length==6||args.Length==10)&&Array.IndexOf(cases,args[1])>=0&&(args[3]=="on"||args[3]=="off")&&args[2]=="flow_"+args[1]+"_"+args[3],"fixed_worker_identity");
                Need(args[4]=="/elevated-install"||args[4]=="/elevated-remove"||args[4]=="/elevated-repair-parent","fixed_worker_operation");
                Worker=true;Mode=args[1];RepairLifecycleFixture.Case=args[2];RepairLifecycleFixture.Mode=Mode;RepairLifecycleFixture.Admin=true;SuppressDiagnostics=args[3]=="off";
                var operation=new string[args.Length-4];Array.Copy(args,4,operation,0,operation.Length);return Program.Main(operation);
            }
            Need(args.Length==0||args.Length==1&&args[0]=="all"||args.Length==2&&args[0]=="case"&&Array.IndexOf(cases,args[1])>=0,"fixed_fixture_only");
            using(var identity=WindowsIdentity.GetCurrent())Need(new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator),"actual_admin_fixture_required");
            Need(!Directory.Exists(FixturePaths.Root),"fresh_fixture_required");
            string longest=Path.Combine(FixturePaths.Root,"flow_rollback_secondary_off","data","AegisDesk","PptReaderRequests",new string('0',36)+".json."+new string('0',32)+".tmp");
            Need(longest.Length<250,"fixture_path_too_long");
            string[] selected=args.Length==2?new[]{args[1]}:cases;
            foreach(string mode in selected)
            {
                var on=One(mode,true);var off=One(mode,false);Need(Contract.Json.Serialize(on)==Contract.Json.Serialize(off),"diagnostic_changed_outcome_"+mode);Console.WriteLine("PASS flow "+mode);
            }
            Registry.CurrentUser.DeleteSubKeyTree(@"Software\AegisDeskRepairFixture\"+FixturePaths.Id,false);
            File.WriteAllText(FixturePaths.Result,"PASS child diagnostics production flow cases="+selected.Length.ToString()+", observations="+results.Count.ToString()+"\n");return 0;
        }
        catch(Exception error){File.WriteAllText(FixturePaths.Result,error.ToString());return 1;}
    }
}
namespace AegisDesk.PptSetup
{
    internal static partial class Mutation {static partial void TraceMutation(string stage){DiagnosticFlow.MutationHook(stage);}}
    internal static partial class Contract {static partial void TraceAtomic(string stage){DiagnosticFlow.AtomicHook(stage);}}
}
