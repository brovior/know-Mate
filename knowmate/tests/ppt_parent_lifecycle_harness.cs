using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Threading;
using Microsoft.Win32;
using AegisDesk.PptSetup;

internal static class RepairLifecycleFixture
{
    internal static string Case, Mode;
    internal static bool Admin, ChildGone, HelperGone;
    internal static int Writes;
    internal static string Anchor { get { return Path.Combine(FixturePaths.Root, Case); } }
    internal static string Data { get { return Path.Combine(Anchor, "data"); } }
    internal static string Install { get { return Path.Combine(Anchor, "install"); } }
    internal static string Root { get { return Path.Combine(Install, "AegisDesk", "PptReader"); } }
    internal static string Requests { get { return Path.Combine(Data, "AegisDesk", "PptReaderRequests"); } }
    internal static string RegistryPath { get { return @"Software\AegisDeskRepairFixture\" + FixturePaths.Id + "\\" + Case; } }
    internal static RegistryKey Registry(bool user)
    { return Microsoft.Win32.Registry.CurrentUser.CreateSubKey(RegistryPath + (user ? @"\user" : @"\machine")); }
    internal static bool ProcessGone(int pid, string ticks)
    { return pid == Process.GetCurrentProcess().Id && ticks == Process.GetCurrentProcess().StartTime.ToUniversalTime().Ticks.ToString() && (HelperGone || ChildGone); }

    private static void Require(bool value, string stage)
    { if (!value) throw new Exception(stage); }

    internal static void Observe(string stage)
    {
        if (stage != "commit_verify") return;
        bool rejected = false;
        var other = new Thread(delegate()
        {
            try { using (var gate = new MachineLock()) { } }
            catch (IOException error) { rejected = error.Message == "mutex_busy"; }
        });
        other.Start(); Require(other.Join(3000) && rejected, "reservation_transition_mutex_required");
    }

    internal static void BeforeBackup()
    { if (Mode == "before-backup") throw new IOException("fixture_before_backup"); }
    internal static void AfterBackup()
    { if (Mode == "after-backup") throw new IOException("fixture_after_backup"); }
    internal static void AfterWrite()
    {
        Writes++;
        if (Mode == "after-first" && Writes == 1 || Mode == "after-second" && Writes == 2)
            throw new IOException("fixture_write_interrupted");
        if (Mode == "sibling" && Writes == 1)
            File.WriteAllText(Path.Combine(Data, "AegisDesk", "unexpected.txt"), "synthetic-race-marker");
    }

    internal static int Elevate(Reservation request)
    {
        Require(!Machine.ReservationsSafe(), "reserved_must_block_before_elevation");
        if (Mode == "cancel") return 11;
        if (Mode == "unknown-child") return 14;
        if (Mode == "unknown-code") return 99;
        Admin = true;
        try { ParentRepair.Elevated(request.request_id); return 0; }
        catch (Exception error) { File.AppendAllText(FixturePaths.Result + ".child-error", Case + "\n" + error + "\n"); return 13; }
        finally { Admin = false; ChildGone = true; }
    }

    private static void Set(RegistryKey root, string path, string name, object value)
    { using (var key = root.CreateSubKey(path)) key.SetValue(name, value); }

    private static FileSecurity ProtectedFile()
    {
        var acl = new FileSecurity(); acl.SetAccessRuleProtection(true, false); acl.SetOwner(new SecurityIdentifier("S-1-5-32-544"));
        foreach (string sid in new[] { "S-1-5-32-544", "S-1-5-18" })
            acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(sid), FileSystemRights.FullControl, AccessControlType.Allow));
        acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier("S-1-5-32-545"), FileSystemRights.ReadAndExecute, AccessControlType.Allow)); return acl;
    }

    private static void HistoricalProtect(string directory)
    {
        // 70fe63f/c5c638a는 이 디렉터리만 보호하고 외곽 부모는 상속 상태로 남겼다.
        Directory.CreateDirectory(directory);
        var acl = new DirectorySecurity(); acl.SetAccessRuleProtection(true, false); acl.SetOwner(new SecurityIdentifier("S-1-5-32-544"));
        foreach (string sid in new[] { "S-1-5-32-544", "S-1-5-18" })
            acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(sid), FileSystemRights.FullControl,
                InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None, AccessControlType.Allow));
        acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier("S-1-5-32-545"), FileSystemRights.ReadAndExecute,
            InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None, AccessControlType.Allow));
        Directory.SetAccessControl(directory, acl);
    }

    private static void Reset(string name)
    {
        Case = name; Mode = name; Admin = false; ChildGone = false; HelperGone = false; Writes = 0;
        Require(!Directory.Exists(Anchor), "fresh_fixture_required"); Directory.CreateDirectory(Anchor); Directory.CreateDirectory(Install);
        var platformAcl = Directory.GetAccessControl(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), AccessControlSections.Access);
        var privateAcl = new DirectorySecurity();
        privateAcl.SetSecurityDescriptorSddlForm(platformAcl.GetSecurityDescriptorSddlForm(AccessControlSections.Access), AccessControlSections.Access);
        Directory.SetAccessControl(Install, privateAcl);
        var create = typeof(ParentNativeHarness).GetMethod("Fixture", BindingFlags.Static | BindingFlags.NonPublic);
        create.Invoke(null, new object[] { Data });
        RequestDirectory.Create(Requests, RequestDirectory.Expected(false), true);
        HistoricalProtect(Root); HistoricalProtect(Path.Combine(Root, "1.0.0.0"));
        Require(!Directory.GetAccessControl(Path.Combine(Install, "AegisDesk")).AreAccessRulesProtected, "historical_outer_inherited");
        string dll = Machine.InstalledDll("1.0.0.0"); File.Copy(FixturePaths.LegacyDll, dll); File.SetAccessControl(dll, ProtectedFile());
        var record = new Dictionary<string, object> { { "schema", 1 }, { "version", "1.0.0.0" }, { "bitness", 64 },
            { "prog_id", Contract.ProgId }, { "class_id", Contract.ClassId }, { "scope", "Machine" }, { "path", dll }, { "sha256", Contract.Hash(dll) } };
        Contract.AtomicWrite(Machine.Record, record, ProtectedFile());
        using (var root = Registry(false))
        {
            string server = Contract.Keys[0] + @"\InprocServer32";
            Set(root, server, "", "mscoree.dll"); Set(root, server, "ThreadingModel", "Both");
            foreach (string key in new[] { server, server + @"\1.0.0.0" })
            {
                Set(root, key, "Class", Contract.ReaderClass); Set(root, key, "Assembly", AssemblyName.GetAssemblyName(dll).FullName);
                Set(root, key, "RuntimeVersion", "v4.0.30319");
                Set(root, key, "CodeBase", name == "unescaped" ? Mutation.RegistrationCodeBase(dll) : new Uri(dll).AbsoluteUri);
            }
            Set(root, Contract.Keys[0] + @"\ProgId", "", Contract.ProgId);
            Set(root, Contract.Keys[1] + @"\CLSID", "", Contract.ClassId); Set(root, Contract.Keys[2], "LoadBehavior", 3);
        }
    }

    private static Reservation Pending(IReservationNamespace lease)
    {
        foreach (string path in Directory.GetFiles(Requests, "*.json"))
        {
            var data = RequestWriter.Read(lease, Path.GetFileNameWithoutExtension(path));
            if ((string)data["state"] != "complete") return Reservation.Load((string)data["request_id"], lease);
        }
        throw new Exception("pending_request_missing");
    }

    private static Reservation PendingAny()
    {
        try { using (var lease = RequestDirectoryLease.AcquireExisting()) return Pending(lease); }
        catch (SetupFailure) { using (var lease = RepairPreparationLease.Acquire(WindowsIdentity.GetCurrent().User.Value)) return Pending(lease); }
    }

    private static void RecoverCase(string name, bool backup, bool final)
    {
        Reset(name);
        string parent = Path.GetDirectoryName(Requests), original = ParentSecurity.Key(Directory.GetAccessControl(parent));
        Result result = new Result("prepare"); RequestDirectoryLease normal; Reservation next;
        int code = ParentRepair.Run(ref result, out normal, out next);
        Require(code == 12 && next == null && normal == null && !Machine.ReservationsSafe(), "interrupted_pending");
        var pending = PendingAny();
        Require(RepairBackup.Absent(pending.request_id) != backup, "expected_backup_presence");
        if (final) Require(RequestDirectory.Compatible(Directory.GetAccessControl(parent), true), "interrupted_final");
        Admin = true; HelperGone = true;
        ParentRepair.Recover(pending.request_id);
        Require(Machine.ReservationsSafe(), "recovery_complete");
        string current = ParentSecurity.Key(Directory.GetAccessControl(parent));
        Require(backup ? RequestDirectory.Compatible(Directory.GetAccessControl(parent), true) : current == original, "recovery_forward_only");
        Console.WriteLine("PASS lifecycle " + name);
    }

    private static void RefusedRecovery(string name, string corruption)
    {
        Reset(name); Mode = "after-first";
        Result result = new Result("prepare"); RequestDirectoryLease normal; Reservation next;
        Require(ParentRepair.Run(ref result, out normal, out next) == 12, "corrupt_setup_pending");
        var pending = PendingAny(); string path = RepairBackup.PathFor(pending.request_id);
        if (corruption == "missing") File.Delete(path);
        if (corruption == "temporary") File.WriteAllText(path + ".fixture.tmp", "synthetic-incomplete");
        if (corruption == "content") File.WriteAllText(path, "{}");
        if (corruption == "acl")
        {
            var acl = File.GetAccessControl(path); acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier("S-1-5-32-545"), FileSystemRights.Write, AccessControlType.Allow));
            File.SetAccessControl(path, acl);
        }
        string parent = Path.GetDirectoryName(Requests), before = ParentSecurity.Key(Directory.GetAccessControl(parent));
        Admin = true; HelperGone = true; bool rejected = false;
        try { ParentRepair.Recover(pending.request_id); } catch (Exception) { rejected = true; }
        Require(rejected && !Machine.ReservationsSafe() && ParentSecurity.Key(Directory.GetAccessControl(parent)) == before, "corrupt_recovery_rejected");
        Console.WriteLine("PASS lifecycle reject_" + corruption);
    }

    public static int Main(string[] args)
    {
        try
        {
            using (var token = WindowsIdentity.GetCurrent())
                Require(new WindowsPrincipal(token).IsInRole(WindowsBuiltInRole.Administrator), "actual_elevated_fixture_required");
            Require(!Directory.Exists(FixturePaths.Root) && Contract.PayloadValid(), "fixture_payload_and_root");
            Reset("normal");
            Result result = new Result("prepare"); RequestDirectoryLease normal; Reservation next;
            Require(ParentRepair.Run(ref result, out normal, out next) == 0,
                "repair_success:" + result.reason + ":" + result.stage + ":" + result.error_type + ":" + result.winerror);
            using (normal)
            {
                Require(next != null && next.state == "reserved" && !Machine.ReservationsSafe(), "install_handoff_reserved");
                int complete = 0;
                foreach (string path in Directory.GetFiles(Requests, "*.json"))
                    if ((string)RequestWriter.Read(normal, Path.GetFileNameWithoutExtension(path))["state"] == "complete") complete++;
                Require(complete == 1, "repair_complete_before_install_reservation");
            }
            Console.WriteLine("PASS lifecycle normal_handoff");
            Reset("unescaped"); result = new Result("prepare");
            Require(ParentRepair.Run(ref result, out normal, out next) == 0, "historical_unescaped_supported"); normal.Dispose();
            Console.WriteLine("PASS lifecycle historical_unescaped_handoff");
            Reset("cancel"); result = new Result("prepare");
            Require(ParentRepair.Run(ref result, out normal, out next) == 11 && Machine.ReservationsSafe() && next == null, "cancel_without_mutation");
            Console.WriteLine("PASS lifecycle cancel");
            Reset("unknown-code"); result = new Result("prepare");
            Require(ParentRepair.Run(ref result, out normal, out next) == 12 && !Machine.ReservationsSafe() && next == null, "unknown_code_pending");
            Console.WriteLine("PASS lifecycle unknown_code_pending");
            Reset("unknown-child"); result = new Result("prepare");
            Require(ParentRepair.Run(ref result, out normal, out next) == 12 && !Machine.ReservationsSafe(), "unknown_child_pending");
            var pending = PendingAny(); Admin = true; HelperGone = true; ParentRepair.Recover(pending.request_id);
            bool lateRejected = false;
            try { ParentRepair.Elevated(pending.request_id); } catch (Exception) { lateRejected = true; }
            Require(lateRejected && Machine.ReservationsSafe(), "late_child_state_fence");
            Console.WriteLine("PASS lifecycle unknown_child_recover_late_child");
            RecoverCase("before-backup", false, false); RecoverCase("after-backup", true, false);
            RecoverCase("after-first", true, false); RecoverCase("after-second", true, true);
            RefusedRecovery("missing-backup", "missing"); RefusedRecovery("temporary-backup", "temporary");
            RefusedRecovery("corrupt-backup", "content"); RefusedRecovery("bad-backup-acl", "acl");
            Reset("sibling"); result = new Result("prepare");
            Require(ParentRepair.Run(ref result, out normal, out next) == 12 && !Machine.ReservationsSafe() && next == null &&
                File.ReadAllText(Path.Combine(Data, "AegisDesk", "unexpected.txt")) == "synthetic-race-marker", "sibling_preserved_pending");
            Console.WriteLine("PASS lifecycle sibling_preserved_pending");
            File.WriteAllText(FixturePaths.Result, "PASS lifecycle all\n"); return 0;
        }
        catch (Exception error) { File.WriteAllText(FixturePaths.Result, error.ToString()); return 1; }
    }
}

namespace AegisDesk.PptSetup
{
    internal static partial class RequestWriter
    {
        static partial void TraceRequest(string stage) { RepairLifecycleFixture.Observe(stage); }
    }
}
