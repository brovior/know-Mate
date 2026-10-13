using System;
using System.Collections.Generic;
using System.IO;
using System.Diagnostics;
using AegisDesk.PptSetup;

internal static class SetupHarness
{
    private static void Require(bool value, string code) { if (!value) throw new Exception(code); }
    private static Dictionary<string, object> Record()
    {
        return new Dictionary<string, object> { { "schema", 1 }, { "request_id", "33e979c6-c5c4-40af-845a-64c80517d1ae" },
            { "required_version", "1.0.0.0" }, { "helper_sid", "S-1-5-21-1-2-3-1001" }, { "helper_pid", 123 },
            { "helper_creation_ticks", "638901000000000000" }, { "child_pid", null }, { "child_creation_ticks", null }, { "state", "complete" } };
    }
    public static int Main(string[] args)
    {
        string id = "33e979c6-c5c4-40af-845a-64c80517d1ae";
        Require(!Contract.AutoInstallValidated, "gate_not_closed");
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
