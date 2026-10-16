using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using AegisDesk.PptSetup;
using Microsoft.Win32.SafeHandles;

internal static class RenameProbeHarness
{
    private static readonly Stopwatch Clock = Stopwatch.StartNew();
    private static string child, cwd, sourceIdentity, sourceSecurity, sourceText;
    private static string currentStage = "preflight", failureStage;

    internal static void Stage(string stage)
    {
        currentStage = stage;
        Console.WriteLine("PPT_RENAME_STAGE " + stage + " elapsed_ms=" + Clock.ElapsedMilliseconds);
        Console.Out.Flush();
    }

    internal static void TraceAtomic(string stage)
    {
        Stage(stage);
        if (stage != "atomic_move_call" && stage != "atomic_replace_call") return;
        string[] files = Directory.GetFiles(child, "*.tmp");
        RenameProbeNative.Require(files.Length == 1, "atomic_source_not_unique");
        Capture(files[0]);
    }

    private static string Security(string path, bool directory)
    {
        FileSystemSecurity acl = directory ? (FileSystemSecurity)Directory.GetAccessControl(path) : File.GetAccessControl(path);
        return acl.GetSecurityDescriptorSddlForm(AccessControlSections.Owner | AccessControlSections.Access);
    }

    private static void CompareSecurity(string expected, string target)
    {
        var before = new RawSecurityDescriptor(expected);
        FileSecurity actual = File.GetAccessControl(target);
        var after = new RawSecurityDescriptor(actual.GetSecurityDescriptorBinaryForm(), 0);
        var beforeSecurity = new FileSecurity(); beforeSecurity.SetSecurityDescriptorSddlForm(expected);
        byte[] first = new byte[before.DiscretionaryAcl.BinaryLength];
        byte[] second = new byte[after.DiscretionaryAcl.BinaryLength];
        before.DiscretionaryAcl.GetBinaryForm(first, 0); after.DiscretionaryAcl.GetBinaryForm(second, 0);
        bool dacl = Convert.ToBase64String(first) == Convert.ToBase64String(second);
        bool protectedBefore = (before.ControlFlags & ControlFlags.DiscretionaryAclProtected) != 0;
        Console.WriteLine("PPT_RENAME_SECURITY owner_equal=" + (before.Owner == after.Owner ? "1" : "0")
            + " dacl_equal=" + (dacl ? "1" : "0") + " protected_before=" + (protectedBefore ? "1" : "0")
            + " protected_after=" + (actual.AreAccessRulesProtected ? "1" : "0")
            + " canonical_before=" + (beforeSecurity.AreAccessRulesCanonical ? "1" : "0")
            + " canonical_after=" + (actual.AreAccessRulesCanonical ? "1" : "0")
            + " control_before=" + (int)before.ControlFlags + " control_after=" + (int)after.ControlFlags);
        Console.Out.Flush();
    }

