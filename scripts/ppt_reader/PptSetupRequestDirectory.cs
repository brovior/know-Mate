using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;

namespace AegisDesk.PptSetup
{
    // Only these two fixed directories are created, with their ACL installed by
    // the same native atomic operation. Existing empty directories are never
    // evidence of ownership and their permissions are never rewritten.
    internal static class RequestDirectory
    {
        [StructLayout(LayoutKind.Sequential)]
        private struct SecurityAttributes
        { public int length; public IntPtr descriptor; [MarshalAs(UnmanagedType.Bool)] public bool inherit; }
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CreateDirectory(string path, ref SecurityAttributes attributes);

        internal static DirectorySecurity Expected(bool parent)
        {
            var acl = new DirectorySecurity();
            acl.SetAccessRuleProtection(true, false);
            foreach (WellKnownSidType sid in new[] { WellKnownSidType.BuiltinAdministratorsSid, WellKnownSidType.LocalSystemSid })
                acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(sid, null), FileSystemRights.FullControl,
                    InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None, AccessControlType.Allow));
            var users = new SecurityIdentifier(WellKnownSidType.BuiltinUsersSid, null);
            acl.AddAccessRule(new FileSystemAccessRule(users, FileSystemRights.ReadAndExecute |
                (parent ? FileSystemRights.CreateDirectories : FileSystemRights.CreateFiles),
                InheritanceFlags.None, PropagationFlags.None, AccessControlType.Allow));
            acl.AddAccessRule(new FileSystemAccessRule(users, FileSystemRights.Read,
                InheritanceFlags.ObjectInherit, PropagationFlags.InheritOnly, AccessControlType.Allow));
            acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(WellKnownSidType.CreatorOwnerSid, null), FileSystemRights.FullControl,
                InheritanceFlags.ObjectInherit, PropagationFlags.InheritOnly, AccessControlType.Allow));
            return acl;
        }

        private static HashSet<string> Rules(DirectorySecurity acl)
        {
            var rules = new HashSet<string>(StringComparer.Ordinal);
            foreach (FileSystemAccessRule rule in acl.GetAccessRules(true, true, typeof(SecurityIdentifier)))
                rules.Add(rule.IdentityReference.Value + ":" + (int)rule.FileSystemRights + ":" +
                    (int)rule.AccessControlType + ":" + (int)rule.InheritanceFlags + ":" + (int)rule.PropagationFlags);
            return rules;
        }

        internal static bool Compatible(DirectorySecurity actual, bool parent)
        {
            var owner = actual.GetOwner(typeof(SecurityIdentifier)) as SecurityIdentifier;
            if (owner == null || (!owner.IsAccountSid() &&
                !owner.IsWellKnown(WellKnownSidType.BuiltinAdministratorsSid) &&
                !owner.IsWellKnown(WellKnownSidType.LocalSystemSid))) return false;
            if (!actual.AreAccessRulesCanonical) return false;
            var raw = new RawSecurityDescriptor(actual.GetSecurityDescriptorBinaryForm(), 0);
            if (raw.DiscretionaryAcl == null) return false;
            foreach (GenericAce entry in raw.DiscretionaryAcl)
            {
                var ace = entry as CommonAce;
                // Conditional/object/unknown ACEs cannot be safely interpreted
                // by this deliberately narrow compatibility policy.
                if (ace == null || ace.IsCallback ||
                    (ace.AceQualifier != AceQualifier.AccessAllowed && ace.AceQualifier != AceQualifier.AccessDenied)) return false;
                if (!parent || ace.AceQualifier != AceQualifier.AccessAllowed ||
                    (ace.AceFlags & AceFlags.InheritOnly) != 0) continue;
                SecurityIdentifier sid = ace.SecurityIdentifier;
                if (sid == owner || sid.IsWellKnown(WellKnownSidType.BuiltinAdministratorsSid) ||
                    sid.IsWellKnown(WellKnownSidType.LocalSystemSid)) continue;
                const FileSystemRights dangerous = FileSystemRights.WriteData | FileSystemRights.WriteAttributes |
                    FileSystemRights.WriteExtendedAttributes | FileSystemRights.Delete | FileSystemRights.DeleteSubdirectoriesAndFiles |
                    FileSystemRights.ChangePermissions | FileSystemRights.TakeOwnership;
                if ((((FileSystemRights)ace.AccessMask) & dangerous) != 0) return false;
                // Generic rights are not normalized by every native descriptor.
                if ((ace.AccessMask & unchecked((int)0x50000000)) != 0) return false;
            }
            // Existing inherited parents are accepted only by the policy above.
            // Child permissions remain the original exact protected contract.
            // An account owner retains implicit WRITE_DAC: this trusts that owner.
            return parent || (actual.AreAccessRulesProtected && Rules(actual).SetEquals(Rules(Expected(false))));
        }

        internal static bool Create(string path, DirectorySecurity acl, bool nativeErrors = false)
        {
            Machine.NoLinks(path);
            byte[] bytes = acl.GetSecurityDescriptorBinaryForm();
            GCHandle pinned = GCHandle.Alloc(bytes, GCHandleType.Pinned);
            try
            {
                var attributes = new SecurityAttributes { length = Marshal.SizeOf(typeof(SecurityAttributes)), descriptor = pinned.AddrOfPinnedObject() };
                bool created = CreateDirectory(path, ref attributes);
                if (!created)
                {
                    int code = Marshal.GetLastWin32Error();
                    if (code != 183)
                    {
                        if (nativeErrors) throw new Win32Exception(code);
                        ChildDiagnostics.NativeFailure(code);
                        var failure=new IOException("acl_unavailable");ChildDiagnostics.Attach(failure);throw failure;
                    }
                }
                return created;
            }
            finally { pinned.Free(); }
        }

    }
}
