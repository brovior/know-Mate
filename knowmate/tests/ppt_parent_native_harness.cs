using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using Microsoft.Win32.SafeHandles;
using AegisDesk.PptSetup;

internal static class ParentNativeHarness
{
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateFile(string path, uint access, uint share, IntPtr sa, uint disposition, uint flags, IntPtr template);

    private static void Require(bool value, string name)
    { if (!value) throw new Exception(name); }

    private static string Security(string path)
    {
        var acl = File.Exists(path) ? (FileSystemSecurity)File.GetAccessControl(path) : Directory.GetAccessControl(path);
        return acl.GetSecurityDescriptorSddlForm(AccessControlSections.Owner | AccessControlSections.Group | AccessControlSections.Access);
    }

    private static Dictionary<string, string> Snapshot(string root)
    {
        var result = new Dictionary<string, string>();
        foreach (string path in Directory.GetFileSystemEntries(root, "*", SearchOption.AllDirectories))
            result[path] = Security(path) + (File.Exists(path) ? ":" + Contract.Hash(path) : "");
        return result;
    }

    private static void Preserve(Dictionary<string, string> before)
    {
        foreach (var entry in before)
            Require(Security(entry.Key) + (File.Exists(entry.Key) ? ":" + Contract.Hash(entry.Key) : "") == entry.Value, "descendant_changed");
    }

