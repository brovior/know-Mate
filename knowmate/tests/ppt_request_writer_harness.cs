using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Threading;
using Microsoft.Win32.SafeHandles;
using AegisDesk.PptSetup;

// Isolated actual Reservation/RequestWriter tests. No registry, COM, or UAC.
internal static class RequestWriterHarness
{
    private static readonly Stopwatch Clock = Stopwatch.StartNew();
    internal static string Mode, Child, Target, Source, SourceId, SourceAcl;
    private static string stage = "preflight";
    internal static int CommitCount { get { return commits; } }
    private static bool armed, targetClosed;
    private static IntPtr pendingSource, pendingIo, pendingRename;
    private static int commits, nativeCalls;

    internal static void Stage(string value)
    {
        stage = value;
        Console.WriteLine("PPT_WRITER_STAGE " + value + " elapsed_ms=" + Clock.ElapsedMilliseconds);
        Console.Out.Flush();
    }
    internal static void Trace(string value)
    {
        Stage(value);
        if (value == "target_closed") targetClosed = true;
        if (value == "commit_call")
        {
            commits++;
            if (armed && Mode == "replace") Require(targetClosed, "target_not_closed");
        }
        if (value == "commit_call" || value == "source_acl" || value == "source_written")
        {
            string[] temps = Directory.GetFiles(Child, "*.tmp");
            Require(temps.Length == 1, "source_missing");
            Source = temps[0]; SourceId = RenameProbeNative.Identity(Source); SourceAcl = Security(Source);
        }
        if (armed && Mode == "native-failure-close" && value == "source_close") throw new IOException("test_close_fault");
        if (armed && Mode.StartsWith("fault-", StringComparison.Ordinal) && value == Mode.Substring(6))
            throw new IOException("test_fault");
        if (armed && ((Mode == "interrupt-before" && value == "commit_call")
            || (Mode == "interrupt-after" && value == "commit_return")))
        {
            Console.WriteLine("PPT_WRITER_INTERRUPTION point=" + (Mode == "interrupt-before" ? "before" : "after") + " ready=1");
            Console.Out.Flush(); Thread.Sleep(Timeout.Infinite);
        }
        if (armed && value == "native_unconfirmed")
        {
            var thread = new Thread(delegate()
            {
                Thread.Sleep(500);
                bool buffers = pendingIo != IntPtr.Zero && pendingRename != IntPtr.Zero
                    && Marshal.ReadInt32(pendingRename, 2 * IntPtr.Size) > 0;
                bool source = GetFileType(pendingSource) == 1;
                bool lease = false;
                using (SafeFileHandle probe = OpenFile(Child, 2, 7, IntPtr.Zero, 3, 0x02200000, IntPtr.Zero))
                    lease = probe.IsInvalid && Marshal.GetLastWin32Error() == 32;
                bool mutex = false;
                using (Mutex probe = Mutex.OpenExisting(Contract.MutexName, MutexRights.Synchronize | MutexRights.Modify))
                { bool got = probe.WaitOne(0); mutex = !got; if (got) probe.ReleaseMutex(); }
                Console.WriteLine("PPT_WRITER_HOLD verified=" + (buffers && source && lease && mutex ? "1" : "0")
                    + " buffers=" + (buffers ? "1" : "0") + " source=" + (source ? "1" : "0")
                    + " lease=" + (lease ? "1" : "0") + " mutex=" + (mutex ? "1" : "0"));
                Console.Out.Flush();
            });
            thread.IsBackground = true; thread.Start();
        }
    }
    internal static void Intercept(IntPtr source, IntPtr io, IntPtr rename, ref bool handled, ref uint status)
    {
        nativeCalls++;
        if (!armed) return;
        if (Mode.StartsWith("native-failure", StringComparison.Ordinal))
        {
            handled = true; status = 0xC0000043;
            if (Mode == "native-failure-io") Marshal.WriteIntPtr(io, new IntPtr(unchecked((int)0xC0000022)));
        }
        if (Mode == "fake-success") { handled = true; status = 0; Marshal.WriteIntPtr(io, IntPtr.Zero); }
        if (Mode == "pending" || Mode == "io-incomplete" || Mode == "io-unwritten")
        {
            handled = true; status = Mode == "pending" ? 0x103u : 0u;
            if (Mode == "io-incomplete") Marshal.WriteIntPtr(io, new IntPtr(0x103));
            pendingSource = source; pendingIo = io; pendingRename = rename;
        }
    }
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true, EntryPoint = "CreateFileW")]
    private static extern SafeFileHandle OpenFile(string path, uint access, uint share, IntPtr security,
        uint disposition, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern uint GetFileType(IntPtr handle);
    private static void Require(bool value, string code)
    { if (!value) throw new RenameProbeNative.ProbeFailure(code); }
    private static string Security(string path)
    { return File.GetAccessControl(path).GetSecurityDescriptorSddlForm(AccessControlSections.Owner | AccessControlSections.Access); }
    private static void WriteTarget(Dictionary<string, object> values)
    { File.WriteAllText(Target, Contract.Json.Serialize(values)); }
    private static void Run(string anchor, int count)
    {
        if (Mode == "hardlink" || Mode == "reparse" || Mode == "reparse-directory")
        { WriterLinkFixture.Run(anchor, count, Mode); return; }
        Directory.CreateDirectory(anchor);
        // Preserve the inherited legacy parent used by the actual reported failure.
        Directory.CreateDirectory(Path.Combine(anchor, "AegisDesk"));
        string cwd = Path.Combine(anchor, "cwd"); Directory.CreateDirectory(cwd);
        string previous = Environment.CurrentDirectory;
        using (var mutex = new MachineLock())
        using (RequestDirectoryLease first = RequestDirectoryLease.AcquireAt(anchor))
        using (RequestDirectoryLease second = count == 2 ? RequestDirectoryLease.AcquireAt(anchor) : null)
        {
            Child = first.ChildPath;
            Stage("reservation_create");
            Reservation request = Reservation.Create(first); Target = request.Path;
            Require(Path.GetDirectoryName(Target) == Child, "target_outside_fixture");
            string decoy = Path.Combine(cwd, Path.GetFileName(Target));
            File.WriteAllText(decoy, "decoy"); string decoyId = RenameProbeNative.Identity(decoy);
            Environment.CurrentDirectory = cwd;
            try
            {
                Stage("reservation_load");
                Reservation loaded = Reservation.Load(request.request_id, first);
                Require(loaded.state == "reserved", "initial_state");
                Dictionary<string, object> values = RequestWriter.Read(first, request.request_id);
                string identity = RenameProbeNative.Identity(Target);
                int beforeCommits = commits, beforeNativeCalls = nativeCalls;
                bool rejected = false, expectReject = Mode != "create" && Mode != "replace";
                if (Mode == "replace")
                {
                    targetClosed = false; armed = true;
                    loaded.state = "complete"; loaded.Save();
                    Require(Reservation.Load(request.request_id, first).state == "complete", "final_state");
                }
                else if (Mode != "create")
                {
                    if (Mode == "missing") File.Delete(Target);
                    if (Mode == "malformed") File.WriteAllText(Target, "{broken");
                    if (Mode == "identity") { string id = (string)values["request_id"]; values["request_id"] = Guid.NewGuid().ToString("D"); WriteTarget(values); values["request_id"] = id; }
                    if (Mode == "state") values["state"] = "reserved"; // reserved -> reserved is not a replacement transition
                    if (Mode == "size") values["required_version"] = "1." + new string('1', 9000) + ".0.0";
                    if (Mode == "readonly") File.SetAttributes(Target, FileAttributes.ReadOnly);
                    if (Mode == "policy")
                    {
                        FileSecurity unsafeAcl = File.GetAccessControl(Target);
                        unsafeAcl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(WellKnownSidType.BuiltinUsersSid, null),
                            FileSystemRights.Write, AccessControlType.Allow)); File.SetAccessControl(Target, unsafeAcl);
                    }
                    string expectedText = File.Exists(Target) ? File.ReadAllText(Target) : null;
                    string expectedSecurity = File.Exists(Target) ? Security(Target) : null;
                    byte[] expectedSourceBytes = null;
                    armed = true; Source = null;
                    SafeFileHandle external = null;
                    try
                    {
                        if (Mode == "external-reader")
                        {
                            external = OpenFile(Target, 0x80000000, 7, IntPtr.Zero, 3, 0x00200000, IntPtr.Zero);
                            Require(!external.IsInvalid, "external_reader_open_failed");
                        }
                        if (Mode == "collision")
                        {
                            var collisionValues = RequestWriter.Read(first, request.request_id);
                            expectedSourceBytes = System.Text.Encoding.UTF8.GetBytes(Contract.Json.Serialize(collisionValues));
                            RequestWriter.Write(first, collisionValues, RequestWriteMode.CreateOnly);
                        }
                        else
                        {
                            if (Mode != "state") values["state"] = "complete";
                            expectedSourceBytes = Mode == "fault-source_acl" || Mode == "fault-source_write" ? new byte[0]
                                : System.Text.Encoding.UTF8.GetBytes(Contract.Json.Serialize(values));
                            RequestWriter.Write(first, values, RequestWriteMode.ReplaceExisting);
                        }
                    }
                    catch (SetupFailure error)
                    {
                        rejected = true;
                        if (Mode.StartsWith("native-failure", StringComparison.Ordinal))
                            Require(error.NativeStage == "commit_return" && error.NtStatus == "C0000043" && error.WinError == null
                                && error.IoStatus == (Mode == "native-failure-io" ? "C0000022" : null), "primary_native_failure_changed");
                        if (Mode == "fake-success") Require(error.NativeStage == "commit_verify", "fake_success_accepted");
                        if (Mode == "fault-source_close") Require(error.NativeStage == "commit_close", "close_failure_not_classified");
                        if (error.NativeStage != null)
                            Console.WriteLine("PPT_WRITER_NATIVE stage=" + error.NativeStage + " ntstatus=" + error.NtStatus
                                + " io_status=" + (error.IoStatus ?? "none"));
                        Console.WriteLine("PPT_WRITER_REJECT stage=" + error.Stage + " error_type=" + error.ErrorType
                            + " winerror=" + (error.WinError.HasValue ? error.WinError.Value.ToString() : "none"));
                    }
                    catch (IOException) { rejected = true; Console.WriteLine("PPT_WRITER_REJECT stage=policy error_type=IOException winerror=none"); }
                    finally { if (external != null) external.Dispose(); }
                    Require(rejected, "expected_rejection_missing");
                    Stage("preservation_verify");
                    if (Mode == "missing") Require(!File.Exists(Target), "missing_target_recreated");
                    else if (Mode == "fault-source_close")
                    {
                        Require(Reservation.Load(request.request_id, first).state == "complete", "committed_close_fault_state");
                        Require(RenameProbeNative.Identity(Target) == SourceId, "source_identity_not_preserved");
                    }
                    else Require(File.ReadAllText(Target) == expectedText && Security(Target) == expectedSecurity,
                        "rejected_target_content_changed");
                    if (Mode != "identity" && Mode != "malformed" && Mode != "reparse" && Mode != "missing" && Mode != "hardlink" && Mode != "fault-source_close")
                        Require(RenameProbeNative.Identity(Target) == identity, "rejected_target_identity_changed");
                    // Failures before commit must never reach the syscall.
                    if (Mode == "malformed" || Mode == "identity" || Mode == "missing" || Mode == "state" || Mode == "size"
                        || Mode == "readonly" || Mode == "hardlink" || Mode == "reparse" || Mode == "policy")
                        Require(commits == beforeCommits, "invalid_target_reached_commit");
                    if (Mode.StartsWith("fault-", StringComparison.Ordinal) && Mode != "fault-source_close")
                        Require(nativeCalls == beforeNativeCalls, "fault_reached_native");
                    if (Source != null && File.Exists(Source))
                    {
                        Require(RenameProbeNative.Identity(Source) == SourceId && Security(Source) == SourceAcl
                            && expectedSourceBytes != null && Convert.ToBase64String(File.ReadAllBytes(Source)) == Convert.ToBase64String(expectedSourceBytes),
                            "rejected_source_changed");
                        using (SafeFileHandle closed = OpenFile(Source, 0x80000000, 0, IntPtr.Zero, 3, 0x00200000, IntPtr.Zero))
                            Require(!closed.IsInvalid, "source_handle_leaked");
                    }
                    if (Mode == "fault-target_close" || Mode == "fault-source_close")
                        using (SafeFileHandle closed = OpenFile(Target, 0x80000000, 0, IntPtr.Zero, 3, 0x00200000, IntPtr.Zero))
                            Require(!closed.IsInvalid, "target_handle_leaked");
                }
                Stage("verify");
                Require(File.ReadAllText(decoy) == "decoy" && RenameProbeNative.Identity(decoy) == decoyId, "cwd_decoy_changed");
                if (Mode == "create" || Mode == "replace")
                {
                    Require(SourceId == RenameProbeNative.Identity(Target), "source_identity_not_preserved");
                    Require(SourceAcl == Security(Target), "source_security_not_preserved");
                    FileSecurity actual = File.GetAccessControl(Target);
                    Require(actual.AreAccessRulesProtected && actual.AreAccessRulesCanonical, "target_security_policy");
                }
                first.Recheck(); if (second != null) second.Recheck();
                RenameProbeNative.DirectoryWriteProbe(Child, true);
                Console.WriteLine("PPT_WRITER_EXPECT expected=" + (expectReject ? "reject" : "success")
                    + " actual=" + (expectReject ? "reject" : "success") + " verified=1");
            }
            finally { armed = false; Environment.CurrentDirectory = previous; }
        }
    }
    internal static void RecordRejection(SetupFailure error)
    {
        Console.WriteLine("PPT_WRITER_REJECT stage=" + error.Stage + " error_type=" + error.ErrorType
            + " winerror=" + (error.WinError.HasValue ? error.WinError.Value.ToString() : "none"));
    }
    private static string ErrorType(Exception error)
    {
        var setup = error as SetupFailure;
        if (setup != null) return setup.ErrorType;
        if (error is WriterFixtureUnavailable || error is Win32Exception) return "Win32Exception";
        if (error is RenameProbeNative.ProbeFailure) return "contract";
        if (error is UnauthorizedAccessException) return "access";
        return error is IOException ? "IOException" : "other";
    }
    private static string Evidence(Exception error, string at)
    {
        var setup = error as SetupFailure; var native = error as Win32Exception;
        var unavailable = error as WriterFixtureUnavailable; var failure = error as RenameProbeNative.ProbeFailure;
        string code = unavailable != null ? "fixture_creation_failed" : failure == null ? "exception" : failure.Code;
        string winerror = unavailable != null ? unavailable.WinError.ToString() : native != null ? native.NativeErrorCode.ToString()
            : setup != null && setup.WinError.HasValue ? setup.WinError.Value.ToString() : "none";
        return "stage=" + at + " error_type=" + ErrorType(error) + " code=" + code + " winerror=" + winerror
            + " ntstatus=" + (setup == null ? "none" : setup.NtStatus ?? "none")
            + " io_status=" + (setup == null ? "none" : setup.IoStatus ?? "none");
    }
    private static bool Cleanup(WriterFixtureRoot root)
    {
        try
        {
            if (root != null) root.Cleanup();
            WriterLinkFixture.VerifyExternal();
            if (WriterLinkFixture.External != null) WriterLinkFixture.External.Cleanup();
            if (WriterFixtureRoot.UnconfirmedCreation)
                throw new RenameProbeNative.ProbeFailure("cleanup_identity_missing");
            Console.WriteLine("PPT_WRITER_CLEANUP result=PASS stage=cleanup_verify error_type=none code=none winerror=none ntstatus=none io_status=none");
            return true;
        }
        catch (Exception error)
        {
            string evidence = Evidence(error, WriterFixtureRoot.Stage);
            Console.WriteLine("PPT_WRITER_DIAGNOSTIC phase=cleanup " + evidence);
            var failure = error as RenameProbeNative.ProbeFailure;
            Console.WriteLine("PPT_WRITER_CLEANUP result=" + (failure != null && failure.Code == "cleanup_identity_missing"
                ? "INCOMPLETE" : "FAIL") + " " + evidence);
            return false;
        }
    }
    public static int Main(string[] args)
    {
        Console.WriteLine("PPT_WRITER_ARCH bits=" + IntPtr.Size * 8);
        WriterFixtureRoot root = null; int result = 0; string terminal = "PASS"; bool cleanupOnly = false;
        try
        {
            Require(args.Length == 3 || args.Length == 6, "argument_count"); Mode = args[1]; cleanupOnly = Mode == "cleanup";
            Require(cleanupOnly || args.Length == 3, "argument_count");
            using (WindowsIdentity user = WindowsIdentity.GetCurrent())
                if (new WindowsPrincipal(user).IsInRole(WindowsBuiltInRole.Administrator))
                { terminal = "SKIP reason=elevated"; result = 77; }
            if (result == 0)
            {
                string anchor = Path.GetFullPath(args[0]); Guid guid; string leaf = Path.GetFileName(anchor);
                Require(leaf.StartsWith("ppt-writer-", StringComparison.Ordinal) && Guid.TryParseExact(leaf.Substring(11), "N", out guid)
                    && (cleanupOnly || !Directory.Exists(anchor)) && !File.Exists(anchor)
                    && !anchor.StartsWith(@"\\", StringComparison.Ordinal), "fixture_invalid");
                string programData = Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData).TrimEnd('\\') + "\\";
                Require(!anchor.StartsWith(programData, StringComparison.OrdinalIgnoreCase), "programdata_fixture_forbidden");
                DriveInfo drive = new DriveInfo(Path.GetPathRoot(anchor));
                if (drive.DriveType != DriveType.Fixed || drive.DriveFormat != "NTFS")
                { terminal = "SKIP reason=nonlocal_ntfs"; result = 77; }
                else
                {
                    int count = Int32.Parse(args[2]); Require(count == 1 || count == 2, "lease_count_invalid");
                    Machine.NoLinks(Path.GetDirectoryName(anchor));
                    root = cleanupOnly ? WriterFixtureRoot.Handoff(anchor, args) : WriterFixtureRoot.Create(anchor, true);
                    if (!cleanupOnly) Run(anchor, count);
                }
            }
        }
        catch (Exception error)
        {
            string phase = error is WriterFixtureUnavailable ? "fixture" : "primary";
            Console.WriteLine("PPT_WRITER_DIAGNOSTIC phase=" + phase + " " + Evidence(error, stage));
            var setup = error as SetupFailure; var failure = error as RenameProbeNative.ProbeFailure;
            Console.WriteLine("PPT_WRITER_ERROR stage=" + stage + " kind=" + (setup == null ? "contract" : "setup")
                + " code=" + (failure == null ? "exception" : failure.Code));
            if (setup != null)
            {
                RecordRejection(setup);
                if (setup.NativeStage != null) Console.WriteLine("PPT_WRITER_NATIVE stage=" + setup.NativeStage
                    + " ntstatus=" + setup.NtStatus + " io_status=" + (setup.IoStatus ?? "none"));
            }
            var unavailable = error as WriterFixtureUnavailable;
            if (unavailable != null) Console.WriteLine("PPT_WRITER_UNEXECUTED gate=" + Mode
                + " reason=fixture_creation_failed winerror=" + unavailable.WinError);
            result = 1;
            if (cleanupOnly && root == null)
                Console.WriteLine("PPT_WRITER_CLEANUP result=INCOMPLETE " + Evidence(error, "cleanup_validate"));
        }
        // Body and cleanup evidence have separate lifetimes. Exactly one terminal follows both.
        if (root != null && !Cleanup(root)) result = 1;
        else if (root == null && WriterFixtureRoot.UnconfirmedCreation)
        {
            Console.WriteLine("PPT_WRITER_CLEANUP result=INCOMPLETE stage=cleanup_validate error_type=contract code=cleanup_identity_missing winerror=none ntstatus=none io_status=none");
            result = 1;
        }
        if (result == 1) terminal = "FAIL";
        Stage("complete"); Console.WriteLine("PPT_WRITER_RESULT " + terminal); Console.Out.Flush(); return result;
    }
}
namespace AegisDesk.PptSetup
{
    internal static partial class RequestWriter
    {
        static partial void TraceRequest(string stage) { RequestWriterHarness.Trace(stage); }
        static partial void InterceptNative(IntPtr source, IntPtr io, IntPtr rename, uint length, ref bool handled, ref uint status)
        { RequestWriterHarness.Intercept(source, io, rename, ref handled, ref status); }
    }
}