    private static FileSecurity RequestSecurity()
    {
        var acl = new FileSecurity();
        acl.SetAccessRuleProtection(true, false);
        SecurityIdentifier user = WindowsIdentity.GetCurrent().User;
        acl.SetOwner(user);
        foreach (SecurityIdentifier sid in new[] { user,
            new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null),
            new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null) })
            acl.AddAccessRule(new FileSystemAccessRule(sid, FileSystemRights.FullControl, AccessControlType.Allow));
        acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(WellKnownSidType.BuiltinUsersSid, null),
            FileSystemRights.Read, AccessControlType.Allow));
        return acl;
    }

    private static void Capture(string source)
    {
        sourceIdentity = RenameProbeNative.Identity(source);
        sourceSecurity = Security(source, false);
        sourceText = File.ReadAllText(source);
    }

    private static void Denied(Action action)
    {
        try { action(); }
        catch (IOException) { return; }
        catch (UnauthorizedAccessException) { return; }
        throw new RenameProbeNative.ProbeFailure("namespace_mutation_allowed");
    }

    private static void LeaseProof(string anchor, string parent, List<RequestDirectoryLease> leases)
    {
        Stage("lease_recheck");
        foreach (RequestDirectoryLease lease in leases) lease.Recheck();
        Stage("directory_write_probe");
        RenameProbeNative.DirectoryWriteProbe(child, leases.Count != 0);
        if (leases.Count == 0) return;
        Stage("anchor_rename_probe");
        Denied(delegate { Directory.Move(anchor, anchor + ".renamed"); });
        Stage("parent_rename_probe");
        Denied(delegate { Directory.Move(parent, parent + ".renamed"); });
        Stage("child_rename_probe");
        Denied(delegate { Directory.Move(child, child + ".renamed"); });
    }

    private static void Run(string anchor, string mode, int count)
    {
        string originalCwd = Environment.CurrentDirectory;
        string parent = Path.Combine(anchor, "AegisDesk");
        child = Path.Combine(parent, "PptReaderRequests");
        cwd = Path.Combine(anchor, "cwd-decoy");
        Directory.CreateDirectory(parent); Directory.CreateDirectory(cwd);
        RenameProbeNative.Require(RequestDirectory.Create(child, RequestDirectory.Expected(false)), "child_create");
        string parentSecurity = Security(parent, true), childSecurity = Security(child, true);
        string request = Path.Combine(child, "request.json"), source = Path.Combine(child, "source.tmp");
        string decoy = Path.Combine(cwd, "request.json"), decoySource = Path.Combine(cwd, "source.tmp");
        File.WriteAllText(decoy, "cwd-target-fixture"); File.WriteAllText(decoySource, "cwd-source-fixture");
        string decoyIdentity = RenameProbeNative.Identity(decoy), decoySourceIdentity = RenameProbeNative.Identity(decoySource);
        string decoySecurity = Security(decoy, false), decoySourceSecurity = Security(decoySource, false);
        var leases = new List<RequestDirectoryLease>();
        try
        {
            Stage("lease_acquire");
            for (int index = 0; index < count; index++) leases.Add(RequestDirectoryLease.AcquireAt(anchor));
            // Prove deletion denial while the child is still empty.
            if (count != 0)
            {
                Stage("empty_child_delete_probe");
                Denied(delegate { Directory.Delete(child); });
            }
            Environment.CurrentDirectory = cwd;
            bool replace = mode.EndsWith("replace", StringComparison.Ordinal);
            bool collision = mode == "native-collision";
            var acl = RequestSecurity();
            if (replace || collision)
            {
                File.WriteAllText(request, "target-fixture"); File.SetAccessControl(request, acl);
            }
            string oldIdentity = File.Exists(request) ? RenameProbeNative.Identity(request) : null;
            string oldSecurity = File.Exists(request) ? Security(request, false) : null;
            if (mode.StartsWith("managed-", StringComparison.Ordinal))
            {
                Stage("managed_commit");
                Contract.AtomicWrite(request, new Dictionary<string, object> { { "fixture", 2 } }, acl);
            }
            else
            {
                Stage("source_prepare");
                using (var stream = new FileStream(source, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                {
                    byte[] fixture = System.Text.Encoding.UTF8.GetBytes("source-fixture");
                    stream.Write(fixture, 0, fixture.Length); stream.Flush(true);
                }
                File.SetAccessControl(source, acl); Capture(source);
                Stage("source_open");
                SafeFileHandle handle = RenameProbeNative.OpenSource(source);
                try
                {
                    Stage("source_opened");
                    RenameProbeNative.Require(RenameProbeNative.Identity(handle) == sourceIdentity, "source_identity_changed");
                    Stage("native_rename_call");
                    int status = RenameProbeNative.Rename(handle, "request.json", replace);
                    Stage("native_return");
                    if (collision)
                    {
                        RenameProbeNative.Require(unchecked((uint)status) == 0xC0000035, "collision_status_not_name_collision");
                        // Path reopening with FileShare.Read conflicts with our own DELETE
                        // handle. Preserve the status here; verify names only after close.
                    }
                    else RenameProbeNative.Require(status == 0 && RenameProbeNative.Identity(handle) == sourceIdentity,
                        "native_rename_failed");
                }
                catch { failureStage = currentStage; throw; }
                finally { Stage("source_close"); handle.Dispose(); Stage("source_closed"); }
                if (collision)
                {
                    Stage("collision_verify");
                    RenameProbeNative.Require(File.ReadAllText(source) == sourceText
                        && RenameProbeNative.Identity(source) == sourceIdentity && Security(source, false) == sourceSecurity,
                        "collision_source_changed");
                }
                else RenameProbeNative.Require(!File.Exists(source), "source_name_remains");
            }
            Stage("verify");
            RenameProbeNative.Require(File.ReadAllText(request) == (collision ? "target-fixture" : sourceText), "target_content_changed");
            RenameProbeNative.Require(RenameProbeNative.Identity(request) == (collision ? oldIdentity : sourceIdentity), "target_identity_changed");
            CompareSecurity(collision ? oldSecurity : sourceSecurity, request);
            RenameProbeNative.Require(Security(request, false) == (collision ? oldSecurity : sourceSecurity), "target_security_changed");
            RenameProbeNative.Require(File.ReadAllText(decoy) == "cwd-target-fixture" && File.ReadAllText(decoySource) == "cwd-source-fixture"
                && RenameProbeNative.Identity(decoy) == decoyIdentity && RenameProbeNative.Identity(decoySource) == decoySourceIdentity
                && Security(decoy, false) == decoySecurity && Security(decoySource, false) == decoySourceSecurity, "cwd_decoy_changed");
            RenameProbeNative.Require(Security(parent, true) == parentSecurity && Security(child, true) == childSecurity,
                "directory_security_changed");
            // A CWD handle below anchor could independently prevent its rename;
            // restore the external CWD before proving directory lease behavior.
            Environment.CurrentDirectory = originalCwd;
            LeaseProof(anchor, parent, leases);
        }
        catch { if (failureStage == null) failureStage = currentStage; throw; }
        finally
        {
            Stage("lease_release");
            for (int index = leases.Count - 1; index >= 0; index--) leases[index].Dispose();
        }
    }

    private static void Error(Exception error)
    {
        string kind = error is Win32Exception ? "win32" : error is IOException ? "io"
            : error is UnauthorizedAccessException ? "access" : "contract";
        uint hresult = unchecked((uint)Marshal.GetHRForException(error));
        var setup = error as SetupFailure;
        string winerror = setup != null ? (setup.WinError.HasValue ? setup.WinError.Value.ToString() : "none")
            : error is Win32Exception ? ((Win32Exception)error).NativeErrorCode.ToString()
            : (hresult & 0xFFFF0000) == 0x80070000 ? (hresult & 0xFFFF).ToString() : "none";
        if (setup != null)
            Console.WriteLine("PPT_RENAME_SETUP stage=" + setup.Stage + " error_type=" + setup.ErrorType + " winerror=" + winerror);
        var failure = error as RenameProbeNative.ProbeFailure;
        Console.WriteLine("PPT_RENAME_ERROR kind=" + kind + " code=" + (failure == null ? "exception" : failure.Code)
            + " stage=" + (failureStage ?? currentStage) + " hresult=" + hresult.ToString("X8") + " winerror=" + winerror);
        Console.Out.Flush();
    }

    public static int Main(string[] args)
    {
        Console.WriteLine("PPT_RENAME_ARCH bits=" + (IntPtr.Size * 8));
        string anchor = null, originalCwd = Environment.CurrentDirectory;
        int result = 0;
        try
        {
            Stage("preflight");
            RenameProbeNative.Require(args.Length == 3, "argument_count");
            string mode = args[1];
            RenameProbeNative.Require(mode == "managed-create" || mode == "managed-replace" || mode == "native-create"
                || mode == "native-replace" || mode == "native-collision", "mode_invalid");
            int count;
            RenameProbeNative.Require(Int32.TryParse(args[2], out count) && count >= 0 && count <= 2, "lease_count_invalid");
            using (WindowsIdentity identity = WindowsIdentity.GetCurrent())
            {
                if (new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator))
                { Console.WriteLine("PPT_RENAME_RESULT SKIP reason=elevated"); return 77; }
            }
            string candidate = Path.GetFullPath(args[0]);
            Guid fixtureId;
            string leaf = Path.GetFileName(candidate);
            RenameProbeNative.Require(leaf.StartsWith("ppt-rename-probe-", StringComparison.Ordinal)
                && Guid.TryParseExact(leaf.Substring(17), "N", out fixtureId) && !Directory.Exists(candidate)
                && !File.Exists(candidate) && !candidate.StartsWith(@"\\", StringComparison.Ordinal), "fixture_path_invalid");
            string programData = Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData).TrimEnd('\\') + "\\";
            RenameProbeNative.Require(!candidate.StartsWith(programData, StringComparison.OrdinalIgnoreCase), "programdata_fixture_forbidden");
            DriveInfo drive = new DriveInfo(Path.GetPathRoot(candidate));
            if (drive.DriveType != DriveType.Fixed || drive.DriveFormat != "NTFS")
            { Console.WriteLine("PPT_RENAME_RESULT SKIP reason=nonlocal_ntfs"); return 77; }
            Machine.NoLinks(Path.GetDirectoryName(candidate));
            Stage("fixture_create"); Directory.CreateDirectory(candidate); anchor = candidate;
            Run(anchor, mode, count);
        }
        catch (Exception error) { Error(error); result = 1; }
        finally
        {
            failureStage = null;
            try
            {
                Environment.CurrentDirectory = originalCwd;
                if (anchor != null)
                {
                    Stage("cleanup");
                    foreach (string directory in Directory.GetDirectories(anchor, "*", SearchOption.AllDirectories))
                    {
                        DirectorySecurity acl = Directory.GetAccessControl(directory);
                        acl.AddAccessRule(new FileSystemAccessRule(WindowsIdentity.GetCurrent().User,
                            FileSystemRights.FullControl, AccessControlType.Allow));
                        Directory.SetAccessControl(directory, acl);
                    }
                    Directory.Delete(anchor, true);
                }
            }
            catch (Exception error) { Error(error); result = 1; }
        }
        Stage("complete");
        Console.WriteLine("PPT_RENAME_RESULT " + (result == 0 ? "PASS" : "FAIL"));
        return result;
    }
}

namespace AegisDesk.PptSetup
{
    internal static partial class Contract
    {
        static partial void TraceAtomic(string stage) { RenameProbeHarness.TraceAtomic(stage); }
    }
}
