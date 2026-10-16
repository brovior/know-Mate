using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Security.AccessControl;
using System.Security.Principal;
using AegisDesk.PptSetup;

internal sealed class WriterFixtureUnavailable : Exception
{
    internal readonly int WinError;
    internal WriterFixtureUnavailable(int error) { WinError = error; }
}

internal static class WriterLinkFixture
{
    internal static WriterFixtureRoot External;
    private static string externalSentinel, externalText, externalId, externalSecurity, externalRootSecurity;
    internal static void VerifyExternal()
    {
        if (External == null) return;
        WriterFixtureRoot.Stage = "cleanup_verify";
        WriterFixtureRoot.Require(WriterFixtureNative.Inspect(External.PathName).Id == External.Id
            && WriterFixtureNative.Security(External.PathName) == externalRootSecurity
            && WriterFixtureNative.Inspect(externalSentinel).Id == externalId
            && WriterFixtureNative.Security(externalSentinel) == externalSecurity
            && File.ReadAllText(externalSentinel) == externalText, "cleanup_external_changed");
    }
    private static Dictionary<string, object> Values(string id)
    {
        using (Process process = Process.GetCurrentProcess())
        using (WindowsIdentity identity = WindowsIdentity.GetCurrent())
            return new Dictionary<string, object> { { "schema", 1 }, { "request_id", id },
                { "required_version", Contract.Version }, { "helper_sid", identity.User.Value },
                { "helper_pid", process.Id }, { "helper_creation_ticks", process.StartTime.ToUniversalTime().Ticks.ToString() },
                { "child_pid", null }, { "child_creation_ticks", null }, { "state", "reserved" } };
    }
    private static void Require(bool value, string code) { WriterFixtureRoot.Require(value, code); }
    internal static void Run(string anchor, int count, string mode)
    {
        RequestWriterHarness.Stage("fixture_prepare");
        string parent = Path.Combine(anchor, "AegisDesk"), child = Path.Combine(parent, "PptReaderRequests");
        Directory.CreateDirectory(child);
        string id = Guid.NewGuid().ToString("D"), target = Path.Combine(child, id + ".json");
        string backing = Path.Combine(anchor, "backing.fixture");
        Dictionary<string, object> values = Values(id);
        string text = Contract.Json.Serialize(values);
        if (mode == "hardlink")
        {
            File.WriteAllText(target, text);
            File.SetAccessControl(target, RequestWriter.ExpectedSecurity((string)values["helper_sid"], true));
        }
        else if (mode == "reparse")
        {
            File.WriteAllText(backing, text);
            File.SetAccessControl(backing, RequestWriter.ExpectedSecurity((string)values["helper_sid"], true));
        }
        else
        {
            string external = Path.Combine(Path.GetDirectoryName(anchor), "ppt-writer-" + Guid.NewGuid().ToString("N"));
            External = WriterFixtureRoot.Create(external, false);
            externalSentinel = Path.Combine(external, "sentinel.fixture"); File.WriteAllText(externalSentinel, "sentinel");
            externalText = File.ReadAllText(externalSentinel); externalId = WriterFixtureNative.Inspect(externalSentinel).Id;
            externalSecurity = WriterFixtureNative.Security(externalSentinel); externalRootSecurity = WriterFixtureNative.Security(external);
        }
        // Every preparation handle is closed before either product directory lease exists.
        try
        {
            if (mode == "hardlink") WriterFixtureNative.Hardlink(backing, target);
            else if (mode == "reparse") WriterFixtureNative.Symlink(target, backing);
            else WriterFixtureNative.Junction(target, External.PathName);
        }
        catch (Win32Exception error) { throw new WriterFixtureUnavailable(error.NativeErrorCode); }
        RequestWriterHarness.Stage("fixture_validate");
        WriterFixtureNative.Info baseline = WriterFixtureNative.Inspect(target);
        if (mode == "hardlink")
        {
            var alias = WriterFixtureNative.Inspect(backing);
            Require(baseline.Id == alias.Id, "fixture_identity");
            Require(baseline.Links == 2 && alias.Links == 2, "fixture_link_count");
        }
        else Require((baseline.Attributes & 0x400) != 0, "fixture_identity");
        if (mode != "hardlink") WriterFixtureNative.ProtectLink(target);
        Directory.SetAccessControl(child, RequestDirectory.Expected(false));
        baseline = WriterFixtureNative.Inspect(target);
        string security = WriterFixtureNative.Security(target);
        string backingId = mode == "reparse-directory" ? null : WriterFixtureNative.Inspect(backing).Id;
        string backingSecurity = mode == "reparse-directory" ? null : WriterFixtureNative.Security(backing);
        using (var mutex = new MachineLock())
        using (var first = RequestDirectoryLease.AcquireAt(anchor))
        using (var second = count == 2 ? RequestDirectoryLease.AcquireAt(anchor) : null)
        {
            RequestWriterHarness.Child = first.ChildPath;
            int before = RequestWriterHarness.CommitCount;
            for (int operation = 0; operation != 2; operation++)
            {
                RequestWriterHarness.Stage(operation == 0 ? "fixture_read" : "fixture_write");
                bool rejected = false;
                try
                {
                    if (operation == 0) RequestWriter.Read(first, id);
                    else { values["state"] = "complete"; RequestWriter.Write(first, values, RequestWriteMode.ReplaceExisting); }
                }
                catch (SetupFailure error) { rejected = true; RequestWriterHarness.RecordRejection(error); }
                catch (IOException)
                { rejected = true; Console.WriteLine("PPT_WRITER_REJECT stage=policy error_type=IOException winerror=none"); }
                catch (UnauthorizedAccessException)
                { rejected = true; Console.WriteLine("PPT_WRITER_REJECT stage=policy error_type=access winerror=none"); }
                Require(rejected, operation == 0 ? "fixture_read_accepted" : "fixture_write_accepted");
                RequestWriterHarness.Stage("preservation_verify");
                var after = WriterFixtureNative.Inspect(target);
                Require(after.Id == baseline.Id && after.Attributes == baseline.Attributes && after.Links == baseline.Links
                    && WriterFixtureNative.Security(target) == security, "fixture_preservation");
                if (mode != "reparse-directory") Require(File.ReadAllText(backing) == text
                    && WriterFixtureNative.Inspect(backing).Id == backingId && WriterFixtureNative.Security(backing) == backingSecurity,
                    "fixture_preservation");
                else VerifyExternal();
                Require(RequestWriterHarness.CommitCount == before, "invalid_target_reached_commit");
                first.Recheck(); if (second != null) second.Recheck();
            }
            Console.WriteLine("PPT_WRITER_EXPECT expected=reject actual=reject verified=1");
        }
    }
}
