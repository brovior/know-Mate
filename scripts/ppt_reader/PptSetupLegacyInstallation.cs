using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using Microsoft.Win32;
using Microsoft.Win32.SafeHandles;

namespace AegisDesk.PptSetup
{
    /// <summary>고정 설치 경로와 구버전 1.0.0.0의 파일·등록을 다시 확인한다.</summary>
    internal sealed class LegacyInstallation : IDisposable
    {
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern SafeFileHandle CreateFile(string path, uint access, uint share, IntPtr attributes, uint disposition, uint flags, IntPtr template);
        private readonly List<SafeFileHandle> handles = new List<SafeFileHandle>();
        internal readonly int Bitness;
        private LegacyInstallation(int bitness) { Bitness = bitness; }

        private void Pin(string path, bool directory, bool platformAnchor = false, bool inheritedContainer = false)
        {
            Machine.NoLinks(path);
            SafeFileHandle handle = CreateFile(path, 0x00120081, 1, IntPtr.Zero, 3, directory ? 0x02200000U : 0x00200000U, IntPtr.Zero);
            if (handle.IsInvalid) { handle.Dispose(); throw new SetupFailure("parent_repair_check", "policy"); }
            handles.Add(handle);
            ParentSecurity.Inspect(handle, path, directory);
            if (platformAnchor) return;
            var acl = ParentSecurity.Read(handle);
            if (!CompatibleBoundary(acl, inheritedContainer)) throw new SetupFailure("parent_repair_check", "policy");
        }

        internal static bool CompatibleBoundary(DirectorySecurity acl, bool inheritedContainer)
        {
            var raw = new RawSecurityDescriptor(acl.GetSecurityDescriptorBinaryForm(), 0);
            if ((!inheritedContainer && !acl.AreAccessRulesProtected) || raw.Owner == null || (raw.Owner.Value != "S-1-5-32-544" && raw.Owner.Value != "S-1-5-18") ||
                raw.DiscretionaryAcl == null || !acl.AreAccessRulesCanonical)
                return false;
            foreach (GenericAce entry in raw.DiscretionaryAcl)
            {
                var ace = entry as CommonAce;
                if (ace == null || ace.IsCallback || ace.AceQualifier != AceQualifier.AccessAllowed)
                    return false;
                // 구버전 외곽 폴더가 Windows ProgramFiles에서 상속한 설치 서비스 권한만 허용한다.
                if (ace.SecurityIdentifier.Value == "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464")
                {
                    if (!inheritedContainer) return false;
                    if (ace.AceFlags == AceFlags.Inherited && ace.AccessMask == 0x1f01ff) continue;
                    if (ace.AceFlags == (AceFlags.ContainerInherit | AceFlags.InheritOnly | AceFlags.Inherited) && ace.AccessMask == 0x10000000) continue;
                    return false;
                }
                if (ace.SecurityIdentifier.Value == "S-1-3-0")
                {
                    if (inheritedContainer && ace.AceFlags == (AceFlags.ObjectInherit | AceFlags.ContainerInherit | AceFlags.InheritOnly | AceFlags.Inherited) &&
                        ace.AccessMask == 0x10000000) continue;
                    return false;
                }
                if (ace.SecurityIdentifier.Value != "S-1-5-32-544" && ace.SecurityIdentifier.Value != "S-1-5-18" &&
                    (ace.AccessMask & unchecked((int)0x500d0156)) != 0)
                    return false;
            }
            return true;
        }

        internal static LegacyInstallation Acquire(bool includeUser, string own)
        {
            if (!Contract.AutoInstallEnabled || !Contract.PayloadValid() || !Machine.OfficeClosed() || !Machine.ReservationsSafe(own))
                throw new SetupFailure("parent_repair_check", "policy");
            var state = Machine.Check("prepare", true, includeUser, own);
            if (!state.machine_safe || state.office_bitness == null || state.status == "disabled" || state.status == "conflict")
                throw new SetupFailure("parent_repair_check", "policy");
            var lease = new LegacyInstallation(state.office_bitness.Value);
            try
            {
                string programFiles = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles);
                lease.Pin(programFiles, true, true);
                lease.Pin(Path.Combine(programFiles, "AegisDesk"), true, false, true);
                lease.Pin(Machine.Root, true);
                lease.Pin(Path.Combine(Machine.Root, "1.0.0.0"), true);
                lease.Pin(Machine.Record, false);
                string path = Machine.InstalledDll("1.0.0.0"); lease.Pin(path, false);
                var record = Contract.Read(Machine.Record);
                if (record.Count != 8 || !Mutation.RecordIdentity(record) || (string)record["version"] != "1.0.0.0" ||
                    (int)record["schema"] != 1 || (int)record["bitness"] != lease.Bitness || Contract.Hash(path) != (string)record["sha256"])
                    throw new SetupFailure("parent_repair_check", "policy");
                AssemblyName identity = AssemblyName.GetAssemblyName(path);
                if (identity.Name != "AegisDesk.PptReader" || identity.Version.ToString() != "1.0.0.0")
                    throw new SetupFailure("parent_repair_check", "policy");
                using (var root = RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, Machine.View(lease.Bitness)))
                using (var server = root.OpenSubKey(Contract.Keys[0] + @"\InprocServer32"))
                using (var version = root.OpenSubKey(Contract.Keys[0] + @"\InprocServer32\1.0.0.0"))
                using (var prog = root.OpenSubKey(Contract.Keys[1] + @"\CLSID"))
                using (var clsid = root.OpenSubKey(Contract.Keys[0] + @"\ProgId"))
                using (var addin = root.OpenSubKey(Contract.Keys[2]))
                {
                    if (server == null || version == null || prog == null || clsid == null || addin == null ||
                        !Object.Equals(addin.GetValue("LoadBehavior"), 3) || !Object.Equals(prog.GetValue(""), Contract.ClassId) ||
                        !Object.Equals(clsid.GetValue(""), Contract.ProgId) || !Object.Equals(server.GetValue(""), "mscoree.dll") ||
                        !Object.Equals(server.GetValue("ThreadingModel"), "Both")) throw new SetupFailure("parent_repair_check", "policy");
                    foreach (var key in new[] { server, version })
                        if (!Object.Equals(key.GetValue("Assembly"), identity.FullName) || !Object.Equals(key.GetValue("Class"), Contract.ReaderClass) ||
                            !Object.Equals(key.GetValue("RuntimeVersion"), "v4.0.30319") ||
                            (!Object.Equals(key.GetValue("CodeBase"), new Uri(path).AbsoluteUri) &&
                             !Object.Equals(key.GetValue("CodeBase"), Mutation.RegistrationCodeBase(path))))
                            throw new SetupFailure("parent_repair_check", "policy");
                }
                return lease;
            }
            catch { lease.Dispose(); throw; }
        }

        public void Dispose()
        { for (int index = handles.Count - 1; index >= 0; index--) handles[index].Dispose(); handles.Clear(); }
    }
}
