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
            if (handle.IsInvalid) { int code = Marshal.GetLastWin32Error(); handle.Dispose(); throw SetupFailure.From(writable ? "parent_repair_write" : "parent_repair_check", new Win32Exception(code)); }
            return handle;
        }

        internal static string Inspect(SafeFileHandle handle, string path, bool directory)
        {
            FileIdentity identity;
            var actual = new StringBuilder(32768);
            if (!GetFileInformationByHandle(handle, out identity)) throw SetupFailure.From("parent_repair_verify", new Win32Exception(Marshal.GetLastWin32Error()));
            if ((identity.attributes & 0x400) != 0 ||
                ((identity.attributes & 0x10) != 0) != directory ||
                (!directory && identity.links != 1)) throw new SetupFailure("parent_repair_verify", "policy");
            uint length = GetFinalPathNameByHandle(handle, actual, (uint)actual.Capacity, 0);
            if (length == 0) throw SetupFailure.From("parent_repair_verify", new Win32Exception(Marshal.GetLastWin32Error()));
            if (length >= actual.Capacity ||
                !String.Equals(actual.ToString().TrimEnd('\\'), @"\\?\" + Path.GetFullPath(path).TrimEnd('\\'), StringComparison.OrdinalIgnoreCase))
                throw new SetupFailure("parent_repair_verify", "policy");
            var filesystem = new StringBuilder(32); uint serial, maximum, flags;
            if (!GetVolumeInformationByHandleW(handle, null, 0, out serial, out maximum, out flags, filesystem, (uint)filesystem.Capacity))
                throw SetupFailure.From("parent_repair_verify", new Win32Exception(Marshal.GetLastWin32Error()));
            if (filesystem.ToString() != "NTFS" || serial != identity.volume) throw new SetupFailure("parent_repair_verify", "policy");
            return identity.volume.ToString("X8") + ":" + identity.indexHigh.ToString("X8") + identity.indexLow.ToString("X8");
        }

        internal static DirectorySecurity Read(SafeFileHandle handle)
        {
            IntPtr owner, group, dacl, sacl, descriptor;
            uint code = GetSecurityInfo(handle, 1, 7, out owner, out group, out dacl, out sacl, out descriptor);
            if (code != 0) throw SetupFailure.From("parent_repair_verify", new Win32Exception((int)code));
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

        internal enum Classification { SafeAlreadyCompatible, Repairable, UnsupportedPolicy, AccessUnavailable, Changed }
        internal sealed class AceAnalysis
        {
            internal int Index, OriginalMask;
            internal AceFlags Flags;
            internal uint Expanded, Removable, Residual;
        }
        internal sealed class Analysis
        {
            internal Classification Kind;
            internal string Reason;
            internal readonly System.Collections.Generic.Dictionary<string, uint> Roles = new System.Collections.Generic.Dictionary<string, uint>(StringComparer.Ordinal);
            internal readonly System.Collections.Generic.List<AceAnalysis> Aces = new System.Collections.Generic.List<AceAnalysis>();
        }
        internal const uint Full = 0x1f01ff, ReadExecute = 0x1200a9, HistoricalWrite = 0x116, ParentAccess = 0x1200ad;

        internal static uint Expand(int mask)
        {
            uint value = unchecked((uint)mask), mapped = value & 0x0fffffffU;
            if ((value & 0x80000000U) != 0) mapped |= 0x120089U;
            if ((value & 0x40000000U) != 0) mapped |= 0x120116U;
            if ((value & 0x20000000U) != 0) mapped |= 0x1200a0U;
            if ((value & 0x10000000U) != 0) mapped |= Full;
            return mapped;
        }
        private static bool Subset(uint value, uint upper) { return (value & ~upper) == 0; }
        private static Analysis Reject(Analysis result, string reason)
        { result.Kind = Classification.UnsupportedPolicy; result.Reason = reason; return result; }

        private static bool PairedUsersWrite(RawAcl acl)
        {
            uint current = 0, parent = 0, descendants = 0;
            foreach (GenericAce entry in acl)
            {
                var ace = entry as CommonAce;
                if (ace == null || ace.IsCallback || ace.AceQualifier != AceQualifier.AccessAllowed || ace.SecurityIdentifier.Value != "S-1-5-32-545") continue;
                AceFlags scope = ace.AceFlags & ~AceFlags.Inherited;
                uint mask = Expand(ace.AccessMask);
                if (!Subset(mask, ReadExecute | HistoricalWrite)) continue;
                if (scope == AceFlags.None)
                {
                    if (Subset(mask, ParentAccess)) parent |= mask & HistoricalWrite;
                    else current |= mask & HistoricalWrite;
                }
                if (scope == (AceFlags.ContainerInherit | AceFlags.InheritOnly)) descendants |= mask & HistoricalWrite;
            }
            // 기존 부모 전용 CreateDirectories는 현재 쓰기 역할과 분리해 보존한다.
            return descendants != 0 && (current & ~descendants) == 0 && (descendants & ~(current | parent)) == 0;
        }

        internal static Analysis Analyze(DirectorySecurity acl, string helperSid)
        {
            var result = new Analysis();
            var raw = new RawSecurityDescriptor(acl.GetSecurityDescriptorBinaryForm(), 0);
            if (raw.Owner == null || (raw.Owner.Value != helperSid && raw.Owner.Value != "S-1-5-32-544" && raw.Owner.Value != "S-1-5-18"))
                return Reject(result, "unsafe_owner");
            if (!acl.AreAccessRulesCanonical || raw.DiscretionaryAcl == null) return Reject(result, "unsupported_ace");
            bool admin = false, system = false, removable = false;
            bool pairedUsersWrite = PairedUsersWrite(raw.DiscretionaryAcl);
            AceFlags both = AceFlags.ObjectInherit | AceFlags.ContainerInherit;
            for (int index = 0; index < raw.DiscretionaryAcl.Count; index++)
            {
                var ace = raw.DiscretionaryAcl[index] as CommonAce;
                if (ace == null || ace.IsCallback || ace.AceQualifier != AceQualifier.AccessAllowed)
                    return Reject(result, "unsupported_ace");
                AceFlags scope = ace.AceFlags & ~AceFlags.Inherited;
                if ((scope & ~(both | AceFlags.InheritOnly)) != 0 ||
                    (acl.AreAccessRulesProtected && (ace.AceFlags & AceFlags.Inherited) != 0))
                    return Reject(result, "unsupported_scope");
                uint expanded = Expand(ace.AccessMask), remove = 0;
                string sid = ace.SecurityIdentifier.Value;
                if (sid == raw.Owner.Value || sid == "S-1-5-32-544" || sid == "S-1-5-18")
                {
                    if (scope != AceFlags.None && scope != both && !(sid == raw.Owner.Value && scope == (both | AceFlags.InheritOnly)))
                        return Reject(result, "unsupported_scope");
                    if (!Subset(expanded, Full)) return Reject(result, "unsupported_mask");
                    admin |= sid == "S-1-5-32-544"; system |= sid == "S-1-5-18";
                }
                else if (sid == "S-1-3-0")
                {
                    if (scope != (both | AceFlags.InheritOnly)) return Reject(result, "unsupported_scope");
                    if (!Subset(expanded, Full)) return Reject(result, "unsupported_mask");
                }
                else if (sid == "S-1-5-32-545")
                {
                    if (scope == (AceFlags.ContainerInherit | AceFlags.InheritOnly))
                    {
                        if ((expanded & HistoricalWrite) != 0 && !pairedUsersWrite) return Reject(result, "unsupported_scope");
                        if (!Subset(expanded, ReadExecute | (pairedUsersWrite ? HistoricalWrite : 0U))) return Reject(result, "unknown_write");
                        remove = expanded & HistoricalWrite;
                    }
                    else
                    {
                        if (scope != AceFlags.None && scope != AceFlags.ContainerInherit && scope != both)
                            return Reject(result, "unsupported_scope");
                        uint upper = scope == AceFlags.None ? ParentAccess : scope == AceFlags.ContainerInherit ? ReadExecute | HistoricalWrite : ReadExecute;
                        bool pairedCurrent = scope == AceFlags.None && pairedUsersWrite && !Subset(expanded, ParentAccess);
                        if (pairedCurrent) upper = ReadExecute | HistoricalWrite;
                        if (!Subset(expanded, upper)) return Reject(result, "unknown_write");
                        if (scope == AceFlags.ContainerInherit || pairedCurrent) remove = expanded & HistoricalWrite;
                    }
                }
                else
                {
                    AceFlags inheritance = scope & both;
                    if ((scope & AceFlags.InheritOnly) != 0 && inheritance == 0) return Reject(result, "unsupported_scope");
                    if (!Subset(expanded, ReadExecute)) return Reject(result, "unknown_write");
                }
                string role = sid + ":" + ((int)scope).ToString();
                uint combined; result.Roles.TryGetValue(role, out combined); result.Roles[role] = combined | expanded;
                result.Aces.Add(new AceAnalysis { Index = index, OriginalMask = ace.AccessMask, Flags = ace.AceFlags,
                    Expanded = expanded, Removable = remove, Residual = expanded & ~remove });
                removable |= remove != 0;
            }
            if (!admin || !system) return Reject(result, "missing_trusted_principal");
            result.Kind = removable ? Classification.Repairable : RequestDirectory.Compatible(acl, true) ?
                Classification.SafeAlreadyCompatible : Classification.UnsupportedPolicy;
            if (result.Kind == Classification.UnsupportedPolicy) result.Reason = "required_access_missing";
            return result;
        }
        internal static bool Legacy(DirectorySecurity acl, string helperSid)
        { return Analyze(acl, helperSid).Kind == Classification.Repairable; }

        internal static void RequireRepairable(DirectorySecurity acl, string helperSid, string stage)
        {
            Analysis analysis = Analyze(acl, helperSid);
            if (analysis.Kind != Classification.Repairable)
                throw new SetupFailure(stage, "policy") { PolicyReason = analysis.Reason ?? "required_access_missing" };
        }

        internal static DirectorySecurity Transform(DirectorySecurity before, string helperSid, bool final)
        {
            RequireRepairable(before, helperSid, "parent_repair_check");
            Analysis analysis = Analyze(before, helperSid);
            var raw = new RawSecurityDescriptor(before.GetSecurityDescriptorBinaryForm(), 0);
            raw.SetFlags((raw.ControlFlags | ControlFlags.DiscretionaryAclProtected) &
                ~(ControlFlags.DiscretionaryAclAutoInheritRequired | ControlFlags.DiscretionaryAclAutoInherited));
            for (int index = raw.DiscretionaryAcl.Count - 1; index >= 0; index--)
            {
                var ace = (CommonAce)raw.DiscretionaryAcl[index]; ace.AceFlags &= ~AceFlags.Inherited;
                AceAnalysis item = analysis.Aces[index];
                if ((final && item.Removable != 0) || (unchecked((uint)ace.AccessMask) & 0xf0000000U) != 0)
                {
                    raw.DiscretionaryAcl.RemoveAce(index);
                    uint residual = final ? item.Residual : item.Expanded;
                    if (residual != 0) raw.DiscretionaryAcl.InsertAce(index,
                        new CommonAce(ace.AceFlags, AceQualifier.AccessAllowed, unchecked((int)residual), ace.SecurityIdentifier, false, null));
                }
            }
            var bytes = new byte[raw.BinaryLength]; raw.GetBinaryForm(bytes, 0);
            var result = new DirectorySecurity(); result.SetSecurityDescriptorBinaryForm(bytes);
            if (final)
                result.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier("S-1-5-32-545"),
                    FileSystemRights.ReadAndExecute | FileSystemRights.CreateDirectories, AccessControlType.Allow));
            if (final)
            {
                // 잔여 권한이 같은 ACE로 수렴하면 NTFS도 중복을 제거하므로 예상값을 고정한다.
                raw = new RawSecurityDescriptor(result.GetSecurityDescriptorBinaryForm(), 0);
                var seen = new System.Collections.Generic.HashSet<string>(StringComparer.Ordinal);
                for (int index = 0; index < raw.DiscretionaryAcl.Count; index++)
                {
                    var ace = (CommonAce)raw.DiscretionaryAcl[index];
                    if (ace.SecurityIdentifier.Value != "S-1-5-32-545" || ace.AceFlags != AceFlags.ContainerInherit) continue;
                    if (!seen.Add(ace.AccessMask.ToString())) raw.DiscretionaryAcl.RemoveAce(index--);
                }
                bytes = new byte[raw.BinaryLength]; raw.GetBinaryForm(bytes, 0);
                result = new DirectorySecurity(); result.SetSecurityDescriptorBinaryForm(bytes);
            }
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
