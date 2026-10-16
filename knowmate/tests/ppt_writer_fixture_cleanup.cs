using System;
using System.IO;
using System.Security.AccessControl;
using System.Security.Principal;
using Microsoft.Win32.SafeHandles;
using AegisDesk.PptSetup;

// Only a UUID root created by this invocation (or explicitly handed off) is eligible.
internal sealed class WriterFixtureRoot
{
    internal readonly string PathName, Id;
    internal static string Stage = "cleanup_validate";
    internal static bool UnconfirmedCreation;
    private WriterFixtureRoot(string path, string id) { PathName = path; Id = id; }
    internal static WriterFixtureRoot Create(string path, bool emit)
    {
        ValidatePath(path); WriterFixtureNative.NewDirectory(path); UnconfirmedCreation = true;
        using (SafeFileHandle handle = WriterFixtureNative.Pin(path))
        {
            var info = WriterFixtureNative.Inspect(handle);
            Require((info.Attributes & 0x410) == 0x10 && WriterFixtureNative.Owned(handle), "fixture_identity");
            var root = new WriterFixtureRoot(path, info.Id);
            if (emit)
            {
                Console.WriteLine("PPT_WRITER_FIXTURE_ID volume=" + info.Volume + " high=" + info.High + " low=" + info.Low);
                Console.Out.Flush();
            }
            UnconfirmedCreation = false; return root;
        }
    }
    internal static WriterFixtureRoot Handoff(string path, string[] args)
    {
        Require(args.Length == 6, "cleanup_identity_missing");
        uint volume = 0, high = 0, low = 0;
        Require(UInt32.TryParse(args[3], out volume) && UInt32.TryParse(args[4], out high)
            && UInt32.TryParse(args[5], out low), "cleanup_identity_missing");
        return new WriterFixtureRoot(path, volume + ":" + high + ":" + low);
    }
    internal static void Require(bool value, string code)
    { if (!value) throw new RenameProbeNative.ProbeFailure(code); }
    private static void ValidatePath(string path)
    {
        Guid id; string leaf = System.IO.Path.GetFileName(path);
        Require(leaf.StartsWith("ppt-writer-", StringComparison.Ordinal) && Guid.TryParseExact(leaf.Substring(11), "N", out id)
            && path == System.IO.Path.GetFullPath(path) && !path.StartsWith(@"\\", StringComparison.Ordinal), "cleanup_path_invalid");
        string programData = Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData).TrimEnd('\\') + "\\";
        Require(!path.StartsWith(programData, StringComparison.OrdinalIgnoreCase), "cleanup_path_invalid");
        var drive = new DriveInfo(System.IO.Path.GetPathRoot(path));
        Require(drive.DriveType == DriveType.Fixed && drive.DriveFormat == "NTFS", "cleanup_path_invalid");
        string parent = System.IO.Path.GetDirectoryName(path);
        Machine.NoLinks(parent);
        using (SafeFileHandle handle = WriterFixtureNative.Pin(parent))
            Require((WriterFixtureNative.Inspect(handle).Attributes & 0x410) == 0x10
                && WriterFixtureNative.Owned(handle), "cleanup_owner_mismatch");
    }
    internal void Cleanup()
    {
        Stage = "cleanup_validate"; ValidatePath(PathName);
        // Retain non-delete-sharing regular handles through enumeration and ACL repair.
        using (SafeFileHandle root = WriterFixtureNative.Pin(PathName))
        {
            var info = WriterFixtureNative.Inspect(root);
            Require(info.Id == Id && (info.Attributes & 0x410) == 0x10, "cleanup_identity_mismatch");
            Require(WriterFixtureNative.Owned(root), "cleanup_owner_mismatch");
            CleanDirectory(PathName, root);
        }
        Stage = "cleanup_delete"; WriterFixtureNative.Remove(PathName, true);
    }
    private static void CleanDirectory(string directory, SafeFileHandle handle)
    {
        Require((WriterFixtureNative.Inspect(handle).Attributes & 0x410) == 0x10, "cleanup_identity_mismatch");
        Require(WriterFixtureNative.Owned(handle), "cleanup_owner_mismatch");
        Stage = "cleanup_acl";
        DirectorySecurity acl = Directory.GetAccessControl(directory);
        using (WindowsIdentity identity = WindowsIdentity.GetCurrent())
        {
            bool full = false;
            foreach (FileSystemAccessRule rule in acl.GetAccessRules(true, true, typeof(SecurityIdentifier)))
                if (rule.IdentityReference == identity.User && rule.AccessControlType == AccessControlType.Allow
                    && (rule.PropagationFlags & PropagationFlags.InheritOnly) == 0
                    && (rule.FileSystemRights & FileSystemRights.FullControl) == FileSystemRights.FullControl) full = true;
            if (!full)
            {
                acl.AddAccessRule(new FileSystemAccessRule(identity.User, FileSystemRights.FullControl,
                    InheritanceFlags.None, PropagationFlags.None, AccessControlType.Allow));
                Directory.SetAccessControl(directory, acl);
            }
        }
        Stage = "cleanup_enumerate";
        foreach (string item in Directory.GetFileSystemEntries(directory, "*", SearchOption.TopDirectoryOnly))
        {
            bool isDirectory;
            using (SafeFileHandle child = WriterFixtureNative.Pin(item))
            {
                var info = WriterFixtureNative.Inspect(child); isDirectory = (info.Attributes & 0x10) != 0;
                Require(WriterFixtureNative.Owned(child), "cleanup_owner_mismatch");
                // Never enumerate, change attributes, or repair ACLs through a reparse point.
                if ((info.Attributes & 0x400) == 0)
                {
                    if (isDirectory) CleanDirectory(item, child);
                    else if ((info.Attributes & 1) != 0) File.SetAttributes(item, (FileAttributes)(info.Attributes & ~1u));
                }
            }
            Stage = "cleanup_delete"; WriterFixtureNative.Remove(item, isDirectory);
        }
    }
}
