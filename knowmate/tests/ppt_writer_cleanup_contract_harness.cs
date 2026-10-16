using System;
using System.ComponentModel;
using System.IO;
using System.Security.AccessControl;
using System.Security.Principal;

// Additional native safety checks; these do not change the 62 product cases.
internal static class WriterCleanupContractHarness
{
    private static void Require(bool value) { if (!value) throw new RenameProbeNative.ProbeFailure("cleanup_contract"); }
    private static void Refused(Action action, string code)
    {
        try { action(); }
        catch (RenameProbeNative.ProbeFailure failure) { Require(failure.Code == code); return; }
        throw new RenameProbeNative.ProbeFailure("cleanup_not_refused");
    }
    private static string stage = "preflight";
    private static void Record(string phase, string at, Exception error)
    {
        var native = error as Win32Exception; var failure = error as RenameProbeNative.ProbeFailure;
        string type = native != null ? "Win32Exception" : failure != null ? "contract"
            : error is UnauthorizedAccessException ? "access" : error is IOException ? "IOException" : "other";
        Console.WriteLine("PPT_CLEANUP_DIAGNOSTIC phase=" + phase + " stage=" + at + " error_type=" + type
            + " code=" + (failure == null ? "exception" : failure.Code)
            + " winerror=" + (native == null ? "none" : native.NativeErrorCode.ToString()));
        Console.Out.Flush();
    }
    public static int Main(string[] args)
    {
        try
        {
            if (args.Length != 1) throw new RenameProbeNative.ProbeFailure("argument_count");
            using (WindowsIdentity identity = WindowsIdentity.GetCurrent())
                if (new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator)) return 77;
            var drive = new DriveInfo(Path.GetPathRoot(Path.GetFullPath(args[0])));
            if (drive.DriveType != DriveType.Fixed || drive.DriveFormat != "NTFS") return 77;
        }
        catch (Exception error) { Record("primary", "preflight", error); return 1; }
        WriterFixtureRoot root = null, external = null, linkRoot = null;
        bool rootPresent = false; int result = 0;
        try
        {
            string path = Path.Combine(args[0], "ppt-writer-" + Guid.NewGuid().ToString("N"));
            stage = "fixture_create";
            root = WriterFixtureRoot.Create(path, true); rootPresent = true;
            string file = Path.Combine(path, "sentinel.fixture"); File.WriteAllText(file, "sentinel");
            string id = WriterFixtureNative.Inspect(file).Id, security = WriterFixtureNative.Security(file);
            stage = "refusal_missing_id";
            Refused(delegate { WriterFixtureRoot.Handoff(path, new[] {path, "cleanup", "1"}); }, "cleanup_identity_missing");
            string[] parts = root.Id.Split(':');
            string wrong = (UInt32.Parse(parts[2]) ^ 1u).ToString();
            var other = WriterFixtureRoot.Handoff(path, new[] {path, "cleanup", "1", parts[0], parts[1], wrong});
            stage = "refusal_wrong_id";
            Refused(delegate { other.Cleanup(); }, "cleanup_identity_mismatch");
            stage = "preservation_verify";
            Require(File.ReadAllText(file) == "sentinel" && WriterFixtureNative.Inspect(file).Id == id
                && WriterFixtureNative.Security(file) == security);
            string externalPath = Path.Combine(args[0], "ppt-writer-" + Guid.NewGuid().ToString("N"));
            external = WriterFixtureRoot.Create(externalPath, false);
            string externalFile = Path.Combine(externalPath, "external.fixture"); File.WriteAllText(externalFile, "external");
            string externalId = WriterFixtureNative.Inspect(externalFile).Id;
            string externalSecurity = WriterFixtureNative.Security(externalFile), directorySecurity = WriterFixtureNative.Security(externalPath);
            stage = "fixture_junction";
            WriterFixtureNative.Junction(Path.Combine(path, "junction.fixture"), externalPath);
            File.SetAttributes(file, FileAttributes.ReadOnly);
            string regular = Path.Combine(path, "strict.fixture"); Directory.CreateDirectory(regular);
            var acl = new DirectorySecurity(); acl.SetAccessRuleProtection(true, false);
            using (WindowsIdentity identity = WindowsIdentity.GetCurrent())
            {
                acl.SetOwner(identity.User);
                acl.AddAccessRule(new FileSystemAccessRule(identity.User, FileSystemRights.ReadAndExecute | FileSystemRights.ChangePermissions,
                    InheritanceFlags.None, PropagationFlags.None, AccessControlType.Allow));
            }
            Directory.SetAccessControl(regular, acl);
            stage = "cleanup_regular";
            root.Cleanup(); rootPresent = false;
            stage = "external_verify";
            Require(!Directory.Exists(path) && File.ReadAllText(externalFile) == "external"
                && WriterFixtureNative.Inspect(externalPath).Id == external.Id
                && WriterFixtureNative.Inspect(externalFile).Id == externalId
                && WriterFixtureNative.Security(externalFile) == externalSecurity
                && WriterFixtureNative.Security(externalPath) == directorySecurity);
            // The original ID still matches after converting the owned root to a junction.
            stage = "refusal_reparse_root";
            linkRoot = WriterFixtureRoot.Create(path, false);
            WriterFixtureNative.Junction(path, externalPath, false);
            Require(WriterFixtureNative.Inspect(path).Id == linkRoot.Id);
            Refused(delegate { linkRoot.Cleanup(); }, "cleanup_identity_mismatch");
            Require(File.ReadAllText(externalFile) == "external" && WriterFixtureNative.Security(externalPath) == directorySecurity);
        }
        catch (Exception error) { result = 1; Record("primary", stage, error); }
        finally
        {
            // Every finalizer uses the identity captured at its own successful creation.
            bool rootError = false;
            try
            {
                if (linkRoot != null)
                {
                    WriterFixtureNative.Info info;
                    using (var handle = WriterFixtureNative.Pin(linkRoot.PathName))
                    { info = WriterFixtureNative.Inspect(handle); Require(info.Id == linkRoot.Id && WriterFixtureNative.Owned(handle)); }
                    if ((info.Attributes & 0x400) != 0) WriterFixtureNative.Remove(linkRoot.PathName, true);
                    else linkRoot.Cleanup();
                }
                if (rootPresent) root.Cleanup();
            }
            catch (Exception error) { result = 1; rootError = true; Record("cleanup", "cleanup_root", error); }
            try { if (external != null) external.Cleanup(); }
            catch (Exception error) { result = 1; Record("cleanup", "cleanup_external", error); }
            if (WriterFixtureRoot.UnconfirmedCreation && !rootError)
            { result = 1; Record("cleanup", "cleanup_root", new RenameProbeNative.ProbeFailure("cleanup_identity_missing")); }
        }
        Console.WriteLine("PPT_WRITER_CLEANUP_CONTRACT " + (result == 0 ? "PASS" : "FAIL"));
        return result;
    }
}
