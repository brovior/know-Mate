using System;
using System.Collections.Generic;
using System.IO;
using System.Security.AccessControl;
using System.Security.Principal;

namespace AegisDesk.PptSetup
{
    /// <summary>신뢰한 설치 루트에 부모 DACL 변경의 감사 기록만 보존한다.</summary>
    internal static class RepairBackup
    {
        internal static string Root { get { return Path.Combine(Machine.Root, "repair-backups"); } }
        internal static string PathFor(string id) { return Path.Combine(Root, RequestWriter.Basename(id)); }
        internal static bool HasTemporary(string root, string id)
        {
            string prefix = RequestWriter.Basename(id) + ".";
            // Windows wildcard는 id.json.*에 id.json도 포함하므로 전체 이름으로 판정한다.
            foreach (string path in Directory.GetFileSystemEntries(root))
                if (Path.GetFileName(path).StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) return true;
            return false;
        }

        private static FileSecurity FileAcl()
        {
            var acl = new FileSecurity(); acl.SetAccessRuleProtection(true, false);
            acl.SetOwner(new SecurityIdentifier("S-1-5-32-544"));
            foreach (string sid in new[] { "S-1-5-32-544", "S-1-5-18" })
                acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(sid), FileSystemRights.FullControl, AccessControlType.Allow));
            acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier("S-1-5-32-545"), FileSystemRights.Read, AccessControlType.Allow));
            return acl;
        }

        internal static void ValidateFileSecurity(DirectorySecurity acl)
        {
            var raw = new RawSecurityDescriptor(acl.GetSecurityDescriptorBinaryForm(), 0);
            if (!acl.AreAccessRulesProtected || !acl.AreAccessRulesCanonical || raw.Owner == null || raw.DiscretionaryAcl == null ||
                (raw.Owner.Value != "S-1-5-32-544" && raw.Owner.Value != "S-1-5-18")) throw new SetupFailure("parent_repair_backup", "policy");
            var principals = new HashSet<string>();
            foreach (GenericAce entry in raw.DiscretionaryAcl)
            {
                var ace = entry as CommonAce;
                if (ace == null || ace.IsCallback || ace.AceQualifier != AceQualifier.AccessAllowed || ace.AceFlags != AceFlags.None)
                    throw new SetupFailure("parent_repair_backup", "policy");
                string sid = ace.SecurityIdentifier.Value;
                if ((sid == "S-1-5-32-544" || sid == "S-1-5-18") ? ace.AccessMask != 0x1f01ff : sid != "S-1-5-32-545" || ace.AccessMask != 0x120089)
                    throw new SetupFailure("parent_repair_backup", "policy");
                if (!principals.Add(sid)) throw new SetupFailure("parent_repair_backup", "policy");
            }
            if (principals.Count != 3) throw new SetupFailure("parent_repair_backup", "policy");
        }

        private static void ValidateFile(string path)
        {
            using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
            {
                ParentSecurity.Inspect(stream.SafeFileHandle, path, false);
                ValidateFileSecurity(ParentSecurity.Read(stream.SafeFileHandle));
            }
        }

        internal static bool Absent(string id)
        {
            Machine.NoLinks(Root);
            if (!Directory.Exists(Root)) return !File.Exists(Root);
            using (var pin = ParentSecurity.OpenSecurity(Root, false))
            {
                ParentSecurity.Inspect(pin, Root, true);
                ValidateDirectory(ParentSecurity.Read(pin));
                return !File.Exists(PathFor(id)) && !Directory.Exists(PathFor(id)) &&
                    !HasTemporary(Root, id);
            }
        }

        private static void ValidateDirectory(DirectorySecurity acl)
        {
            var owner = acl.GetOwner(typeof(SecurityIdentifier));
            if (!acl.AreAccessRulesProtected || !acl.AreAccessRulesCanonical ||
                (owner.Value != "S-1-5-32-544" && owner.Value != "S-1-5-18")) throw new SetupFailure("parent_repair_backup", "policy");
            var raw = new RawSecurityDescriptor(acl.GetSecurityDescriptorBinaryForm(), 0);
            var principals = new HashSet<string>();
            if (raw.DiscretionaryAcl == null) throw new SetupFailure("parent_repair_backup", "policy");
            foreach (GenericAce entry in raw.DiscretionaryAcl)
            {
                var ace = entry as CommonAce;
                if (ace == null || ace.IsCallback || ace.AceQualifier != AceQualifier.AccessAllowed ||
                    ace.AceFlags != (AceFlags.ObjectInherit | AceFlags.ContainerInherit)) throw new SetupFailure("parent_repair_backup", "policy");
                string sid = ace.SecurityIdentifier.Value;
                if ((sid == "S-1-5-32-544" || sid == "S-1-5-18") ? ace.AccessMask != 0x1f01ff : sid != "S-1-5-32-545" || ace.AccessMask != 0x1200a9)
                    throw new SetupFailure("parent_repair_backup", "policy");
                if (!principals.Add(sid)) throw new SetupFailure("parent_repair_backup", "policy");
            }
            if (principals.Count != 3) throw new SetupFailure("parent_repair_backup", "policy");
        }

        internal static Dictionary<string, object> Create(Reservation request, string parentIdentity, DirectorySecurity before)
        {
            if (!Machine.Admin || !Absent(request.request_id)) throw new SetupFailure("parent_repair_backup", "policy");
            Mutation.Protect(Root);
            var data = new Dictionary<string, object> {
                { "schema", 1 }, { "request_id", request.request_id }, { "required_version", Contract.Version },
                { "helper_sid", request.helper_sid }, { "helper_pid", request.helper_pid }, { "helper_creation_ticks", request.helper_creation_ticks },
                { "child_pid", request.child_pid }, { "child_creation_ticks", request.child_creation_ticks }, { "parent_identity", parentIdentity },
                { "payload_hash", Contract.Hash(System.Reflection.Assembly.GetExecutingAssembly().Location) },
                { "before", ParentSecurity.Key(before) },
                { "intermediate", ParentSecurity.Key(ParentSecurity.Transform(before, request.helper_sid, false)) },
                { "final", ParentSecurity.Key(ParentSecurity.Transform(before, request.helper_sid, true)) }, { "stage", "prepared" }
            };
            Save(data, true); return data;
        }

        internal static void Save(Dictionary<string, object> data, bool create = false)
        {
            if (!Machine.Admin) throw new SetupFailure("parent_repair_backup", "policy");
            string id = (string)data["request_id"], path = PathFor(id);
            using (var pin = ParentSecurity.OpenSecurity(Root, false))
            {
                ParentSecurity.Inspect(pin, Root, true); ValidateDirectory(ParentSecurity.Read(pin));
                if (HasTemporary(Root, id) ||
                    (create ? File.Exists(path) || Directory.Exists(path) : !File.Exists(path)))
                    throw new SetupFailure("parent_repair_backup", "policy");
                if (!create) ValidateFile(path);
                Contract.AtomicWrite(path, data, FileAcl());
                ValidateFile(path);
            }
        }

        internal static Dictionary<string, object> Read(Reservation request)
        {
            string id = request.request_id, path = PathFor(id);
            using (var pin = ParentSecurity.OpenSecurity(Root, false))
            {
                ParentSecurity.Inspect(pin, Root, true); ValidateDirectory(ParentSecurity.Read(pin));
                Machine.NoLinks(path);
                if (HasTemporary(Root, id) || !File.Exists(path))
                    throw new SetupFailure("parent_repair_backup", "policy");
                using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
                {
                    ParentSecurity.Inspect(stream.SafeFileHandle, path, false);
                    ValidateFileSecurity(ParentSecurity.Read(stream.SafeFileHandle));
                    var data = Contract.Read(path);
                    if (data.Count != 14 || !Object.Equals(data["schema"], 1) || (string)data["request_id"] != id ||
                        (string)data["required_version"] != Contract.Version || (string)data["helper_sid"] != request.helper_sid ||
                        !Object.Equals(data["helper_pid"], request.helper_pid) || (string)data["helper_creation_ticks"] != request.helper_creation_ticks ||
                        !Object.Equals(data["child_pid"], request.child_pid) || (string)data["child_creation_ticks"] != request.child_creation_ticks ||
                        (string)data["payload_hash"] != Contract.Hash(System.Reflection.Assembly.GetExecutingAssembly().Location))
                        throw new SetupFailure("parent_repair_backup", "policy");
                    string stage = (string)data["stage"];
                    if (stage != "prepared" && stage != "protected" && stage != "child_verified") throw new SetupFailure("parent_repair_backup", "policy");
                    var before = new DirectorySecurity(); before.SetSecurityDescriptorSddlForm((string)data["before"]);
                    if (!ParentSecurity.Legacy(before, request.helper_sid) ||
                        (string)data["intermediate"] != ParentSecurity.Key(ParentSecurity.Transform(before, request.helper_sid, false)) ||
                        (string)data["final"] != ParentSecurity.Key(ParentSecurity.Transform(before, request.helper_sid, true)))
                        throw new SetupFailure("parent_repair_backup", "policy");
                    return data;
                }
            }
        }
    }
}
