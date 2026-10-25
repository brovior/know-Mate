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
        catch (Exception error) { File.AppendAllText(FixturePaths.Result + ".child-error", Case + "\nHRESULT=" + error.HResult.ToString("X8") + "\n" + error + "\n"); return 13; }
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
        Reset(name); Mode = corruption == "missing" ? "after-second" : "after-first";
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

    private static void ReservationOnly(string name, bool protectedOrigin, bool afterFirst)
    {
        Reset(name);
        string parent = Path.GetDirectoryName(Requests);
        if (protectedOrigin)
        {
            var original = Directory.GetAccessControl(parent);
            Directory.SetAccessControl(parent, ParentSecurity.Transform(original, WindowsIdentity.GetCurrent().User.Value, false));
        }
        Mode = afterFirst ? "after-first" : "before-backup";
        Result result = new Result("prepare"); RequestDirectoryLease normal; Reservation next;
        Require(ParentRepair.Run(ref result, out normal, out next) == 12, "reservation_only_interrupted");
        var pending = PendingAny();
        if (afterFirst) File.Delete(RepairBackup.PathFor(pending.request_id));
        string before = ParentSecurity.Key(Directory.GetAccessControl(parent));
        string dll = Machine.InstalledDll("1.0.0.0"), hash = Contract.Hash(dll), record = Contract.Hash(Machine.Record);
        Admin = true; HelperGone = true;
        var output = new StringWriter(); TextWriter old = Console.Out; int code;
        try { Console.SetOut(output); code = Program.Main(new[] { "/recover-parent", pending.request_id }); }
        finally { Console.SetOut(old); }
        string json = output.ToString();
        var data = Contract.Json.Deserialize<Dictionary<string, object>>(json);
        File.AppendAllText(FixturePaths.Result + ".endpoint.jsonl", json);
        Require(code == 0 && (string)data["status"] == "outdated" && !(bool)data["machine_safe"] &&
            (string)data["policy_reason"] == "reservation_released_retry_required", "main_retry_endpoint");
        Require(Machine.ReservationsSafe() && before == ParentSecurity.Key(Directory.GetAccessControl(parent)) &&
            hash == Contract.Hash(dll) && record == Contract.Hash(Machine.Record), "reservation_only_no_mutation");
        bool normalRejected = false, lateRejected = false;
        try { using (var lease = RequestDirectoryLease.AcquireExisting()) { } } catch (SetupFailure) { normalRejected = true; }
        try { ParentRepair.Elevated(pending.request_id); } catch (Exception) { lateRejected = true; }
        Require(normalRejected && lateRejected, "reservation_only_not_ready_late_fence");
        Admin = false; HelperGone = false; ChildGone = false; Mode = "normal"; Writes = 0;
        result = new Result("prepare");
        Require(ParentRepair.Run(ref result, out normal, out next) == 0 && next.request_id != pending.request_id, "reservation_only_retry_new_uuid");
        normal.Dispose(); Console.WriteLine("PASS lifecycle " + name);
    }

    [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]
    private struct RmProcess { internal int pid; internal System.Runtime.InteropServices.ComTypes.FILETIME started; }
    [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential, CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
    private struct RmInfo
    {
        internal RmProcess process;
        [System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.ByValTStr, SizeConst = 256)] internal string name;
        [System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.ByValTStr, SizeConst = 64)] internal string service;
        internal uint type, status, session;
        [System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.Bool)] internal bool restartable;
    }
    [System.Runtime.InteropServices.DllImport("rstrtmgr.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
    private static extern int RmStartSession(out uint session, int flags, System.Text.StringBuilder key);
    [System.Runtime.InteropServices.DllImport("rstrtmgr.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
    private static extern int RmRegisterResources(uint session, uint count, string[] files, uint applications, IntPtr processes, uint services, IntPtr names);
    [System.Runtime.InteropServices.DllImport("rstrtmgr.dll")]
    private static extern int RmGetList(uint session, out uint needed, ref uint count, [System.Runtime.InteropServices.In, System.Runtime.InteropServices.Out] RmInfo[] processes, ref uint reasons);
    [System.Runtime.InteropServices.DllImport("rstrtmgr.dll")]
    private static extern int RmEndSession(uint session);
    private static string Lockers(string path)
    {
        Require(Path.GetFullPath(path).StartsWith(FixturePaths.Root + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase), "restartmanager_fixture_only");
        uint session; int code = RmStartSession(out session, 0, new System.Text.StringBuilder(33));
        if (code != 0) return "start:" + code;
        try
        {
            code = RmRegisterResources(session, 1, new[] { path }, 0, IntPtr.Zero, 0, IntPtr.Zero);
            if (code != 0) return "register:" + code;
            uint needed, count = 0, reasons = 0;
            code = RmGetList(session, out needed, ref count, null, ref reasons);
            if (code == 0) return "none";
            if (code != 234 || needed > 64) return "query:" + code + ":" + needed;
            count = needed; var processes = new RmInfo[count];
            code = RmGetList(session, out needed, ref count, processes, ref reasons);
            string result = "code:" + code;
            if (code == 0) for (int index = 0; index < count; index++) result += ":pid=" + processes[index].process.pid;
            return result;
        }
        finally { RmEndSession(session); }
    }

    internal static void AtomicFailure(string target, string temporary, Exception error)
    {
        string line = Case + ",HRESULT=" + error.HResult.ToString("X8") + ",targetLockers=" + Lockers(target) + ",tempLockers=" + Lockers(temporary);
        foreach (string path in new[] { target, temporary })
        {
            line += ",kind=" + (path == target ? "target" : "temp");
            foreach (uint access in new uint[] { 0x10000, 0x40000000, 0x40000, 0x80000, 0xc0010000 })
                using (var handle = OpenProbe(path, access)) line += ",open" + access.ToString("X8") + "=" + (handle.IsInvalid ? System.Runtime.InteropServices.Marshal.GetLastWin32Error() : 0);
            if (File.Exists(path))
            {
                line += ",attributes=" + ((int)File.GetAttributes(path)) + ",hash=" + Contract.Hash(path);
                bool aclValid = false;
                try { using (var stream = File.OpenRead(path)) RepairBackup.ValidateFileSecurity(ParentSecurity.Read(stream.SafeFileHandle)); aclValid = true; }
                catch (Exception) { aclValid = false; }
                line += ",exactBackupAcl=" + aclValid;
            }
            else line += ",absent";
        }
        File.AppendAllText(FixturePaths.Result + ".commit-failure", line + "\n");
    }

    private static void WriteProbe(string path, FileSecurity acl, string stage)
    {
        var bytes = System.Text.Encoding.UTF8.GetBytes("{\"stage\":\"" + stage + "\",\"synthetic\":\"" + new string('x', 1250) + "\"}");
        using (var stream = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.None)) { stream.Write(bytes, 0, bytes.Length); stream.Flush(true); }
        var fresh = new FileSecurity(); fresh.SetSecurityDescriptorBinaryForm(acl.GetSecurityDescriptorBinaryForm());
        File.SetAccessControl(path, fresh);
        var expected = new RawSecurityDescriptor(fresh.GetSecurityDescriptorBinaryForm(), 0);
        var actual = new RawSecurityDescriptor(File.GetAccessControl(path).GetSecurityDescriptorBinaryForm(), 0);
        var auto = ControlFlags.DiscretionaryAclAutoInherited | ControlFlags.DiscretionaryAclAutoInheritRequired;
        expected.SetFlags(expected.ControlFlags & ~auto); actual.SetFlags(actual.ControlFlags & ~auto);
        Require(actual.GetSddlForm(AccessControlSections.Owner | AccessControlSections.Access) ==
            expected.GetSddlForm(AccessControlSections.Owner | AccessControlSections.Access), "probe_actual_acl_applied");
    }

    private sealed class CommitFault : IOException
    { internal CommitFault(int code) { HResult = code; } }
    internal static string CommitMode;
    internal static int CommitAttempts;
    internal static void CommitHook(string stage, string source, string target)
    {
        if (CommitMode == null) { File.AppendAllText(FixturePaths.Result + ".actual-replace", Case + "," + stage + "\n"); return; }
        if (stage == "before_first")
        {
            CommitAttempts++;
            throw new CommitFault(CommitMode == "other_error" ? unchecked((int)0x80070005) : unchecked((int)0x80070497));
        }
        if (stage == "replace_retry")
        {
            CommitAttempts++;
            if (CommitMode == "permanent") throw new CommitFault(unchecked((int)0x80070497));
        }
        if (stage != "after_first_failure") return;
        if (CommitMode == "source_hash") File.AppendAllText(source, "synthetic-change");
        if (CommitMode == "target_hash") File.AppendAllText(target, "synthetic-change");
        if (CommitMode == "partial") File.WriteAllText(source + ".extra.tmp", "synthetic-partial");
        if (CommitMode == "source_identity" || CommitMode == "target_identity")
        {
            string path = CommitMode == "source_identity" ? source : target;
            string held = Path.Combine(RepairBackup.Root, "held-" + CommitMode);
            File.Move(path, held); File.Copy(held, path); var copied = new FileSecurity();
            copied.SetSecurityDescriptorBinaryForm(File.GetAccessControl(held).GetSecurityDescriptorBinaryForm()); File.SetAccessControl(path, copied);
        }
        if (CommitMode == "file_acl")
        {
            var acl = File.GetAccessControl(source); acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier("S-1-5-32-545"), FileSystemRights.Write, AccessControlType.Allow));
            File.SetAccessControl(source, acl);
        }
        if (CommitMode == "file_group")
        {
            var group = new FileSecurity(); group.SetGroup(new SecurityIdentifier("S-1-5-32-544")); File.SetAccessControl(source, group);
        }
        if (CommitMode == "root_group")
        {
            var group = new DirectorySecurity(); group.SetGroup(new SecurityIdentifier("S-1-5-32-544")); Directory.SetAccessControl(RepairBackup.Root, group);
        }
        if (CommitMode == "root_identity") Directory.Move(RepairBackup.Root, RepairBackup.Root + "-held");
    }

    private static void CommitFaultGate()
    {
        foreach (string fault in new[] { "clean", "other_error", "permanent", "source_hash", "target_hash", "source_identity", "target_identity", "file_acl", "file_group", "root_group", "root_identity", "partial" })
        {
            Reset("commit_" + fault); Admin = true; Mutation.Protect(RepairBackup.Root);
            string target = Path.Combine(RepairBackup.Root, Guid.NewGuid().ToString("D") + ".json");
            string source = target + "." + Guid.NewGuid().ToString("N") + ".tmp";
            var acl = ProtectedFile();
            acl.RemoveAccessRuleAll(new FileSystemAccessRule(new SecurityIdentifier("S-1-5-32-545"), FileSystemRights.ReadAndExecute, AccessControlType.Allow));
            acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier("S-1-5-32-545"), FileSystemRights.Read, AccessControlType.Allow));
            WriteProbe(target, acl, "before"); WriteProbe(source, acl, "after");
            using (var stream = File.OpenRead(source)) RepairBackup.ValidateFileSecurity(ParentSecurity.Read(stream.SafeFileHandle));
            using (var stream = File.OpenRead(target)) RepairBackup.ValidateFileSecurity(ParentSecurity.Read(stream.SafeFileHandle));
            string expected = Contract.Hash(source); CommitMode = fault; CommitAttempts = 0; bool failed = false;
            try { BackupCommit.Commit(source, target); } catch (Exception error) { failed = true; File.AppendAllText(FixturePaths.Result + ".fault-error", fault + ",attempts=" + CommitAttempts + ",HRESULT=" + error.HResult.ToString("X8") + "\n" + error + "\n"); }
            finally { CommitMode = null; }
            if (fault == "clean") Require(!failed && !File.Exists(source) && Contract.Hash(target) == expected && CommitAttempts == 2, "guarded_1175_retry_success");
            else
            {
                Require(failed && CommitAttempts == (fault == "permanent" ? 2 : 1), "guarded_retry_rejects_changes");
                if (fault != "root_identity") Require(File.Exists(source) && File.Exists(target), "guarded_retry_preserves_names");
            }
            File.AppendAllText(FixturePaths.Result + ".retry", fault + ",attempts=" + CommitAttempts + ",failed=" + failed + "\n");
        }
        Console.WriteLine("PASS lifecycle guarded backup commit faults=12");
    }

    private static void AtomicProbe()
    {
        Reset("atomicprobe");
        string directory = Path.Combine(Root, "atomic-probe"); Mutation.Protect(directory);
        int number = 0;
        foreach (bool legacyPin in new[] { false, true })
        foreach (bool directoryPin in new[] { false, true })
        foreach (bool usersRead in new[] { false, true })
        foreach (bool validate in new[] { false, true })
        for (int repeat = 0; repeat < 8; repeat++)
        {
            using (var installation = legacyPin ? LegacyInstallation.Acquire(false, null) : null)
            using (var pin = directoryPin ? ParentSecurity.OpenSecurity(directory, false) : null)
            {
                string target = Path.Combine(directory, Guid.NewGuid().ToString("D") + ".json"), temporary = target + "." + Guid.NewGuid().ToString("N") + ".tmp"; number++;
                var acl = ProtectedFile();
                if (!usersRead) acl.RemoveAccessRuleAll(new FileSystemAccessRule(new SecurityIdentifier("S-1-5-32-545"), FileSystemRights.ReadAndExecute, AccessControlType.Allow));
                WriteProbe(target, acl, "before");
                WriteProbe(temporary, acl, "after");
                if (validate)
                    using (var stream = new FileStream(target, FileMode.Open, FileAccess.Read, FileShare.Read))
                    { ParentSecurity.Read(stream.SafeFileHandle); ParentSecurity.Inspect(stream.SafeFileHandle, target, false); }
                string prefix = "legacy=" + legacyPin + ",pin=" + directoryPin + ",users=" + usersRead + ",validate=" + validate + ",case=" + number;
                try { File.Replace(temporary, target, null); File.AppendAllText(FixturePaths.Result + ".atomic", prefix + ",PASS\n"); }
                catch (Exception error)
                {
                    int deleteOpen;
                    using (var handle = OpenProbe(target, 0x10000)) deleteOpen = handle.IsInvalid ? System.Runtime.InteropServices.Marshal.GetLastWin32Error() : 0;
                    int writeOpen;
                    using (var handle = OpenProbe(target, 0x40000000)) writeOpen = handle.IsInvalid ? System.Runtime.InteropServices.Marshal.GetLastWin32Error() : 0;
                    File.AppendAllText(FixturePaths.Result + ".atomic", prefix + ",FAIL HRESULT=" + error.HResult.ToString("X8") +
                        ",targetDelete=" + deleteOpen + ",targetWrite=" + writeOpen + ",targetExists=" + File.Exists(target) +
                        ",tempExists=" + File.Exists(temporary) + ",targetBytes=" + (File.Exists(target) ? File.ReadAllText(target) : "absent") + "\n");
                }
            }
        }
    }

    [System.Runtime.InteropServices.DllImport("kernel32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode, SetLastError = true)]
    private static extern Microsoft.Win32.SafeHandles.SafeFileHandle CreateFile(string path, uint access, uint share, IntPtr sa, uint disposition, uint flags, IntPtr template);
    private static Microsoft.Win32.SafeHandles.SafeFileHandle OpenProbe(string path, uint access)
    { return CreateFile(path, access, 7, IntPtr.Zero, 3, 0x00200000, IntPtr.Zero); }

    private static void RawLeaseChanges()
    {
        foreach (bool groupOnly in new[] { false, true })
        {
            Reset(groupOnly ? "groupchange" : "rawchange");
            string parent = Path.GetDirectoryName(Requests);
            using (var lease = RepairPreparationLease.Acquire(WindowsIdentity.GetCurrent().User.Value))
            {
                var before = Directory.GetAccessControl(parent);
                if (groupOnly)
                {
                    var group = new DirectorySecurity(); group.SetGroup(new SecurityIdentifier("S-1-5-32-544"));
                    Directory.SetAccessControl(parent, group);
                }
                else
                {
                    var raw = new RawSecurityDescriptor(before.GetSecurityDescriptorBinaryForm(), 0);
                    // 현재 helper에게 이미 있는 권한의 안전한 중복 표현도 raw 변경이다.
                    raw.DiscretionaryAcl.InsertAce(0, new CommonAce(AceFlags.None, AceQualifier.AccessAllowed, 0x20000,
                        WindowsIdentity.GetCurrent().User, false, null));
                    var bytes = new byte[raw.BinaryLength]; raw.GetBinaryForm(bytes, 0);
                    var changed = new DirectorySecurity(); changed.SetSecurityDescriptorBinaryForm(bytes); Directory.SetAccessControl(parent, changed);
                }
                Require(ParentSecurity.Legacy(Directory.GetAccessControl(parent), WindowsIdentity.GetCurrent().User.Value), "changed_semantically_repairable");
                bool denied = false;
                try { lease.Recheck(); } catch (SetupFailure error) { denied = error.PolicyReason == "namespace_changed"; }
                Require(denied, "raw_or_group_change_fenced");
            }
        }
        Console.WriteLine("PASS lifecycle raw and group changes fenced");
    }

    private static void MainErrors()
    {
        foreach (string fault in new[] { "nonadmin", "uuid", "version", "badbackup", "liveness", "unresolved" })
        {
            Reset("error_" + fault); Mode = "before-backup";
            Result result = new Result("prepare"); RequestDirectoryLease normal; Reservation next;
            Require(ParentRepair.Run(ref result, out normal, out next) == 12, "main_error_pending");
            var pending = PendingAny(); Admin = fault != "nonadmin"; HelperGone = fault != "liveness"; ChildGone = false;
            using (var lease = RepairPreparationLease.Acquire(WindowsIdentity.GetCurrent().User.Value))
            {
                pending = Reservation.Load(pending.request_id, lease);
                if (fault == "version" || fault == "unresolved")
                {
                    var changed = Contract.Read(pending.Path); changed[fault == "version" ? "required_version" : "state"] = fault == "version" ? "1.0.5.11" : "unresolved";
                    File.WriteAllText(pending.Path, Contract.Json.Serialize(changed));
                }
                if (fault == "badbackup")
                {
                    Mode = "normal";
                    using (var parent = ParentSecurity.OpenSecurity(Path.GetDirectoryName(Requests), false))
                        RepairBackup.Create(pending, ParentSecurity.Inspect(parent, Path.GetDirectoryName(Requests), true), ParentSecurity.Read(parent));
                    File.WriteAllText(RepairBackup.PathFor(pending.request_id), "{}");
                }
            }
            var output = new StringWriter(); TextWriter old = Console.Out; int code;
            try { Console.SetOut(output); code = Program.Main(new[] { "/recover-parent", fault == "uuid" ? "invalid" : pending.request_id }); }
            finally { Console.SetOut(old); }
            string json = output.ToString();
            var data = Contract.Json.Deserialize<Dictionary<string, object>>(json);
            Require(code != 0 && (string)data["status"] != "ready" && !(bool)data["machine_safe"], "main_error_fixed_json");
            File.AppendAllText(FixturePaths.Result + ".errors.jsonl", code.ToString() + " " + json);
            Require(!Machine.ReservationsSafe(), "main_error_keeps_reservation");
        }
        Console.WriteLine("PASS lifecycle main errors=6");
    }

    private static void PortabilityMatrix()
    {
        int count = 0;
        foreach (int mask in new[] { 0x116, 0x100116, 2, 0x40000000, unchecked((int)0xc0000000), 0x60000000 })
        foreach (bool protectedOrigin in new[] { false, true })
        foreach (bool split in new[] { false, true })
        foreach (int outer in new[] { 0, 1, 2, 3 })
        {
            Reset("matrix" + count++);
            string parent = Path.GetDirectoryName(Requests), sid = WindowsIdentity.GetCurrent().User.Value;
            string childSecurity = Directory.GetAccessControl(Requests).GetSecurityDescriptorSddlForm(AccessControlSections.Owner | AccessControlSections.Group);
            var anchorAcl = ParentNativeHarness.WriteVariant(Directory.GetAccessControl(Data), mask, split);
            Directory.SetAccessControl(Data, anchorAcl);
            var before = Directory.GetAccessControl(parent);
            if (protectedOrigin)
                using (var handle = ParentSecurity.OpenSecurity(parent, true)) ParentSecurity.Write(handle, ParentSecurity.Transform(before, sid, false));
            before = Directory.GetAccessControl(parent);
            var input = ParentSecurity.Analyze(before, sid);
            string role = "S-1-5-32-545:" + ((int)AceFlags.ContainerInherit).ToString();
            uint currentUsers = 0, descendantUsers = 0;
            var actualInput = new RawSecurityDescriptor(before.GetSecurityDescriptorBinaryForm(), 0);
            foreach (GenericAce entry in actualInput.DiscretionaryAcl)
            {
                var ace = (CommonAce)entry;
                if (ace.SecurityIdentifier.Value != "S-1-5-32-545") continue;
                if ((ace.AceFlags & AceFlags.InheritOnly) == 0) currentUsers |= ParentSecurity.Expand(ace.AccessMask);
                if ((ace.AceFlags & AceFlags.ContainerInherit) != 0) descendantUsers |= ParentSecurity.Expand(ace.AccessMask);
            }
            uint expectedUsers = ParentSecurity.ReadExecute | ParentSecurity.Expand(mask) | (split ? 0x100000U : 0U);
            Require(input.Kind == ParentSecurity.Classification.Repairable && currentUsers == expectedUsers && descendantUsers == expectedUsers,
                "actual_parent_mask_not_reinherited");
            Require(before.AreAccessRulesProtected == protectedOrigin && RequestDirectory.Compatible(Directory.GetAccessControl(Requests), false) &&
                childSecurity == Directory.GetAccessControl(Requests).GetSecurityDescriptorSddlForm(AccessControlSections.Owner | AccessControlSections.Group), "actual_origin_child_preserved");
            string container = Path.Combine(Install, "AegisDesk");
            var raw = new RawSecurityDescriptor(Directory.GetAccessControl(Install).GetSecurityDescriptorBinaryForm(), 0);
            foreach (GenericAce entry in raw.DiscretionaryAcl)
            {
                var ace = (CommonAce)entry;
                if (ace.SecurityIdentifier.Value != "S-1-3-0" && !ace.SecurityIdentifier.Value.StartsWith("S-1-5-80-", StringComparison.Ordinal)) continue;
                uint expanded = ParentSecurity.Expand(ace.AccessMask);
                if (outer == 1) ace.AccessMask = unchecked((int)(expanded & ~0x100000U));
                else if (outer == 2 && expanded == ParentSecurity.Full) ace.AccessMask = 0x10000000;
                else if (outer == 3 && expanded == ParentSecurity.Full) ace.AccessMask = unchecked((int)0xf0000000);
            }
            var bytes = new byte[raw.BinaryLength]; raw.GetBinaryForm(bytes, 0);
            var acl = new DirectorySecurity(); acl.SetSecurityDescriptorBinaryForm(bytes, AccessControlSections.Access); Directory.SetAccessControl(Install, acl);
            var actualOuter = new RawSecurityDescriptor(Directory.GetAccessControl(container).GetSecurityDescriptorBinaryForm(), 0);
            foreach (GenericAce entry in actualOuter.DiscretionaryAcl)
            {
                var ace = (CommonAce)entry;
                if (ace.SecurityIdentifier.Value != "S-1-3-0" && !ace.SecurityIdentifier.Value.StartsWith("S-1-5-80-", StringComparison.Ordinal)) continue;
                uint expected = outer == 1 ? ParentSecurity.Full & ~0x100000U : ParentSecurity.Full;
                Require(ParentSecurity.Expand(ace.AccessMask) == expected, "actual_outer_semantic_mask");
            }
            Result result = new Result("prepare"); RequestDirectoryLease normal; Reservation next;
            Require(ParentRepair.Run(ref result, out normal, out next) == 0, "matrix_handoff:" + count + ":" + result.stage + ":" + result.policy_reason);
            Require(normal != null && next.state == "reserved", "matrix_install_reserved"); normal.Dispose();
        }
        Console.WriteLine("PASS lifecycle portability matrix=" + count);
    }

    public static int Main(string[] args)
    {
        try
        {
            using (var token = WindowsIdentity.GetCurrent())
                Require(new WindowsPrincipal(token).IsInRole(WindowsBuiltInRole.Administrator), "actual_elevated_fixture_required");
            Require(!Directory.Exists(FixturePaths.Root) && Contract.PayloadValid(), "fixture_payload_and_root");
            CommitFaultGate();
            RawLeaseChanges();
            MainErrors();
            PortabilityMatrix();
            ReservationOnly("raw-release", false, false);
            ReservationOnly("protected-release", true, false);
            ReservationOnly("intermediate-release", false, true);
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

namespace AegisDesk.PptSetup
{
    internal static partial class BackupCommit
    {
        static partial void TraceBackupCommit(string stage, string source, string target)
        { RepairLifecycleFixture.CommitHook(stage, source, target); }
    }
}
