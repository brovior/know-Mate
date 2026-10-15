using System;
using System.Collections.Generic;
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
            // A first ordinary-user creation is owned by that account. Later
            // users may reuse it only with exactly the protected shared ACL.
            if (owner == null || (!owner.IsAccountSid() &&
                !owner.IsWellKnown(WellKnownSidType.BuiltinAdministratorsSid) &&
                !owner.IsWellKnown(WellKnownSidType.LocalSystemSid))) return false;
            return actual.AreAccessRulesProtected && actual.AreAccessRulesCanonical && Rules(actual).SetEquals(Rules(Expected(parent)));
        }

        internal static bool Create(string path, DirectorySecurity acl)
        {
            Machine.NoLinks(path);
            byte[] bytes = acl.GetSecurityDescriptorBinaryForm();
            GCHandle pinned = GCHandle.Alloc(bytes, GCHandleType.Pinned);
            try
            {
                var attributes = new SecurityAttributes { length = Marshal.SizeOf(typeof(SecurityAttributes)), descriptor = pinned.AddrOfPinnedObject() };
                bool created = CreateDirectory(path, ref attributes);
                if (!created && Marshal.GetLastWin32Error() != 183) throw new IOException("acl_unavailable");
                return created;
            }
            finally { pinned.Free(); }
        }

        private static void Ensure(string path, bool parent)
        {
            Create(path, Expected(parent));
            Machine.NoLinks(path);
            if (!Directory.Exists(path) || !Compatible(Directory.GetAccessControl(path), parent)) throw new IOException("acl_unavailable");
        }

        internal static void Ensure()
        {
            Ensure(Path.GetDirectoryName(Machine.Requests), true);
            Ensure(Machine.Requests, false);
        }
    }
}
