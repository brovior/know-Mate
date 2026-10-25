using System;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace AegisDesk.PptSetup
{
    /// <summary>확인한 구버전 부모 ACL만 객체 단위로 좁힌다.</summary>
    internal static class ParentSecurity
    {
        [StructLayout(LayoutKind.Sequential)]
        private struct FileIdentity
        {
            public uint attributes;
            public System.Runtime.InteropServices.ComTypes.FILETIME creation, access, write;
            public uint volume, sizeHigh, sizeLow, links, indexHigh, indexLow;
        }
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetFileInformationByHandle(SafeFileHandle handle, out FileIdentity identity);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern uint GetFinalPathNameByHandle(SafeFileHandle handle, StringBuilder path, uint length, uint flags);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool GetVolumeInformationByHandleW(SafeFileHandle handle, StringBuilder label, uint size, out uint serial,
            out uint maximum, out uint flags, StringBuilder filesystem, uint filesystemSize);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern SafeFileHandle CreateFile(string path, uint access, uint share, IntPtr attributes, uint disposition, uint flags, IntPtr template);
        [DllImport("ntdll.dll")]
        private static extern int NtSetSecurityObject(SafeFileHandle handle, uint information, byte[] descriptor);
        [DllImport("advapi32.dll")]
        private static extern uint GetSecurityInfo(SafeFileHandle handle, int type, uint information, out IntPtr owner, out IntPtr group, out IntPtr dacl, out IntPtr sacl, out IntPtr descriptor);
        [DllImport("advapi32.dll")]
        private static extern uint GetSecurityDescriptorLength(IntPtr descriptor);
        [DllImport("kernel32.dll")]
        private static extern IntPtr LocalFree(IntPtr descriptor);

        internal static SafeFileHandle OpenSecurity(string path, bool writable)
        {
            var handle = CreateFile(path, writable ? 0x00060000U : 0x00020000U, 1, IntPtr.Zero, 3, 0x02200000, IntPtr.Zero);
            if (handle.IsInvalid) { int code = Marshal.GetLastWin32Error(); handle.Dispose(); throw new Win32Exception(code); }
            return handle;
        }

        internal static string Inspect(SafeFileHandle handle, string path, bool directory)
        {
            FileIdentity identity;
            var actual = new StringBuilder(32768);
            if (!GetFileInformationByHandle(handle, out identity) || (identity.attributes & 0x400) != 0 ||
                ((identity.attributes & 0x10) != 0) != directory ||
                (!directory && identity.links != 1)) throw new SetupFailure("parent_repair_verify", "policy");
            uint length = GetFinalPathNameByHandle(handle, actual, (uint)actual.Capacity, 0);
            if (length == 0 || length >= actual.Capacity ||
                !String.Equals(actual.ToString(), @"\\?\" + Path.GetFullPath(path).TrimEnd('\\'), StringComparison.OrdinalIgnoreCase))
                throw new SetupFailure("parent_repair_verify", "policy");
            var filesystem = new StringBuilder(32); uint serial, maximum, flags;
            if (!GetVolumeInformationByHandleW(handle, null, 0, out serial, out maximum, out flags, filesystem, (uint)filesystem.Capacity) ||
                filesystem.ToString() != "NTFS" || serial != identity.volume) throw new SetupFailure("parent_repair_verify", "policy");
            return identity.volume.ToString("X8") + ":" + identity.indexHigh.ToString("X8") + identity.indexLow.ToString("X8");
        }

        internal static DirectorySecurity Read(SafeFileHandle handle)
        {
            IntPtr owner, group, dacl, sacl, descriptor;
            uint code = GetSecurityInfo(handle, 1, 7, out owner, out group, out dacl, out sacl, out descriptor);
            if (code != 0) throw new Win32Exception((int)code);
            try
            {
                uint length = GetSecurityDescriptorLength(descriptor);
                if (length == 0 || length > 1048576) throw new IOException("security_descriptor_invalid");
                var bytes = new byte[(int)length]; Marshal.Copy(descriptor, bytes, 0, bytes.Length);
                var result = new DirectorySecurity(); result.SetSecurityDescriptorBinaryForm(bytes); return result;
            }
            finally { LocalFree(descriptor); }
        }

        internal static string Key(DirectorySecurity acl)
        { return acl.GetSecurityDescriptorSddlForm(AccessControlSections.Owner | AccessControlSections.Group | AccessControlSections.Access); }

        internal static bool Legacy(DirectorySecurity acl, string helperSid)
        {
            if (!acl.AreAccessRulesCanonical) return false;
            var raw = new RawSecurityDescriptor(acl.GetSecurityDescriptorBinaryForm(), 0);
            if (raw.Owner == null || raw.DiscretionaryAcl == null ||
                (raw.Owner.Value != helperSid && raw.Owner.Value != "S-1-5-32-544" && raw.Owner.Value != "S-1-5-18")) return false;
            bool write = false, ownerAccess = false, usersRead = false, systemAccess = false, adminAccess = false;
            foreach (GenericAce entry in raw.DiscretionaryAcl)
            {
                var ace = entry as CommonAce;
                if (ace == null || ace.IsCallback || ace.AceQualifier != AceQualifier.AccessAllowed) return false;
                if (acl.AreAccessRulesProtected && (ace.AceFlags & AceFlags.Inherited) != 0) return false;
                AceFlags flags = ace.AceFlags & ~AceFlags.Inherited;
                string sid = ace.SecurityIdentifier.Value;
                if (sid == "S-1-3-0")
                {
                    if (flags != (AceFlags.ObjectInherit | AceFlags.ContainerInherit | AceFlags.InheritOnly) ||
                        (ace.AccessMask != 0x001f01ff && ace.AccessMask != 0x10000000)) return false;
                }
                else if (sid == raw.Owner.Value || sid == "S-1-5-32-544" || sid == "S-1-5-18")
                {
                    if (ace.AccessMask != 0x001f01ff ||
                        (flags != AceFlags.None && flags != (AceFlags.ObjectInherit | AceFlags.ContainerInherit) &&
                         !(sid == raw.Owner.Value && flags == (AceFlags.ObjectInherit | AceFlags.ContainerInherit | AceFlags.InheritOnly)))) return false;
                    if (sid == raw.Owner.Value && (flags & AceFlags.InheritOnly) == 0) ownerAccess = true;
                    if (sid == "S-1-5-18" && flags == (AceFlags.ObjectInherit | AceFlags.ContainerInherit)) systemAccess = true;
                    if (sid == "S-1-5-32-544" && flags == (AceFlags.ObjectInherit | AceFlags.ContainerInherit)) adminAccess = true;
                }
                else if (sid == "S-1-5-32-545")
                {
                    if (ace.AccessMask == 0x001200a9 && flags == (AceFlags.ObjectInherit | AceFlags.ContainerInherit)) usersRead = true;
                    else if (ace.AccessMask == 0x00100116 && flags == AceFlags.ContainerInherit) write = true;
                    else if (ace.AccessMask != 0x001200ad || flags != AceFlags.None) return false;
                }
                else return false;
            }
            return ownerAccess && usersRead && write && systemAccess && adminAccess;
        }

        internal static DirectorySecurity Transform(DirectorySecurity before, string helperSid, bool final)
        {
            if (!Legacy(before, helperSid)) throw new SetupFailure("parent_repair_check", "policy");
            var raw = new RawSecurityDescriptor(before.GetSecurityDescriptorBinaryForm(), 0);
            raw.SetFlags((raw.ControlFlags | ControlFlags.DiscretionaryAclProtected) &
                ~(ControlFlags.DiscretionaryAclAutoInheritRequired | ControlFlags.DiscretionaryAclAutoInherited));
            for (int index = raw.DiscretionaryAcl.Count - 1; index >= 0; index--)
            {
                var ace = (CommonAce)raw.DiscretionaryAcl[index]; ace.AceFlags &= ~AceFlags.Inherited;
                if (final && ace.SecurityIdentifier.Value == "S-1-5-32-545" && ace.AccessMask == 0x00100116)
                    raw.DiscretionaryAcl.RemoveAce(index);
            }
            var bytes = new byte[raw.BinaryLength]; raw.GetBinaryForm(bytes, 0);
            var result = new DirectorySecurity(); result.SetSecurityDescriptorBinaryForm(bytes);
            if (final)
                result.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier("S-1-5-32-545"),
                    FileSystemRights.ReadAndExecute | FileSystemRights.CreateDirectories, AccessControlType.Allow));
            return result;
        }

        internal static void Write(SafeFileHandle handle, DirectorySecurity expected)
        {
            // SetSecurityInfo는 하위 ACL을 전파하므로 여기서 대체 호출하지 않는다.
            int status = NtSetSecurityObject(handle, 4, expected.GetSecurityDescriptorBinaryForm());
            if (status != 0) throw new SetupFailure("parent_repair_write", "IOException");
            if (Key(Read(handle)) != Key(expected)) throw new SetupFailure("parent_repair_verify", "policy");
        }
    }
}
