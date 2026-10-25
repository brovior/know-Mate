using System;
using System.Collections.Generic;
using System.IO;
using System.Security.Cryptography;
using System.Threading;
using Microsoft.Win32.SafeHandles;

namespace AegisDesk.PptSetup
{
    /// <summary>보호된 감사 백업의 이름 보존 실패만 한 번 재검사해 재시도한다.</summary>
    internal static partial class BackupCommit
    {
        static partial void TraceBackupCommit(string stage, string source, string target);

        private sealed class DirectoryPin : IDisposable
        {
            internal string Path, Identity, Security;
            internal SafeFileHandle Handle;
            public void Dispose() { if (Handle != null) Handle.Dispose(); }
        }

        private sealed class FileSnapshot
        {
            internal string Identity, Security, Hash;
            internal long Length;
            internal bool Same(FileSnapshot other)
            { return Identity == other.Identity && Security == other.Security && Hash == other.Hash && Length == other.Length; }
        }

        private static SetupFailure Changed()
        { return new SetupFailure("parent_repair_backup", "policy") { PolicyReason = "namespace_changed" }; }

        private static void ValidateNames(string source, string target)
        {
            if (!Machine.Admin || String.IsNullOrEmpty(source) || String.IsNullOrEmpty(target)) throw Changed();
            string root = System.IO.Path.GetFullPath(RepairBackup.Root);
            if (!String.Equals(System.IO.Path.GetDirectoryName(target), root, StringComparison.OrdinalIgnoreCase) ||
                !String.Equals(System.IO.Path.GetDirectoryName(source), root, StringComparison.OrdinalIgnoreCase) ||
                !String.Equals(System.IO.Path.GetFullPath(target), target, StringComparison.OrdinalIgnoreCase) ||
                !String.Equals(System.IO.Path.GetFullPath(source), source, StringComparison.OrdinalIgnoreCase)) throw Changed();
            string name = System.IO.Path.GetFileName(target);
            Guid request;
            if (name.Length != 41 || !name.EndsWith(".json", StringComparison.Ordinal) ||
                !Guid.TryParseExact(name.Substring(0, 36), "D", out request) ||
                request.ToString("D") + ".json" != name) throw Changed();
            string temporary = System.IO.Path.GetFileName(source), prefix = name + ".";
            Guid unique;
            if (!temporary.StartsWith(prefix, StringComparison.Ordinal) || temporary.Length != prefix.Length + 36 ||
                !temporary.EndsWith(".tmp", StringComparison.Ordinal) ||
                !Guid.TryParseExact(temporary.Substring(prefix.Length, 32), "N", out unique) ||
                unique.ToString("N") != temporary.Substring(prefix.Length, 32)) throw Changed();
        }

        private static List<DirectoryPin> PinDirectories()
        {
            var pins = new List<DirectoryPin>();
            try
            {
                var paths = new Stack<string>();
                for (string path = RepairBackup.Root; path != null; path = System.IO.Path.GetDirectoryName(path)) paths.Push(path);
                while (paths.Count != 0)
                {
                    string path = paths.Pop(); Machine.NoLinks(path);
                    var pin = new DirectoryPin { Path = path, Handle = ParentSecurity.OpenSecurity(path, false) };
                    pins.Add(pin);
                    pin.Identity = ParentSecurity.Inspect(pin.Handle, path, true);
                    var acl = ParentSecurity.Read(pin.Handle);
                    if (String.Equals(path, RepairBackup.Root, StringComparison.OrdinalIgnoreCase)) RepairBackup.ValidateDirectory(acl);
                    else if (String.Equals(path, Machine.Root, StringComparison.OrdinalIgnoreCase) && !LegacyInstallation.CompatibleBoundary(acl, false)) throw Changed();
                    else if (String.Equals(path, System.IO.Path.GetDirectoryName(Machine.Root), StringComparison.OrdinalIgnoreCase) &&
                        !LegacyInstallation.CompatibleBoundary(acl, true)) throw Changed();
                    pin.Security = Convert.ToBase64String(acl.GetSecurityDescriptorBinaryForm());
                }
                return pins;
            }
            catch { DisposePins(pins); throw; }
        }

