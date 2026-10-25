using System;
using System.Collections.Generic;
using System.IO;
using System.Diagnostics;
using System.Security.AccessControl;
using System.Security.Principal;
using AegisDesk.PptSetup;

internal static class SetupHarness
{
    private static void Require(bool value, string code) { if (!value) throw new Exception(code); }
    private static Dictionary<string, object> Record()
    {
        return new Dictionary<string, object> { { "schema", 1 }, { "request_id", "33e979c6-c5c4-40af-845a-64c80517d1ae" },
            { "required_version", "1.0.5.11" }, { "helper_sid", "S-1-5-21-1-2-3-1001" }, { "helper_pid", 123 },
            { "helper_creation_ticks", "638901000000000000" }, { "child_pid", null }, { "child_creation_ticks", null }, { "state", "complete" } };
    }
    public static int Main(string[] args)
    {
        string id = "33e979c6-c5c4-40af-845a-64c80517d1ae";
        Require(Contract.AutoInstallMode == "disabled" || Contract.AutoInstallMode == "candidate" || Contract.AutoInstallMode == "released", "policy_mode");
        Require(typeof(Contract).GetMethod("TraceAtomic", System.Reflection.BindingFlags.Static |
            System.Reflection.BindingFlags.NonPublic) == null, "shipping_atomic_trace_erased");
        foreach (string hook in new[] { "TraceRequest", "InterceptNative" })
            Require(typeof(RequestWriter).GetMethod(hook, System.Reflection.BindingFlags.Static |
                System.Reflection.BindingFlags.NonPublic) == null, "shipping_request_hook_erased_" + hook);
        RequestWriter.NativeLayout();
        Require(RequestWriter.Basename(id) == id + ".json", "request_fixed_basename");
        foreach (string invalidId in new[] { "../" + id, id + ":stream", id.ToUpperInvariant(), id + ".json", "request.json" })
        {
            bool invalidRejected = false;
            try { RequestWriter.Basename(invalidId); } catch (IOException) { invalidRejected = true; }
            Require(invalidRejected, "request_invalid_basename");
        }
        byte[] encoded = RequestWriter.Encode(Record(), RequestWriteMode.ReplaceExisting);
        Require(encoded.Length > 0 && encoded.Length <= 8192 && encoded[0] == (byte)'{', "request_utf8_no_bom");
        var decode = (Func<byte[], string, Dictionary<string, object>>)Delegate.CreateDelegate(
            typeof(Func<byte[], string, Dictionary<string, object>>), typeof(RequestWriter).GetMethod("Decode",
                System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.NonPublic));
        Require((string)decode(encoded, id)["request_id"] == id, "request_decode_identity");
        var legacy = new List<byte>(new byte[] { 0xef, 0xbb, 0xbf }); legacy.AddRange(encoded);
        Require((string)decode(legacy.ToArray(), id)["request_id"] == id, "request_legacy_bom");
        var uppercase = Record(); uppercase["request_id"] = id.ToUpperInvariant();
        bool uppercaseRejected = false;
        try { decode(System.Text.Encoding.UTF8.GetBytes(Contract.Json.Serialize(uppercase)), id); }
        catch (IOException) { uppercaseRejected = true; }
        Require(uppercaseRejected, "request_noncanonical_json_identity");
        foreach (int invalidKind in new[] { 0, 1, 2, 3 })
        {
            var invalidRecord = Record();
            if (invalidKind == 0) invalidRecord["required_version"] = "1." + new string('1', 9000) + ".0.0";
            if (invalidKind == 1) invalidRecord["schema"] = true;
            if (invalidKind == 2) invalidRecord["request_id"] = "../" + id;
            bool invalidRejected = false;
            try { RequestWriter.Encode(invalidRecord, invalidKind == 3 ? (RequestWriteMode)99 : RequestWriteMode.ReplaceExisting); }
            catch (IOException) { invalidRejected = true; }
            Require(invalidRejected, "request_serialization_reject_" + invalidKind);
        }
        var unchanged = Record(); unchanged["state"] = "reserved";
        bool transitionRejected = false;
        try { RequestWriter.ValidateTransition(unchanged, unchanged); } catch (IOException) { transitionRejected = true; }
        Require(transitionRejected, "request_reserved_not_update");
        foreach (string immutable in new[] { "schema", "request_id", "required_version", "helper_sid", "helper_pid", "helper_creation_ticks" })
        {
            var changed = Record(); changed[immutable] = null;
            transitionRejected = false;
            try { RequestWriter.ValidateTransition(Record(), changed); } catch (IOException) { transitionRejected = true; }
            Require(transitionRejected, "request_immutable_" + immutable);
        }
        Require(Contract.AutoInstallValidated == (Contract.AutoInstallMode == "released"), "policy_validated");
        Require(Contract.AutoShutdownEnabled == (Contract.AutoInstallMode == "candidate"), "shutdown_policy_candidate_only");
        Require(Contract.AutoInstallEnabled == (Contract.AutoInstallMode != "disabled"), "policy_enabled");
        var result = new Result("check");
        Require(result.auto_install_mode == Contract.AutoInstallMode && result.auto_install_validated == Contract.AutoInstallValidated, "result_policy");
        Require(Program.FailureCode(new IOException("mutex_busy")) == 10, "mutex_transient");
        Require(Program.FailureCode(new IOException("office_busy")) == 10, "office_transient");
        Require(Program.FailureCode(new IOException("payload_invalid")) == 13, "payload_not_transient");
        foreach (int failure in new[] { 10, 12, 13 })
            Require(Program.CompletionCode(11, failure) == 11, "denial_evidence_survives_" + failure);
        Require(Program.CompletionCode(0, 10) == 10, "post_mutex_transient");
        foreach (bool parent in new[] { true, false })
        {
            DirectorySecurity acl = RequestDirectory.Expected(parent);
            acl.SetOwner(new SecurityIdentifier("S-1-5-21-1-2-3-1001"));
            Require(RequestDirectory.Compatible(acl, parent), "expected_acl");
            Require(!RequestDirectory.Compatible(acl, !parent), "parent_child_acl_distinct");
            acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(WellKnownSidType.BuiltinUsersSid, null),
                FileSystemRights.FullControl, AccessControlType.Allow));
            Require(!RequestDirectory.Compatible(acl, parent), "existing_unsafe_acl_rejected");
        }
        // Production ACL predicate, not a second implementation of its policy.
        var accountOwner = new SecurityIdentifier("S-1-5-21-1-2-3-1001");
        var ordinary = new SecurityIdentifier("S-1-5-21-1-2-3-1002");
        var inheritedParent = RequestDirectory.Expected(true);
        inheritedParent.SetOwner(accountOwner);
        inheritedParent.SetAccessRuleProtection(false, true);
        Require(!inheritedParent.AreAccessRulesProtected && RequestDirectory.Compatible(inheritedParent, true), "safe_inherited_parent");
        Require(!RequestDirectory.Compatible(inheritedParent, false), "inherited_child_rejected");
        foreach (FileSystemRights danger in new[] { FileSystemRights.WriteData, FileSystemRights.WriteAttributes,
            FileSystemRights.WriteExtendedAttributes, FileSystemRights.Delete, FileSystemRights.DeleteSubdirectoriesAndFiles,
            FileSystemRights.ChangePermissions, FileSystemRights.TakeOwnership })
        {
            var unsafeParent = RequestDirectory.Expected(true); unsafeParent.SetOwner(accountOwner);
            unsafeParent.AddAccessRule(new FileSystemAccessRule(ordinary, danger, AccessControlType.Allow));
            Require(!RequestDirectory.Compatible(unsafeParent, true), "parent_danger_" + danger);
        }
        var trustedOwner = RequestDirectory.Expected(true); trustedOwner.SetOwner(accountOwner);
        trustedOwner.AddAccessRule(new FileSystemAccessRule(accountOwner, FileSystemRights.FullControl, AccessControlType.Allow));
        Require(RequestDirectory.Compatible(trustedOwner, true), "existing_owner_trust_boundary");
        var inheritOnly = RequestDirectory.Expected(true); inheritOnly.SetOwner(accountOwner);
        inheritOnly.AddAccessRule(new FileSystemAccessRule(ordinary, FileSystemRights.FullControl,
            InheritanceFlags.ObjectInherit, PropagationFlags.InheritOnly, AccessControlType.Allow));
        Require(RequestDirectory.Compatible(inheritOnly, true), "inherit_only_not_parent_access");
        var foreignOwner = RequestDirectory.Expected(true);
        foreignOwner.SetOwner(new SecurityIdentifier(WellKnownSidType.BuiltinUsersSid, null));
        Require(!RequestDirectory.Compatible(foreignOwner, true), "non_account_owner_rejected");
        foreach (bool callback in new[] { false, true })
        {
            var rawAcl = new RawAcl(2, 1);
            rawAcl.InsertAce(0, new CommonAce(AceFlags.None, AceQualifier.AccessAllowed,
                unchecked((int)0x40000000), ordinary, callback, callback ? new byte[4] : null));
            var raw = new RawSecurityDescriptor(ControlFlags.DiscretionaryAclPresent | ControlFlags.SelfRelative,
                accountOwner, null, null, rawAcl);
            var bytes = new byte[raw.BinaryLength]; raw.GetBinaryForm(bytes, 0);
            var unknown = new DirectorySecurity(); unknown.SetSecurityDescriptorBinaryForm(bytes);
            Require(!RequestDirectory.Compatible(unknown, true), callback ? "callback_ace_rejected" : "generic_write_rejected");
        }
        var objectAcl = new RawAcl(4, 1);
        objectAcl.InsertAce(0, new ObjectAce(AceFlags.None, AceQualifier.AccessAllowed,
            (int)FileSystemRights.Read, ordinary, ObjectAceFlags.None, Guid.Empty, Guid.Empty, false, null));
        var noncanonicalAcl = new RawAcl(2, 2);
        noncanonicalAcl.InsertAce(0, new CommonAce(AceFlags.None, AceQualifier.AccessAllowed, (int)FileSystemRights.Read, ordinary, false, null));
        noncanonicalAcl.InsertAce(1, new CommonAce(AceFlags.None, AceQualifier.AccessDenied, (int)FileSystemRights.WriteData, ordinary, false, null));
        foreach (RawAcl unsupported in new[] { objectAcl, noncanonicalAcl })
        {
            var raw = new RawSecurityDescriptor(ControlFlags.DiscretionaryAclPresent | ControlFlags.SelfRelative,
                accountOwner, null, null, unsupported);
            var bytes = new byte[raw.BinaryLength]; raw.GetBinaryForm(bytes, 0);
            var actual = new DirectorySecurity(); actual.SetSecurityDescriptorBinaryForm(bytes);
            Require(!RequestDirectory.Compatible(actual, true), unsupported == objectAcl ? "object_ace_rejected" : "noncanonical_dacl_rejected");
        }
        var wrongChild = RequestDirectory.Expected(false); wrongChild.SetOwner(accountOwner);
        wrongChild.AddAccessRule(new FileSystemAccessRule(ordinary, FileSystemRights.Read, AccessControlType.Allow));
        Require(wrongChild.AreAccessRulesProtected && !RequestDirectory.Compatible(wrongChild, false), "protected_child_wrong_rules_rejected");
        var noDacl = new RawSecurityDescriptor(ControlFlags.DiscretionaryAclPresent | ControlFlags.SelfRelative,
            accountOwner, null, null, null);
        var noDaclBytes = new byte[noDacl.BinaryLength]; noDacl.GetBinaryForm(noDaclBytes, 0);
        var noDaclSecurity = new DirectorySecurity(); noDaclSecurity.SetSecurityDescriptorBinaryForm(noDaclBytes);
        Require(!RequestDirectory.Compatible(noDaclSecurity, true), "null_dacl_rejected");
        var diag = new Result("prepare");
        Require(diag.Values().Count == 11, "legacy_result_shape");
        SetupFailure.From("parent_open", new System.ComponentModel.Win32Exception(5)).Apply(diag);
        Require(diag.Values().Count == 14 && diag.stage == "parent_open" && diag.winerror == 5, "native_error_additive_shape");
        var secretError = SetupFailure.From("request_acl", new IOException("SECRET-MARKER"));
        secretError.Apply(diag);
        string safeJson = Contract.Json.Serialize(diag.Values());
        Require(!safeJson.Contains("SECRET-MARKER") && diag.winerror == null, "raw_exception_not_emitted");
        // These are Windows paths even when the pure harness runs under Mono.
        string suffix = "/AegisDesk/PptReader/1.0.5.11/AegisDesk.PptReader.dll";
        foreach (string directory in new[] { "Program Files", "Program Files (x86)", "프로그램 파일", "literal%20folder" })
        {
            string file = @"C:\" + directory + suffix.Replace('/', '\\');
            string codebase = Mutation.RegistrationCodeBase(file);
            Require(codebase == "file:///C:/" + directory + suffix, "codebase_unescaped_" + directory);
        }
        Require(Mutation.RegistrationCodeBase(@"C:\Program Files" + suffix.Replace('/', '\\')) !=
            new Uri(@"C:\Program Files" + suffix.Replace('/', '\\')).AbsoluteUri, "escaped_codebase_regression");
        Require(Reservation.Valid(Record(), id), "complete_valid");
        var old = Record(); old["required_version"] = "0.9.0.0";
        Require(Reservation.Valid(old, id), "old_complete_valid");
        foreach (string field in new[] { "schema", "helper_pid", "helper_creation_ticks", "request_id", "state", "helper_sid" })
        {
            var bad = Record(); bad[field] = field == "schema" || field == "helper_pid" ? (object)true : "invalid";
            Require(!Reservation.Valid(bad, id), "corrupt_" + field);
        }
        Require(!Reservation.Valid(Record(), "00000000-0000-0000-0000-000000000000"), "filename_identity");
        var child = Record(); child["state"] = "elevated";
        Require(!Reservation.Valid(child, id), "missing_child");
        child["child_pid"] = 456; child["child_creation_ticks"] = "638901000000000001";
        Require(Reservation.Valid(child, id), "child_identity");
        using (Process current = Process.GetCurrentProcess())
        {
            string ticks = current.StartTime.ToUniversalTime().Ticks.ToString();
            Require(!Reservation.ProcessGone(current.Id, ticks), "live_helper_not_recovered");
            Require(!Reservation.ProcessGone(current.Id, "1"), "identity_mismatch_not_recovered");
            var late = new Reservation { helper_pid = current.Id, helper_creation_ticks = ticks, state = "complete" };
            bool lateRejected = false;
            try { late.EnterChild(); } catch (IOException) { lateRejected = true; }
            Require(lateRejected, "late_uac_child_no_mutation");
            late.state = "reserved"; late.required_version = "0.9.0.0";
            lateRejected = false;
            try { late.EnterChild(); } catch (IOException) { lateRejected = true; }
            Require(lateRejected, "old_request_no_mutation");
        }
        string path = Path.Combine(args[0], "fixture.exe");
        foreach (int bits in new[] { 32, 64 })
        {
            using (var stream = new FileStream(path, FileMode.Create))
            using (var writer = new BinaryWriter(stream))
            {
                stream.SetLength(256); writer.Write((ushort)0x5a4d); stream.Position = 0x3c; writer.Write(128);
                stream.Position = 128; writer.Write((uint)0x4550); writer.Write((ushort)(bits == 32 ? 0x14c : 0x8664));
            }
            Require(Machine.ReadPeBitness(path) == bits, "actual_pe_" + bits);
        }
        File.WriteAllText(path, "not_pe");
        bool rejected = false;
        try { Machine.ReadPeBitness(path); } catch (IOException) { rejected = true; }
        Require(rejected, "invalid_pe");
        File.Delete(path);
        Console.WriteLine("PASS setup pure contracts; no registry, UAC, Office or installation run");
        return 0;
    }
}
