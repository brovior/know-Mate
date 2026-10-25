using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace AegisDesk.PptSetup
{
    // Namespace lease only. READ sharing does not freeze WRITE_DAC or defeat a
    // malicious trusted directory owner, Administrator, or System account.
    internal sealed class RepairPreparationLease : IDisposable, IReservationNamespace
    {
        [StructLayout(LayoutKind.Sequential)]
        private struct FileInfo
        {
            public uint attributes;
            public System.Runtime.InteropServices.ComTypes.FILETIME creation, access, write;
            public uint volume, sizeHigh, sizeLow, links, indexHigh, indexLow;
        }
        private sealed class Entry
        {
            public string path, stage, policy, security;
            public SafeFileHandle handle;
            public FileInfo identity;
        }
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern SafeFileHandle CreateFile(string path, uint access, uint share, IntPtr attributes, uint disposition, uint flags, IntPtr template);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetFileInformationByHandle(SafeFileHandle handle, out FileInfo info);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern uint GetFinalPathNameByHandle(SafeFileHandle handle, StringBuilder path, uint length, uint flags);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool GetVolumeInformationByHandleW(SafeFileHandle handle, StringBuilder label, uint labelSize,
            out uint serial, out uint maximumComponent, out uint flags, StringBuilder fileSystem, uint fileSystemSize);
        [DllImport("advapi32.dll")]
        private static extern uint GetSecurityInfo(SafeFileHandle handle, int objectType, uint information,
            out IntPtr owner, out IntPtr group, out IntPtr dacl, out IntPtr sacl, out IntPtr descriptor);
        [DllImport("advapi32.dll")]
        private static extern uint GetSecurityDescriptorLength(IntPtr descriptor);
        [DllImport("kernel32.dll")]
        private static extern IntPtr LocalFree(IntPtr memory);

        private readonly List<Entry> entries = new List<Entry>();
        private bool disposed;
        private string helperSid;
        public string ChildPath { get { if (disposed || entries.Count == 0) throw new SetupFailure("lease_recheck", "policy"); return entries[entries.Count - 1].path; } }
        internal string ParentPath { get { return Path.GetDirectoryName(ChildPath); } }
        internal string ParentIdentity
        {
            get
            {
                Entry parent = entries[entries.Count - 2];
                return parent.identity.volume.ToString("X8") + ":" + parent.identity.indexHigh.ToString("X8") + parent.identity.indexLow.ToString("X8");
            }
        }
        internal SafeFileHandle OpenMutationHandle()
        {
            Recheck();
            var handle = ParentSecurity.OpenSecurity(ParentPath, true);
            try
            {
                FileInfo identity;
                Entry parent = entries[entries.Count - 2];
                if (!GetFileInformationByHandle(handle, out identity) || identity.volume != parent.identity.volume ||
                    identity.indexHigh != parent.identity.indexHigh || identity.indexLow != parent.identity.indexLow)
                    throw new SetupFailure("parent_repair_verify", "policy");
                return handle;
            }
            catch { handle.Dispose(); throw; }
        }
        internal void RecheckMutation(string expected)
        {
            if (disposed) throw new SetupFailure("parent_repair_verify", "policy");
            string[] children = Directory.GetFileSystemEntries(ParentPath);
            if (children.Length != 1 || !String.Equals(children[0], ChildPath, StringComparison.OrdinalIgnoreCase))
                throw new SetupFailure("parent_repair_verify", "policy");
            foreach (Entry entry in entries)
            {
                FileInfo now = Inspect(entry);
                if (now.volume != entry.identity.volume || now.indexHigh != entry.identity.indexHigh || now.indexLow != entry.identity.indexLow)
                    throw new SetupFailure("parent_repair_verify", "policy");
                if (entry.policy == null) continue;
                DirectorySecurity acl = ReadSecurity(entry.handle);
                if (entry.policy == "parent")
                {
                    // 일반 writer Recheck는 원래 ACL만 허용하며 변경 중에는 사용하지 않는다.
                    using (var security = ParentSecurity.OpenSecurity(entry.path, false))
                        if (ParentSecurity.Key(ParentSecurity.Read(security)) != expected)
                            throw new SetupFailure("parent_repair_verify", "policy");
                }
                else if (!RequestDirectory.Compatible(acl, false) || SecurityKey(acl) != entry.security)
                    throw new SetupFailure("parent_repair_verify", "policy");
            }
        }
        internal static RepairPreparationLease Acquire(string sid)
        { return AcquireAt(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), sid); }
        // 제품에서는 임의 경로를 지정할 수 없는 내부 시험용 진입점.
        internal static RepairPreparationLease AcquireAt(string anchor, string sid)
        {
            var lease = new RepairPreparationLease { helperSid = sid };
            try
            {
                string full = Path.GetFullPath(anchor).TrimEnd(Path.DirectorySeparatorChar);
                if (full.StartsWith(@"\\", StringComparison.Ordinal) || !Path.IsPathRooted(full))
                    throw new SetupFailure("parent_repair_check", "policy");
                var chain = new Stack<string>();
                for (string path = full; path != null; path = Path.GetDirectoryName(path)) chain.Push(path);
                while (chain.Count != 0) lease.Open(chain.Pop(), "anchor_open", null);
                string parent = Path.Combine(full, "AegisDesk");
                lease.EnsureOpen(parent, "parent_open", "parent_repair_check", true, false);
                lease.EnsureOpen(Path.Combine(parent, "PptReaderRequests"), "child_open", "child_policy", false, false);
                lease.Recheck(); return lease;
            }
            catch { lease.Dispose(); throw; }
        }
        private void EnsureOpen(string path, string openStage, string policyStage, bool parent, bool create)
        {
            try
            {
                if (create) RequestDirectory.Create(path, RequestDirectory.Expected(parent), true);
                Open(path, openStage, parent ? "parent" : "child");
            }
            catch (SetupFailure) { throw; }
            catch (Exception error) { throw SetupFailure.From(openStage, error); }
            Entry entry = entries[entries.Count - 1];
            try
            {
                DirectorySecurity acl = ReadSecurity(entry.handle);
                if (!(parent ? ParentSecurity.Legacy(acl, helperSid) : RequestDirectory.Compatible(acl, false))) throw new SetupFailure(policyStage, "policy");
                entry.security = SecurityKey(acl);
            }
            catch (SetupFailure) { throw; }
            catch (Exception error) { throw SetupFailure.From(policyStage, error); }
        }
        private void Open(string path, string stage, string policy)
        {
            SafeFileHandle handle = null;
            try
            {
                // FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES | READ_CONTROL |
                // SYNCHRONIZE, FILE_SHARE_READ, OPEN_EXISTING, no reparse follow.
                handle = CreateFile(path, 0x00120081, 1, IntPtr.Zero, 3, 0x02200000, IntPtr.Zero);
                if (handle.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error());
                var entry = new Entry { path = Path.GetFullPath(path), stage = stage, policy = policy, handle = handle };
                entry.identity = Inspect(entry);
                entries.Add(entry); handle = null;
            }
            catch (Exception error) { throw SetupFailure.From(stage, error); }
            finally { if (handle != null) handle.Dispose(); }
        }
        private static FileInfo Inspect(Entry entry)
        {
            FileInfo info;
            if (!GetFileInformationByHandle(entry.handle, out info)) throw new Win32Exception(Marshal.GetLastWin32Error());
            if ((info.attributes & 0x10) == 0 || (info.attributes & 0x400) != 0) throw new SetupFailure(entry.stage, "policy");
            var actual = new StringBuilder(32768);
            uint length = GetFinalPathNameByHandle(entry.handle, actual, (uint)actual.Capacity, 0);
            if (length == 0) throw new Win32Exception(Marshal.GetLastWin32Error());
            if (length >= actual.Capacity) throw new SetupFailure(entry.stage, "policy");
            string final = actual.ToString();
            if (!final.StartsWith(@"\\?\", StringComparison.Ordinal) || final.StartsWith(@"\\?\UNC\", StringComparison.OrdinalIgnoreCase))
                throw new SetupFailure(entry.stage, "policy");
            if (!String.Equals(final.Substring(4).TrimEnd('\\'), entry.path.TrimEnd('\\'), StringComparison.OrdinalIgnoreCase))
                throw new SetupFailure(entry.stage, "policy");
            var fs = new StringBuilder(32);
            uint serial, component, flags;
            if (!GetVolumeInformationByHandleW(entry.handle, null, 0, out serial, out component, out flags, fs, (uint)fs.Capacity))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            if (fs.ToString() != "NTFS" || serial != info.volume) throw new SetupFailure(entry.stage, "policy");
            return info;
        }
        private static DirectorySecurity ReadSecurity(SafeFileHandle handle)
        {
            IntPtr owner, group, dacl, sacl, descriptor;
            uint code = GetSecurityInfo(handle, 1, 5, out owner, out group, out dacl, out sacl, out descriptor);
            if (code != 0) throw new Win32Exception((int)code);
            try
            {
                uint length = GetSecurityDescriptorLength(descriptor);
                if (length == 0 || length > 1048576) throw new IOException("security_descriptor_invalid");
                var bytes = new byte[(int)length]; Marshal.Copy(descriptor, bytes, 0, bytes.Length);
                var acl = new DirectorySecurity(); acl.SetSecurityDescriptorBinaryForm(bytes); return acl;
            }
            finally { LocalFree(descriptor); }
        }
        private static string SecurityKey(DirectorySecurity acl)
        { return acl.GetSecurityDescriptorSddlForm(AccessControlSections.Owner | AccessControlSections.Access); }
        public void Recheck()
        {
            if (disposed) throw new SetupFailure("lease_recheck", "policy");
            try
            {
                string parent = Path.GetDirectoryName(ChildPath);
                string[] children = Directory.GetFileSystemEntries(parent);
                if (children.Length != 1 || !String.Equals(children[0], ChildPath, StringComparison.OrdinalIgnoreCase))
                    throw new SetupFailure("parent_repair_check", "policy");
                foreach (Entry entry in entries)
                {
                    FileInfo now = Inspect(entry);
                    if (now.volume != entry.identity.volume || now.indexHigh != entry.identity.indexHigh || now.indexLow != entry.identity.indexLow)
                        throw new SetupFailure("lease_recheck", "policy");
                    if (entry.policy == null) continue;
                    DirectorySecurity acl = ReadSecurity(entry.handle);
                    if (!(entry.policy == "parent" ? ParentSecurity.Legacy(acl, helperSid) : RequestDirectory.Compatible(acl, false)) || SecurityKey(acl) != entry.security)
                        throw new SetupFailure("lease_recheck", "policy");
                }
            }
            catch (Exception error) { throw SetupFailure.From("lease_recheck", error); }
        }
        public void Dispose()
        {
            if (disposed) return;
            disposed = true;
            for (int index = entries.Count - 1; index >= 0; index--) entries[index].handle.Dispose();
            entries.Clear();
        }
    }
}