        private static void DisposePins(List<DirectoryPin> pins)
        { for (int index = pins.Count - 1; index >= 0; index--) pins[index].Dispose(); }

        private static void RecheckDirectories(List<DirectoryPin> pins)
        {
            foreach (var pin in pins)
                if (ParentSecurity.Inspect(pin.Handle, pin.Path, true) != pin.Identity ||
                    Convert.ToBase64String(ParentSecurity.Read(pin.Handle).GetSecurityDescriptorBinaryForm()) != pin.Security) throw Changed();
        }

        private static FileSnapshot Snapshot(string path)
        {
            Machine.NoLinks(path);
            using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
            {
                var result = new FileSnapshot();
                result.Identity = ParentSecurity.Inspect(stream.SafeFileHandle, path, false);
                var acl = ParentSecurity.Read(stream.SafeFileHandle); RepairBackup.ValidateFileSecurity(acl);
                result.Security = Convert.ToBase64String(acl.GetSecurityDescriptorBinaryForm());
                result.Length = stream.Length;
                if (result.Length > 1048576) throw Changed();
                using (var sha = SHA256.Create()) result.Hash = Convert.ToBase64String(sha.ComputeHash(stream));
                if (ParentSecurity.Inspect(stream.SafeFileHandle, path, false) != result.Identity ||
                    Convert.ToBase64String(ParentSecurity.Read(stream.SafeFileHandle).GetSecurityDescriptorBinaryForm()) != result.Security) throw Changed();
                return result;
            }
        }

        private static void NoOtherTemporary(string source, string target)
        {
            string prefix = System.IO.Path.GetFileName(target) + ".";
            foreach (string path in Directory.GetFileSystemEntries(RepairBackup.Root))
                if (!String.Equals(path, source, StringComparison.OrdinalIgnoreCase) &&
                    System.IO.Path.GetFileName(path).StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) throw Changed();
        }

        private static void RequireSourceAbsent(string source)
        {
            try { File.GetAttributes(source); }
            catch (FileNotFoundException) { return; }
            throw Changed();
        }

        internal static void Commit(string source, string target)
        {
            ValidateNames(source, target);
            var pins = PinDirectories();
            try
            {
                NoOtherTemporary(source, target);
                FileSnapshot beforeSource = Snapshot(source), beforeTarget = Snapshot(target);
                RecheckDirectories(pins);
                try
                {
                    TraceBackupCommit("before_first", source, target);
                    File.Replace(source, target, null);
                }
                catch (IOException error)
                {
                    // 1175는 두 원래 이름 보존을 보장하므로 다른 오류는 재시도하지 않는다.
                    if (error.HResult != unchecked((int)0x80070497)) throw;
                    TraceBackupCommit("after_first_failure", source, target);
                    Thread.Sleep(100);
                    TraceBackupCommit("before_retry", source, target);
                    RecheckDirectories(pins); NoOtherTemporary(source, target);
                    if (!beforeSource.Same(Snapshot(source)) || !beforeTarget.Same(Snapshot(target))) throw Changed();
                    RecheckDirectories(pins);
                    TraceBackupCommit("replace_retry", source, target);
                    File.Replace(source, target, null);
                }
                RecheckDirectories(pins);
                FileSnapshot committed = Snapshot(target);
                if (committed.Identity != beforeSource.Identity || committed.Hash != beforeSource.Hash ||
                    committed.Length != beforeSource.Length) throw Changed();
                RequireSourceAbsent(source);
                NoOtherTemporary(source, target);
                RecheckDirectories(pins);
            }
            finally { DisposePins(pins); }
        }
    }
}
