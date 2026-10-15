using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using Microsoft.Win32.SafeHandles;
using AegisDesk.PptSetup;

internal static class NativeAclHarness
{
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateFile(string path, uint access, uint share,
        IntPtr security, uint disposition, uint flags, IntPtr template);

    private static void Require(bool value, string code)
    { if (!value) throw new InvalidOperationException(code); }

    private static string Security(string path)
    { return Directory.GetAccessControl(path).GetSecurityDescriptorSddlForm(AccessControlSections.Owner | AccessControlSections.Access); }

    private static void Denied(Action action, string code)
    {
        try { action(); }
        catch (IOException) { return; }
        catch (UnauthorizedAccessException) { return; }
        throw new InvalidOperationException(code);
    }

    private static FileSecurity RequestSecurity()
    {
        var acl = new FileSecurity();
        acl.SetAccessRuleProtection(true, false);
        var current = WindowsIdentity.GetCurrent().User;
        acl.SetOwner(current);
        foreach (SecurityIdentifier sid in new[] { current,
            new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null),
            new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null) })
            acl.AddAccessRule(new FileSystemAccessRule(sid, FileSystemRights.FullControl, AccessControlType.Allow));
        acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(WellKnownSidType.BuiltinUsersSid, null),
            FileSystemRights.Read, AccessControlType.Allow));
        return acl;
    }

    private static void Cleanup(string anchor)
    {
        if (!Directory.Exists(anchor)) return;
        // Only restore deletion access inside this newly created, test-owned fixture.
        foreach (string path in Directory.GetDirectories(anchor, "*", SearchOption.AllDirectories))
        {
            DirectorySecurity acl = Directory.GetAccessControl(path);
            acl.AddAccessRule(new FileSystemAccessRule(WindowsIdentity.GetCurrent().User,
                FileSystemRights.FullControl, AccessControlType.Allow));
            Directory.SetAccessControl(path, acl);
        }
        Directory.Delete(anchor, true);
    }

    public static int Main(string[] args)
    {
        using (WindowsIdentity identity = WindowsIdentity.GetCurrent())
        {
            if (new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator))
            {
                Console.WriteLine("SKIP native fixture requires an ordinary non-elevated user");
                return 77;
            }
        }
        string anchor = Path.GetFullPath(args[0]);
        if (new DriveInfo(Path.GetPathRoot(anchor)).DriveFormat != "NTFS")
        {
            Console.WriteLine("SKIP native fixture requires local NTFS");
            return 77;
        }
        Require(!Directory.Exists(anchor), "fixture_must_be_new");
        Directory.CreateDirectory(anchor);
        string parent = Path.Combine(anchor, "AegisDesk");
        string child = Path.Combine(parent, "PptReaderRequests");
        try
        {
            File.WriteAllText(parent, "fixture-parent-file");
            Denied(delegate { using (RequestDirectoryLease lease = RequestDirectoryLease.AcquireAt(anchor)) { } }, "parent_file_accepted");
            Require(File.ReadAllText(parent) == "fixture-parent-file", "rejected_parent_file_changed");
            File.Delete(parent);
            Directory.CreateDirectory(parent);
            File.WriteAllText(child, "fixture-child-file");
            string rejectedParentSecurity = Security(parent);
            Denied(delegate { using (RequestDirectoryLease lease = RequestDirectoryLease.AcquireAt(anchor)) { } }, "child_file_accepted");
            Require(File.ReadAllText(child) == "fixture-child-file" && Security(parent) == rejectedParentSecurity, "rejected_child_file_changed");
            File.Delete(child);
            string external = Path.Combine(anchor, "junction-target"); Directory.CreateDirectory(external);
            File.WriteAllText(Path.Combine(external, "sentinel"), "fixture-target");
            var junction = new ProcessStartInfo(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "cmd.exe"),
                "/d /c mklink /J \"" + child + "\" \"" + external + "\"")
                { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true };
            using (Process process = Process.Start(junction))
            { process.StandardOutput.ReadToEnd(); process.StandardError.ReadToEnd(); process.WaitForExit(); Require(process.ExitCode == 0, "junction_fixture_unavailable"); }
            Denied(delegate { using (RequestDirectoryLease lease = RequestDirectoryLease.AcquireAt(anchor)) { } }, "child_junction_accepted");
            Require(File.ReadAllText(Path.Combine(external, "sentinel")) == "fixture-target" && Security(parent) == rejectedParentSecurity, "rejected_junction_changed");
            Directory.Delete(child); Directory.Delete(external, true); Directory.Delete(parent);
            using (RequestDirectoryLease fresh = RequestDirectoryLease.AcquireAt(anchor))
            {
                Require(Directory.GetAccessControl(parent).AreAccessRulesProtected, "fresh_parent_not_protected");
                Require(Directory.GetAccessControl(child).AreAccessRulesProtected, "fresh_child_not_protected");
                fresh.Recheck();
            }
            Cleanup(anchor);
            Directory.CreateDirectory(anchor);
            // Reproduce the old install's inherited parent, then atomically create the strict child.
            Directory.CreateDirectory(parent);
            Require(!Directory.GetAccessControl(parent).AreAccessRulesProtected, "legacy_parent_not_inherited");
            Require(RequestDirectory.Create(child, RequestDirectory.Expected(false)), "child_atomic_create");
            Require(Directory.GetAccessControl(child).AreAccessRulesProtected, "child_not_protected");
            Require(RequestDirectory.Compatible(Directory.GetAccessControl(parent), true), "legacy_parent_policy");
            Require(RequestDirectory.Compatible(Directory.GetAccessControl(child), false), "strict_child_policy");
            string parentBefore = Security(parent), childBefore = Security(child);
            using (RequestDirectoryLease first = RequestDirectoryLease.AcquireAt(anchor))
            using (RequestDirectoryLease second = RequestDirectoryLease.AcquireAt(anchor))
            {
                first.Recheck(); second.Recheck();
                Denied(delegate { Directory.Move(anchor, anchor + ".renamed"); }, "anchor_rename_allowed");
                Denied(delegate { Directory.Move(parent, parent + ".renamed"); }, "parent_rename_allowed");
                Denied(delegate { Directory.Move(child, child + ".renamed"); }, "child_rename_allowed");
                Denied(delegate { Directory.Delete(child); }, "empty_child_delete_allowed");
                string request = Path.Combine(child, Guid.NewGuid().ToString("D") + ".json");
                FileSecurity acl = RequestSecurity();
                Contract.AtomicWrite(request, new Dictionary<string, object> { { "fixture", 1 } }, acl);
                Contract.AtomicWrite(request, new Dictionary<string, object> { { "fixture", 2 } }, acl);
                Require(Convert.ToInt32(Contract.Read(request)["fixture"]) == 2, "atomic_replace_failed");
                Require(Security(parent) == parentBefore && Security(child) == childBefore, "directory_security_changed");
                first.Recheck(); second.Recheck();
                File.Delete(request);
            }
            // A prior handle requesting DELETE must conflict with the later read-only-sharing lease.
            using (SafeFileHandle conflict = CreateFile(parent, 0x00010000, 7, IntPtr.Zero, 3,
                0x02000000 | 0x00200000, IntPtr.Zero))
            {
                Require(!conflict.IsInvalid, "conflict_fixture_open_failed");
                Denied(delegate { using (RequestDirectoryLease lease = RequestDirectoryLease.AcquireAt(anchor)) { } },
                    "delete_handle_conflict_accepted");
            }
            using (RequestDirectoryLease lease = RequestDirectoryLease.AcquireAt(anchor)) { lease.Recheck(); }
            // Confirm the earlier rename denial was caused by the live leases rather than absent caller rights.
            Directory.Move(child, child + ".renamed");
            Directory.Move(child + ".renamed", child);
            Directory.Move(parent, parent + ".renamed");
            Directory.Move(parent + ".renamed", parent);
            Require(Security(parent) == parentBefore && Security(child) == childBefore, "security_changed_after_conflict");
            Console.WriteLine("PASS native inherited parent, strict child, shared leases, namespace denial and atomic writes");
            Console.WriteLine("UNEXECUTED actual ProgramData, UAC, Office and distinct A-owner/B-request-owner/C-nonowner accounts");
            return 0;
        }
        finally
        {
            // Test-owned roots only. Production ProgramData is never passed to this harness.
            Cleanup(anchor);
        }
    }
}
