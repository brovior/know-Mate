using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Security.AccessControl;
using System.Security.Principal;
using Microsoft.Win32;

namespace AegisDesk.PptSetup
{
    internal static partial class Mutation
    {
        static partial void TraceMutation(string stage);
        /// <summary>Return the unescaped file URL expected by .NET COM registration.</summary>
        public static string RegistrationCodeBase(string path)
        {
            // RegAsm uses GetCodeBase(false). AbsoluteUri encodes spaces as %20,
            // which can make native COM activation fail with 0x80070002.
            return new Uri(path).GetComponents(UriComponents.AbsoluteUri, UriFormat.Unescaped);
        }

        public static void Protect(string directory)
        {
            ChildDiagnostics.Mark("install_root_create",ChildDiagnostics.Target(directory));TraceMutation("install_root_create");
            Machine.NoLinks(directory);
            string parent = Path.GetDirectoryName(directory);
            if (!Directory.Exists(parent) || String.Equals(directory, Machine.Root, StringComparison.OrdinalIgnoreCase)) Protect(parent);
            var acl = new DirectorySecurity();
            acl.SetAccessRuleProtection(true, false);
            acl.SetOwner(new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null));
            foreach (WellKnownSidType sid in new[] { WellKnownSidType.BuiltinAdministratorsSid, WellKnownSidType.LocalSystemSid })
                acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(sid, null), FileSystemRights.FullControl,
                    InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None, AccessControlType.Allow));
            acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(WellKnownSidType.BuiltinUsersSid, null), FileSystemRights.ReadAndExecute,
                InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None, AccessControlType.Allow));
            ChildDiagnostics.Mark("install_root_create",ChildDiagnostics.Target(directory));TraceMutation("install_root_create");
            RequestDirectory.Create(directory, acl);
            ChildDiagnostics.Mark("install_root_policy",ChildDiagnostics.Target(directory));TraceMutation("install_root_policy");
            Machine.NoLinks(directory);
            DirectorySecurity existing = Directory.GetAccessControl(directory);
            var owner = (SecurityIdentifier)existing.GetOwner(typeof(SecurityIdentifier));
            if (!owner.IsWellKnown(WellKnownSidType.BuiltinAdministratorsSid) && !owner.IsWellKnown(WellKnownSidType.LocalSystemSid))
                { ChildDiagnostics.Policy("root_owner_untrusted");var failed=new IOException("acl_unavailable");ChildDiagnostics.Attach(failed);throw failed; }
            foreach (FileSystemAccessRule rule in existing.GetAccessRules(true, true, typeof(SecurityIdentifier)))
            {
                var sid = (SecurityIdentifier)rule.IdentityReference;
                if (rule.AccessControlType == AccessControlType.Allow && !sid.IsWellKnown(WellKnownSidType.BuiltinAdministratorsSid) &&
                    !sid.IsWellKnown(WellKnownSidType.LocalSystemSid) &&
                    (rule.FileSystemRights & (FileSystemRights.Write | FileSystemRights.Delete | FileSystemRights.DeleteSubdirectoriesAndFiles |
                        FileSystemRights.ChangePermissions | FileSystemRights.TakeOwnership)) != 0) {ChildDiagnostics.Policy("root_write_untrusted");var failed=new IOException("acl_unavailable");ChildDiagnostics.Attach(failed);throw failed;}
            }
        }
        public static bool RegistrationValid(RegistryKey root, string version)
        {
            string path = Machine.InstalledDll(version);
            Machine.NoLinks(path);
            if (!File.Exists(path) || !Protected(path) || AssemblyName.GetAssemblyName(path).Version.ToString() != version) return false;
            var record = Contract.Read(Machine.Record);
            if (Contract.Hash(path) != (string)record["sha256"]) return false;
            if (new Version(version) > new Version("1.0.4.0"))
            {
                if (!Owned()) return false;
                var files = (Dictionary<string, object>)record["files"];
                foreach (string name in Contract.InstalledFiles)
                {
                    string installed = Path.Combine(Path.GetDirectoryName(path), name);
                    Machine.NoLinks(installed);
                    if (!File.Exists(installed) || !Protected(installed) || Contract.Hash(installed) != (string)files[name]) return false;
                    AssemblyName identity = AssemblyName.GetAssemblyName(installed);
                    if (identity.Name != Path.GetFileNameWithoutExtension(name) || identity.Version.ToString() != version) return false;
                }
            }
            using (RegistryKey server = root.OpenSubKey(Contract.Keys[0] + @"\InprocServer32"))
            using (RegistryKey versionKey = root.OpenSubKey(Contract.Keys[0] + @"\InprocServer32\" + version))
            using (RegistryKey prog = root.OpenSubKey(Contract.Keys[1] + @"\CLSID"))
            {
                string assembly = AssemblyName.GetAssemblyName(path).FullName;
                return server != null && versionKey != null && prog != null &&
                    String.Equals(prog.GetValue("") as string, Contract.ClassId, StringComparison.OrdinalIgnoreCase) &&
                    Object.Equals(server.GetValue(""), "mscoree.dll") && Object.Equals(server.GetValue("Class"), Contract.ReaderClass) &&
                    Object.Equals(server.GetValue("CodeBase"), RegistrationCodeBase(path)) &&
                    Object.Equals(server.GetValue("Assembly"), assembly) && Object.Equals(server.GetValue("RuntimeVersion"), "v4.0.30319") &&
                    Object.Equals(server.GetValue("ThreadingModel"), "Both") &&
                    Object.Equals(versionKey.GetValue("Assembly"), assembly) && Object.Equals(versionKey.GetValue("Class"), Contract.ReaderClass) &&
                    Object.Equals(versionKey.GetValue("RuntimeVersion"), "v4.0.30319") &&
                    Object.Equals(versionKey.GetValue("CodeBase"), RegistrationCodeBase(path));
            }
        }
        private static FileSecurity FileAcl()
        {
            var acl = new FileSecurity();
            acl.SetAccessRuleProtection(true, false);
            acl.SetOwner(new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null));
            foreach (WellKnownSidType sid in new[] { WellKnownSidType.BuiltinAdministratorsSid, WellKnownSidType.LocalSystemSid })
                acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(sid, null), FileSystemRights.FullControl, AccessControlType.Allow));
            acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(WellKnownSidType.BuiltinUsersSid, null), FileSystemRights.ReadAndExecute, AccessControlType.Allow));
            return acl;
        }
        public static bool Protected(string path)
        {
            Machine.NoLinks(path);
            FileSecurity acl = File.GetAccessControl(path);
            var owner = (SecurityIdentifier)acl.GetOwner(typeof(SecurityIdentifier));
            if (!owner.IsWellKnown(WellKnownSidType.BuiltinAdministratorsSid) && !owner.IsWellKnown(WellKnownSidType.LocalSystemSid)) return false;
            foreach (FileSystemAccessRule rule in acl.GetAccessRules(true, true, typeof(SecurityIdentifier)))
            {
                var sid = (SecurityIdentifier)rule.IdentityReference;
                if (rule.AccessControlType == AccessControlType.Allow && !sid.IsWellKnown(WellKnownSidType.BuiltinAdministratorsSid) &&
                    !sid.IsWellKnown(WellKnownSidType.LocalSystemSid) &&
                    (rule.FileSystemRights & (FileSystemRights.Write | FileSystemRights.Delete | FileSystemRights.ChangePermissions | FileSystemRights.TakeOwnership)) != 0) return false;
            }
            return true;
        }
        private static object Snapshot(RegistryKey key)
        {
            if (key == null) return null;
            var values = new Dictionary<string, object>();
            foreach (string name in key.GetValueNames())
            {
                RegistryValueKind kind = key.GetValueKind(name);
                object value = key.GetValue(name, null, RegistryValueOptions.DoNotExpandEnvironmentNames);
                if (kind == RegistryValueKind.Binary || kind == RegistryValueKind.None) value = Convert.ToBase64String((byte[])value);
                if (kind == RegistryValueKind.QWord) value = ((long)value).ToString(System.Globalization.CultureInfo.InvariantCulture);
                values[name] = new Dictionary<string, object> { { "kind", (int)kind }, { "value", value } };
            }
            var children = new Dictionary<string, object>();
            foreach (string name in key.GetSubKeyNames()) using (RegistryKey child = key.OpenSubKey(name)) children[name] = Snapshot(child);
            return new Dictionary<string, object> { { "values", values }, { "children", children } };
        }
        private static void RestoreKey(RegistryKey root, string path, object snapshot)
        {
            root.DeleteSubKeyTree(path, false);
            if (snapshot == null) return;
            var data = (Dictionary<string, object>)snapshot;
            using (RegistryKey key = root.CreateSubKey(path))
                foreach (var item in (Dictionary<string, object>)data["values"])
                {
                    var entry = (Dictionary<string, object>)item.Value;
                    var kind = (RegistryValueKind)Convert.ToInt32(entry["kind"]);
                    object value = entry["value"];
                    if (kind == RegistryValueKind.Binary || kind == RegistryValueKind.None) value = Convert.FromBase64String((string)value);
                    if (kind == RegistryValueKind.QWord) value = Int64.Parse((string)value, System.Globalization.CultureInfo.InvariantCulture);
                    if (kind == RegistryValueKind.MultiString)
                    {
                        var converted = new List<string>();
                        foreach (object text in (IEnumerable)value) converted.Add((string)text);
                        value = converted.ToArray();
                    }
                    key.SetValue(item.Key, value, kind);
                }
            foreach (var child in (Dictionary<string, object>)data["children"]) RestoreKey(root, path + "\\" + child.Key, child.Value);
        }
        public static void Recover()
        {
            bool rollback=ChildDiagnostics.HasPrimary;
            ChildDiagnostics.Mark(rollback?"install_rollback":"mutation_version_check","record");VersionBoundary();
            ChildDiagnostics.Mark(rollback?"install_rollback":"mutation_recovery","journal");
            Machine.NoLinks(Machine.Journal);
            if (!File.Exists(Machine.Journal)) return;
            if (!Protected(Machine.Journal)) throw new IOException("journal_unresolved");
            var data = ReadRecoveryJournal();
            int bitness = (int)data["bitness"];
            var snapshots = (Dictionary<string, object>)data["keys"];
            ChildDiagnostics.Mark(rollback?"install_rollback":"mutation_recovery","registry");
            using (RegistryKey root = RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, Machine.View(bitness)))
                foreach (string path in Contract.Keys) RestoreKey(root, path, snapshots[path]);
            ChildDiagnostics.Mark(rollback?"install_rollback":"mutation_recovery","record");
            if (data["record"] == null) { if (File.Exists(Machine.Record)) File.Delete(Machine.Record); }
            else Contract.AtomicWrite(Machine.Record, data["record"], FileAcl());
            ChildDiagnostics.Mark(rollback?"install_rollback":"mutation_recovery","journal");
            File.Delete(Machine.Journal);
        }
        private static void Journal(RegistryKey root, int bitness)
        {
            var keys = new Dictionary<string, object>();
            foreach (string path in Contract.Keys) using (RegistryKey key = root.OpenSubKey(path)) keys[path] = Snapshot(key);
            Contract.AtomicWrite(Machine.Journal, new Dictionary<string, object> {
                { "schema", 2 }, { "target_version", Contract.Version }, { "prog_id", Contract.ProgId }, { "bitness", bitness }, { "keys", keys },
                { "record", File.Exists(Machine.Record) ? Contract.Read(Machine.Record) : null }
            }, FileAcl());
        }
        private static void Set(RegistryKey root, string path, string name, object value, RegistryValueKind kind = RegistryValueKind.String)
        { using (RegistryKey key = root.CreateSubKey(path)) key.SetValue(name, value, kind); }

        public static void Apply(bool remove, string own, RequestDirectoryLease lease)
        {
            ChildDiagnostics.Mark("mutation_office_check","none");
            if (!Machine.OfficeClosed()) throw new IOException("office_busy");
            ChildDiagnostics.Mark("mutation_reservations_check","request");
            if (!Machine.Admin || !Machine.ReservationsSafe(own)) {ChildDiagnostics.Policy("mutation_blocked");throw new IOException("mutation_blocked");}
            ChildDiagnostics.Mark("mutation_lease_check","request");lease.Recheck();
            ChildDiagnostics.Mark("mutation_version_check","record");VersionBoundary();
            ChildDiagnostics.Mark("mutation_recovery","journal");Recover();
            ChildDiagnostics.Mark("mutation_machine_check","none");
            Result state = Machine.Check("check", true, false, own);
            if (!state.machine_safe || state.office_bitness == null || state.status == "conflict" || (!remove && state.status == "disabled")) {ChildDiagnostics.Gate(state);throw new IOException("mutation_blocked");}
            ChildDiagnostics.Mark("mutation_ownership_check","record");TraceMutation("mutation_ownership_check");
            if (remove && !Owned()) {ChildDiagnostics.Policy("installation_unknown");throw new IOException("installation_unknown");}
            if (Owned() && Convert.ToInt32(Contract.Read(Machine.Record)["bitness"]) != state.office_bitness.Value) {ChildDiagnostics.Policy("installation_view_changed");throw new IOException("installation_view_changed");}
            Protect(Machine.Root);
            ChildDiagnostics.Mark("mutation_lease_check","request");lease.Recheck();
            ChildDiagnostics.Mark("install_registry","registry");TraceMutation("install_registry");
            using (RegistryKey root = RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, Machine.View(state.office_bitness.Value)))
            {
                // Expose the journal before any own registry mutation; every
                // interrupted stage is restored under the next mutation lock.
                ChildDiagnostics.Mark("mutation_lease_check","request");lease.Recheck();
                ChildDiagnostics.Mark("install_journal","journal");TraceMutation("install_journal");Journal(root, state.office_bitness.Value);
                try
                {
                    ChildDiagnostics.Mark("mutation_office_check","none");
                    if (!Machine.OfficeClosed()) throw new IOException("office_busy");
                    ChildDiagnostics.Mark("mutation_lease_check","request");lease.Recheck();
                    if (remove)
                    {
                        ChildDiagnostics.Mark("install_registry","registry");TraceMutation("install_registry");
                        foreach (string key in Contract.Keys) root.DeleteSubKeyTree(key, false);
                        ChildDiagnostics.Mark("install_record","record");TraceMutation("install_record");File.Delete(Machine.Record);
                    }
                    else
                    {
                        ChildDiagnostics.Mark("child_payload_check","payload");
                        if (!Contract.PayloadValid()) throw new IOException("payload_invalid");
                        string path = Machine.InstalledDll(Contract.Version);
                        Protect(Path.GetDirectoryName(path));
                        var hashes = new Dictionary<string, object>();
                        foreach (string name in Contract.InstalledFiles)
                        {
                            ChildDiagnostics.Mark("install_payload_copy","payload");TraceMutation("install_payload_copy");
                            string source = Path.Combine(Contract.PayloadDirectory, name);
                            string target = Path.Combine(Path.GetDirectoryName(path), name);
                            Machine.NoLinks(source); Machine.NoLinks(target);
                            string hash = Contract.Hash(source);
                            if (File.Exists(target)) { if (Contract.Hash(target) != hash) throw new IOException("same_version_changed"); }
                            else File.Copy(source, target, false);
                            ChildDiagnostics.Mark("install_file_acl","payload");TraceMutation("install_file_acl");File.SetAccessControl(target, FileAcl());
                            ChildDiagnostics.Mark("install_verify","payload");TraceMutation("install_verify");
                            AssemblyName identity = AssemblyName.GetAssemblyName(target);
                            if (Contract.Hash(target) != hash || identity.Name != Path.GetFileNameWithoutExtension(name) ||
                                identity.Version.ToString() != Contract.Version) throw new IOException("copy_invalid");
                            hashes[name] = hash;
                        }
                        ChildDiagnostics.Mark("install_registry","registry");TraceMutation("install_registry");
                        string server = Contract.Keys[0] + @"\InprocServer32";
                        string assembly = AssemblyName.GetAssemblyName(path).FullName;
                        Set(root, Contract.Keys[0], "", "Aegis Desk PPT Reader");
                        Set(root, server, "", "mscoree.dll"); Set(root, server, "ThreadingModel", "Both");
                        foreach (string key in new[] { server, server + "\\" + Contract.Version })
                        { Set(root, key, "Class", Contract.ReaderClass); Set(root, key, "Assembly", assembly); Set(root, key, "RuntimeVersion", "v4.0.30319"); Set(root, key, "CodeBase", RegistrationCodeBase(path)); }
                        Set(root, Contract.Keys[0] + @"\ProgId", "", Contract.ProgId);
                        Set(root, Contract.Keys[1], "", "Aegis Desk PPT Reader");
                        Set(root, Contract.Keys[1] + @"\CLSID", "", Contract.ClassId);
                        // Addins is deliberately written last. Never rewrite an
                        // existing disabled key or Office resiliency records.
                        Set(root, Contract.Keys[2], "FriendlyName", "Aegis Desk PPT Reader");
                        Set(root, Contract.Keys[2], "Description", "In-process PowerPoint reader");
                        Set(root, Contract.Keys[2], "LoadBehavior", 3, RegistryValueKind.DWord);
                        ChildDiagnostics.Mark("install_record","record");TraceMutation("install_record");
                        Contract.AtomicWrite(Machine.Record, new Dictionary<string, object> { { "schema", 2 }, { "prog_id", Contract.ProgId },
                            { "class_id", Contract.ClassId }, { "scope", "Machine" }, { "bitness", state.office_bitness.Value },
                            { "version", Contract.Version }, { "path", path }, { "sha256", hashes[Contract.Dll] }, { "files", hashes } }, FileAcl());
                        ChildDiagnostics.Mark("install_verify","registry");TraceMutation("install_verify");
                        if (!RegistrationValid(root, Contract.Version)) throw new IOException("registration_invalid");
                    }
                    ChildDiagnostics.Mark("install_journal","journal");TraceMutation("install_journal");File.Delete(Machine.Journal);
                    // Retain versioned payload files, including old versions.
                    // Registration removal is safe; file cleanup needs a separate
                    // verified file-use procedure and never overwrites a DLL.
                }
                catch (Exception error)
                {
                    ChildDiagnostics.Capture(error);ChildDiagnostics.Mark("install_rollback","journal");TraceMutation("install_rollback");
                    try {Recover();}catch(Exception rollback){ChildDiagnostics.CaptureSecondary(rollback);throw;}
                    throw;
                }
            }
        }
    }
}
