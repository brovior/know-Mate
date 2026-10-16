using System;
using System.Collections.Generic;
using System.IO;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;

namespace AegisDesk.PptSetup
{
    internal enum RequestWriteMode { CreateOnly, ReplaceExisting }

    internal static partial class RequestWriter
    {
        internal static string Basename(string id)
        {
            Guid guid;
            if (!Guid.TryParseExact(id, "D", out guid) || guid.ToString("D") != id)
                throw new IOException("reservation_identity");
            return id + ".json";
        }
        internal static byte[] Encode(Dictionary<string, object> values, RequestWriteMode mode)
        {
            string id = values.ContainsKey("request_id") ? values["request_id"] as string : null;
            Basename(id);
            if (!Reservation.Valid(values, id) || (mode != RequestWriteMode.CreateOnly && mode != RequestWriteMode.ReplaceExisting))
                throw new IOException("reservation_identity");
            if (mode == RequestWriteMode.CreateOnly && ((string)values["state"] != "reserved" || values["child_pid"] != null ||
                (string)values["required_version"] != Contract.Version)) throw new IOException("reservation_identity");
            if (mode == RequestWriteMode.CreateOnly)
                using (WindowsIdentity identity = WindowsIdentity.GetCurrent())
                using (var current = System.Diagnostics.Process.GetCurrentProcess())
                    if (identity.User.Value != (string)values["helper_sid"] || current.Id != (int)values["helper_pid"] ||
                        current.StartTime.ToUniversalTime().Ticks.ToString() != (string)values["helper_creation_ticks"])
                        throw new IOException("reservation_identity");
            byte[] bytes = new UTF8Encoding(false, true).GetBytes(Contract.Json.Serialize(values));
            if (bytes.Length == 0 || bytes.Length > 8192) throw new IOException("reservation_size");
            return bytes;
        }
        internal static void ValidateTransition(Dictionary<string, object> old, Dictionary<string, object> next)
        {
            foreach (string key in new[] { "schema", "request_id", "required_version", "helper_sid", "helper_pid", "helper_creation_ticks" })
                if (!Object.Equals(old[key], next[key])) throw new IOException("reservation_identity");
            string before = (string)old["state"], after = (string)next["state"];
            if (after == "elevated")
            {
                if (before != "reserved" || old["child_pid"] != null || next["child_pid"] == null ||
                    (string)next["required_version"] != Contract.Version || !Machine.Admin)
                    throw new IOException("reservation_identity");
                using (var current = System.Diagnostics.Process.GetCurrentProcess())
                    if ((int)next["child_pid"] != current.Id || (string)next["child_creation_ticks"] != current.StartTime.ToUniversalTime().Ticks.ToString())
                        throw new IOException("reservation_identity");
                using (var helper = System.Diagnostics.Process.GetProcessById((int)old["helper_pid"]))
                    if (helper.HasExited || helper.StartTime.ToUniversalTime().Ticks.ToString() != (string)old["helper_creation_ticks"])
                        throw new IOException("reservation_identity");
                return;
            }
            if (after != "complete") throw new IOException("reservation_identity");
            if (!Object.Equals(old["child_pid"], next["child_pid"]) || !Object.Equals(old["child_creation_ticks"], next["child_creation_ticks"]))
                throw new IOException("reservation_identity");
            using (WindowsIdentity current = WindowsIdentity.GetCurrent())
            using (var process = System.Diagnostics.Process.GetCurrentProcess())
            {
                bool own = current.User.Value == (string)old["helper_sid"] && process.Id == (int)old["helper_pid"] &&
                    process.StartTime.ToUniversalTime().Ticks.ToString() == (string)old["helper_creation_ticks"];
                if (!own && (!Machine.Admin || !Machine.OfficeClosed() ||
                    !Reservation.ProcessGone((int)old["helper_pid"], (string)old["helper_creation_ticks"]) ||
                    (old["child_pid"] != null && !Reservation.ProcessGone((int)old["child_pid"], (string)old["child_creation_ticks"]))))
                    throw new IOException("reservation_unresolved");
                if (own && old["child_pid"] != null && !Reservation.ProcessGone((int)old["child_pid"], (string)old["child_creation_ticks"]))
                    throw new IOException("reservation_unresolved");
            }
        }
        internal static FileSecurity ExpectedSecurity(string helper, bool currentOwner)
        {
            var security = new FileSecurity();
            security.SetAccessRuleProtection(true, false);
            foreach (string sid in new[] { helper, "S-1-5-32-544", "S-1-5-18" })
                security.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(sid), FileSystemRights.FullControl, AccessControlType.Allow));
            security.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier("S-1-5-32-545"), FileSystemRights.Read, AccessControlType.Allow));
            if (currentOwner)
                using (WindowsIdentity identity = WindowsIdentity.GetCurrent())
                    security.SetOwner(identity.User.IsWellKnown(WellKnownSidType.LocalSystemSid) ? identity.User :
                        Machine.Admin ? new SecurityIdentifier("S-1-5-32-544") : identity.User);
            return security;
        }
        private static string[] AccessEntries(FileSecurity security)
        {
            var entries = new List<string>();
            var raw = new RawSecurityDescriptor(security.GetSecurityDescriptorBinaryForm(), 0);
            if (raw.DiscretionaryAcl == null) throw new IOException("request_security");
            foreach (GenericAce entry in raw.DiscretionaryAcl)
            {
                var ace = entry as CommonAce;
                if (ace == null || ace.IsCallback || ace.AceQualifier != AceQualifier.AccessAllowed || ace.AceFlags != AceFlags.None)
                    throw new IOException("request_security");
                entries.Add(ace.SecurityIdentifier.Value + ":" + ace.AccessMask);
            }
            entries.Sort(StringComparer.Ordinal); return entries.ToArray();
        }
        internal static void ValidateSecurity(FileSecurity security, string helper, FileSecurity expectedOwner)
        {
            var owner = security.GetOwner(typeof(SecurityIdentifier)) as SecurityIdentifier;
            if (owner == null || (owner.Value != helper && owner.Value != "S-1-5-32-544" && owner.Value != "S-1-5-18") ||
                !security.AreAccessRulesProtected || !security.AreAccessRulesCanonical ||
                String.Join("|", AccessEntries(security)) != String.Join("|", AccessEntries(ExpectedSecurity(helper, false))))
                throw new IOException("request_security");
            if (expectedOwner != null && !owner.Equals(expectedOwner.GetOwner(typeof(SecurityIdentifier)))) throw new IOException("request_owner");
        }
    }
}