    private static string Fixture(string anchor)
    {
        var acl = new DirectorySecurity();
        acl.SetOwner(WindowsIdentity.GetCurrent().User); acl.SetAccessRuleProtection(true, false);
        foreach (string sid in new[] { WindowsIdentity.GetCurrent().User.Value, "S-1-5-32-544", "S-1-5-18" })
            acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(sid), FileSystemRights.FullControl,
                InheritanceFlags.ObjectInherit | InheritanceFlags.ContainerInherit, PropagationFlags.None, AccessControlType.Allow));
        acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier("S-1-3-0"), FileSystemRights.FullControl,
            InheritanceFlags.ObjectInherit | InheritanceFlags.ContainerInherit, PropagationFlags.InheritOnly, AccessControlType.Allow));
        acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier("S-1-5-32-545"), FileSystemRights.ReadAndExecute,
            InheritanceFlags.ObjectInherit | InheritanceFlags.ContainerInherit, PropagationFlags.None, AccessControlType.Allow));
        acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier("S-1-5-32-545"), FileSystemRights.Write,
            InheritanceFlags.ContainerInherit, PropagationFlags.None, AccessControlType.Allow));
        RequestDirectory.Create(anchor, acl, true);
        string parent = Path.Combine(anchor, "AegisDesk"); Directory.CreateDirectory(parent);
        var inherited = new DirectorySecurity(); inherited.SetOwner(WindowsIdentity.GetCurrent().User);
        inherited.SetAccessRuleProtection(false, false); Directory.SetAccessControl(parent, inherited);
        Require(!Directory.GetAccessControl(parent).AreAccessRulesProtected, "inherited_fixture");
        return parent;
    }

    private static void Run(string anchor, int race)
    {
        string parent = Fixture(anchor), sid = WindowsIdentity.GetCurrent().User.Value;
        var before = Directory.GetAccessControl(parent);
        Require(!RequestDirectory.Compatible(before, true), "ordinary_policy_must_reject");
        Require(ParentSecurity.Legacy(before, sid), "legacy_policy_must_accept");
        string plain = Path.Combine(parent, "plain"); Directory.CreateDirectory(Path.Combine(plain, "nested"));
        string protectedChild = Path.Combine(parent, "PptReaderRequests"); RequestDirectory.Create(protectedChild, RequestDirectory.Expected(false), true);
        File.WriteAllText(Path.Combine(plain, "marker.txt"), "synthetic-parent-native-marker");
        File.WriteAllText(Path.Combine(plain, "nested", "marker.txt"), "synthetic-parent-native-marker");
        File.WriteAllText(Path.Combine(protectedChild, "marker.txt"), "synthetic-parent-native-marker");
        var preserved = Snapshot(parent);
        using (var pin = CreateFile(parent, 0x00120081, 1, IntPtr.Zero, 3, 0x02200000, IntPtr.Zero))
        using (var writable = ParentSecurity.OpenSecurity(parent, true))
        {
            Require(!pin.IsInvalid, "namespace_pin");
            using (var denied = CreateFile(parent, 0x40000000, 7, IntPtr.Zero, 3, 0x02200000, IntPtr.Zero))
                Require(denied.IsInvalid, "write_handle_must_conflict");
            using (var denied = CreateFile(parent, 0x00010000, 7, IntPtr.Zero, 3, 0x02200000, IntPtr.Zero))
                Require(denied.IsInvalid, "delete_handle_must_conflict");
            if (race == 0 || race == 1) Inject(parent, preserved);
            ParentSecurity.Write(writable, ParentSecurity.Transform(before, sid, false)); Preserve(preserved);
            if (race == 2) Inject(parent, preserved);
            ParentSecurity.Write(writable, ParentSecurity.Transform(before, sid, true)); Preserve(preserved);
            var after = ParentSecurity.Read(writable);
            Require(after.AreAccessRulesProtected && RequestDirectory.Compatible(after, true), "final_policy");
            Require(before.GetOwner(typeof(SecurityIdentifier)).Equals(after.GetOwner(typeof(SecurityIdentifier))), "owner_unchanged");
            Require(before.GetGroup(typeof(SecurityIdentifier)).Equals(after.GetGroup(typeof(SecurityIdentifier))), "group_unchanged");
        }
        using (var existing = CreateFile(parent, 0x40000000, 7, IntPtr.Zero, 3, 0x02200000, IntPtr.Zero))
        using (var denied = CreateFile(parent, 0x00120081, 1, IntPtr.Zero, 3, 0x02200000, IntPtr.Zero))
            Require(!existing.IsInvalid && denied.IsInvalid, "preexisting_write_must_conflict");
        using (var readonlyHandle = ParentSecurity.OpenSecurity(parent, false))
        {
            bool rejected = false;
            try { ParentSecurity.Write(readonlyHandle, ParentSecurity.Transform(before, sid, true)); }
            catch (SetupFailure) { rejected = true; }
            Require(rejected, "readonly_write_rejected");
        }
        using (var normal = RequestDirectoryLease.AcquireAt(anchor, false))
        {
            bool rejected = false;
            try { ParentRepair.ValidateRequests(normal, null); }
            catch (SetupFailure) { rejected = true; }
            Require(rejected, "normal_recovery_sibling_rejection");
        }
        Console.WriteLine("PASS native parent-only variant=" + race);
    }

    private static void Inject(string parent, Dictionary<string, string> preserved)
    {
        string sibling = Path.Combine(parent, "race-sibling"); Directory.CreateDirectory(sibling);
        File.WriteAllText(Path.Combine(parent, "race-file.txt"), "synthetic-race-file");
        File.WriteAllText(Path.Combine(sibling, "marker.txt"), "synthetic-race-marker");
        foreach (var entry in Snapshot(parent)) if (!preserved.ContainsKey(entry.Key)) preserved.Add(entry.Key, entry.Value);
    }

    private static void ReservationGate(string anchor)
    {
        string parent = Fixture(anchor), child = Path.Combine(parent, "PptReaderRequests");
        RequestDirectory.Create(child, RequestDirectory.Expected(false), true);
        bool rejected = false;
        try { using (var ordinary = RequestDirectoryLease.AcquireAt(anchor, false)) { } }
        catch (SetupFailure) { rejected = true; }
        Require(rejected, "ordinary_lease_must_reject");
        using (var special = RepairPreparationLease.AcquireAt(anchor, WindowsIdentity.GetCurrent().User.Value))
        {
            Reservation request = Reservation.Create(special);
            var loaded = Reservation.Load(request.request_id, special);
            Require(loaded.state == "reserved", "durable_reserved");
            bool elevationRejected = false;
            try { loaded.EnterChild(); }
            catch (SetupFailure) { elevationRejected = true; }
            Require(elevationRejected && !Machine.Admin, "ordinary_elevated_transition_rejected");
            loaded = Reservation.Load(request.request_id, special);
            loaded.state = "complete"; loaded.Save();
            Require(Reservation.Load(request.request_id, special).state == "complete", "durable_complete");
            Reservation.Create(special);
        }
        Console.WriteLine("PASS native special lease reservation");
    }

    private static DirectorySecurity Alter(DirectorySecurity before, int mask, AceFlags flags, string principal, bool deny)
    {
        var raw = new RawSecurityDescriptor(before.GetSecurityDescriptorBinaryForm(), 0);
        raw.DiscretionaryAcl.InsertAce(0, new CommonAce(flags, deny ? AceQualifier.AccessDenied : AceQualifier.AccessAllowed,
            mask, new SecurityIdentifier(principal), false, null));
        var bytes = new byte[raw.BinaryLength]; raw.GetBinaryForm(bytes, 0);
        var result = new DirectorySecurity(); result.SetSecurityDescriptorBinaryForm(bytes); return result;
    }

    private static void PolicyGate(string anchor)
    {
        string parent = Fixture(anchor), sid = WindowsIdentity.GetCurrent().User.Value;
        var before = Directory.GetAccessControl(parent);
        var explicitAcl = ParentSecurity.Transform(before, sid, false);
        Require(ParentSecurity.Legacy(before, sid) && ParentSecurity.Legacy(explicitAcl, sid), "inherited_and_explicit_policy");
        ParentRepair.ValidateBackupFreeOrigin(before, sid);
        bool protectedRecoveryRejected = false;
        try { ParentRepair.ValidateBackupFreeOrigin(explicitAcl, sid); }
        catch (SetupFailure) { protectedRecoveryRejected = true; }
        Require(protectedRecoveryRejected, "backup_absent_protected_origin_rejected");
        Require(!ParentSecurity.Legacy(before, "S-1-5-21-1-2-3-1001"), "foreign_owner");
        foreach (var entry in new[] {
            Alter(before, 0x100116, AceFlags.ObjectInherit | AceFlags.ContainerInherit, "S-1-5-32-545", false),
            Alter(before, 0x100136, AceFlags.ContainerInherit, "S-1-5-32-545", false),
            Alter(before, 0x100116, AceFlags.ContainerInherit, "S-1-5-32-545", true),
            Alter(before, 0x1200a9, AceFlags.ObjectInherit | AceFlags.ContainerInherit, "S-1-5-11", false),
            Alter(before, 0x1f01ff, AceFlags.None, "S-1-3-0", false) })
            Require(!ParentSecurity.Legacy(entry, sid), "unexpected_ace");
        string child = Path.Combine(parent, "PptReaderRequests"); RequestDirectory.Create(child, RequestDirectory.Expected(false), true);
        string other = Path.Combine(parent, "unexpected.txt"); File.WriteAllText(other, "synthetic-preserve");
        var preserved = Snapshot(parent); string originalParent = Security(parent);
        bool rejected = false;
        try { using (var special = RepairPreparationLease.AcquireAt(anchor, sid)) { } }
        catch (SetupFailure) { rejected = true; }
        Require(rejected && Security(parent) == originalParent, "foreign_sibling_rejection"); Preserve(preserved);
        Console.WriteLine("PASS native exact policy and rejection");
    }

    private static void BackupPolicyGate()
    {
        var valid = new DirectorySecurity();
        valid.SetOwner(new SecurityIdentifier("S-1-5-32-544")); valid.SetAccessRuleProtection(true, false);
        foreach (string sid in new[] { "S-1-5-32-544", "S-1-5-18" })
            valid.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(sid), FileSystemRights.FullControl, AccessControlType.Allow));
        valid.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier("S-1-5-32-545"), FileSystemRights.Read, AccessControlType.Allow));
        RepairBackup.ValidateFileSecurity(valid);
        var bad = new List<DirectorySecurity>();
        var inherited = new DirectorySecurity(); inherited.SetSecurityDescriptorBinaryForm(valid.GetSecurityDescriptorBinaryForm());
        inherited.SetAccessRuleProtection(false, true); bad.Add(inherited);
        bad.Add(Alter(valid, 0x1f01ff, AceFlags.None, "S-1-5-11", false));
        bad.Add(Alter(valid, unchecked((int)0x10000000), AceFlags.None, "S-1-5-32-545", false));
        bad.Add(Alter(valid, 0x120089, AceFlags.ObjectInherit, "S-1-5-32-545", false));
        bad.Add(Alter(valid, 0x120089, AceFlags.None, "S-1-5-32-545", true));
        var nullDacl = new DirectorySecurity(); nullDacl.SetSecurityDescriptorSddlForm("O:BAG:BAD:PNO_ACCESS_CONTROL"); bad.Add(nullDacl);
        foreach (var candidate in bad)
        {
            bool rejected = false;
            try { RepairBackup.ValidateFileSecurity(candidate); }
            catch (SetupFailure) { rejected = true; }
            Require(rejected, "unsafe_backup_security_rejected");
        }
        Console.WriteLine("PASS exact backup file ACL policy");
    }

    private static void RollbackGate()
    {
        var validate = typeof(Mutation).GetMethod("ValidateSnapshot", System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.NonPublic);
        var previous = new Dictionary<string, object> { { "version", "1.0.0.0" } };
        string path = Machine.InstalledDll("1.0.0.0");
        foreach (string url in new[] { new Uri(path).AbsoluteUri, Mutation.RegistrationCodeBase(path) })
        {
            var values = new Dictionary<string, object> {
                { "Assembly", new Dictionary<string, object> { { "kind", 1 }, { "value", "AegisDesk.PptReader, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null" } } },
                { "CodeBase", new Dictionary<string, object> { { "kind", 1 }, { "value", url } } }
            };
            var snapshot = new Dictionary<string, object> { { "values", values }, { "children", new Dictionary<string, object>() } };
            validate.Invoke(null, new object[] { snapshot, previous, 0 });
            ((Dictionary<string, object>)values["CodeBase"])["value"] = "file:///C:/foreign/reader.dll";
            bool rejected = false;
            try { validate.Invoke(null, new object[] { snapshot, previous, 0 }); }
            catch (System.Reflection.TargetInvocationException) { rejected = true; }
            Require(rejected, "foreign_rollback_codebase_rejected");
        }
        Console.WriteLine("PASS legacy rollback exact URL pair");
    }

    private static void TemporaryGate(string anchor)
    {
        Directory.CreateDirectory(anchor);
        string id = "33e979c6-c5c4-40af-845a-64c80517d1ae";
        File.WriteAllText(Path.Combine(anchor, id + ".json"), "synthetic-backup-marker");
        Require(!RepairBackup.HasTemporary(anchor, id), "base_json_is_not_temporary");
        File.WriteAllText(Path.Combine(anchor, id + ".json.fixture.tmp"), "synthetic-partial-marker");
        Require(RepairBackup.HasTemporary(anchor, id), "actual_temporary_blocks");
        Console.WriteLine("PASS exact temporary prefix excludes base JSON");
    }

    private static DirectorySecurity ContainerPolicy(string principal, int mask, AceFlags flags, string owner, bool protectedAcl = false)
    {
        var entries = new RawAcl(2, 3);
        AceFlags adminFlags = protectedAcl ? AceFlags.None : AceFlags.ContainerInherit | AceFlags.ObjectInherit | AceFlags.Inherited;
        entries.InsertAce(0, new CommonAce(adminFlags,
            AceQualifier.AccessAllowed, 0x1f01ff, new SecurityIdentifier("S-1-5-32-544"), false, null));
        entries.InsertAce(1, new CommonAce(adminFlags,
            AceQualifier.AccessAllowed, 0x1f01ff, new SecurityIdentifier("S-1-5-18"), false, null));
        entries.InsertAce(2, new CommonAce(flags, AceQualifier.AccessAllowed, mask, new SecurityIdentifier(principal), false, null));
        var raw = new RawSecurityDescriptor(ControlFlags.DiscretionaryAclPresent | ControlFlags.SelfRelative |
            (protectedAcl ? ControlFlags.DiscretionaryAclProtected : 0),
            new SecurityIdentifier(owner), null, null, entries);
        var bytes = new byte[raw.BinaryLength]; raw.GetBinaryForm(bytes, 0);
        var result = new DirectorySecurity(); result.SetSecurityDescriptorBinaryForm(bytes); return result;
    }

    private static void ContainerGate()
    {
        const string trustedInstaller = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464";
        AceFlags flags = AceFlags.Inherited;
        var inherited = ContainerPolicy(trustedInstaller, 0x1f01ff, flags, "S-1-5-32-544");
        Require(LegacyInstallation.CompatibleBoundary(inherited, true), "historical_outer_container");
        Require(!LegacyInstallation.CompatibleBoundary(inherited, false), "reader_root_still_requires_protection");
        Require(LegacyInstallation.CompatibleBoundary(ContainerPolicy("S-1-3-0", 0x10000000,
            AceFlags.ObjectInherit | AceFlags.ContainerInherit | AceFlags.InheritOnly | AceFlags.Inherited, "S-1-5-32-544"), true), "historical_creator_owner");
        foreach (var rootInvalid in new[] {
            ContainerPolicy(trustedInstaller, 0x1f01ff, AceFlags.None, "S-1-5-32-544", true),
            ContainerPolicy(trustedInstaller, 0x10000000, AceFlags.ContainerInherit | AceFlags.InheritOnly, "S-1-5-32-544", true),
            ContainerPolicy("S-1-5-21-1-2-3-1001", 0x40000000, AceFlags.ContainerInherit | AceFlags.InheritOnly, "S-1-5-32-544", true) })
            Require(!LegacyInstallation.CompatibleBoundary(rootInvalid, false), "protected_reader_foreign_io_write_rejected");
        foreach (var invalid in new[] {
            ContainerPolicy(trustedInstaller.Substring(0, trustedInstaller.Length - 1) + "5", 0x1f01ff, flags, "S-1-5-32-544"),
            ContainerPolicy(trustedInstaller, 0x1f01ff, AceFlags.ContainerInherit | AceFlags.Inherited, "S-1-5-32-544"),
            ContainerPolicy(trustedInstaller, 0x40000000, flags, "S-1-5-32-544"),
            ContainerPolicy("S-1-5-32-545", 0x1f01ff, flags, "S-1-5-32-544"),
            ContainerPolicy("S-1-5-21-1-2-3-1001", 0x40000000, AceFlags.ContainerInherit | AceFlags.InheritOnly | AceFlags.Inherited, "S-1-5-32-544"),
            ContainerPolicy("S-1-3-0", 0x10000000, AceFlags.ContainerInherit | AceFlags.Inherited, "S-1-5-32-544"),
            ContainerPolicy(trustedInstaller, 0x1f01ff, flags, "S-1-5-21-1-2-3-1001") })
            Require(!LegacyInstallation.CompatibleBoundary(invalid, true), "unsafe_outer_container_rejected");
        Console.WriteLine("PASS historical outer container exact service SID");
    }

    public static int Main(string[] args)
    {
        try
        {
            Require(args.Length == 1 && !Directory.Exists(args[0]), "fresh_fixture_required");
            Directory.CreateDirectory(args[0]);
            ReservationGate(Path.Combine(args[0], "reservation"));
            PolicyGate(Path.Combine(args[0], "policy"));
            BackupPolicyGate();
            RollbackGate();
            TemporaryGate(Path.Combine(args[0], "temporary"));
            ContainerGate();
            for (int race = 0; race < 3; race++) Run(Path.Combine(args[0], "case" + race), race);
            Console.WriteLine("PASS native parent-only gate"); return 0;
        }
        catch (SetupFailure error) { Console.WriteLine("FAIL " + error.Stage + " " + error.ErrorType + " " + error.WinError); return 1; }
        catch (Exception error) { Console.WriteLine("FAIL " + error.GetType().Name + " " + error.Message); return 1; }
    }
}
