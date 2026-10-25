using System;
using System.Collections.Generic;
using System.IO;
using System.Text;

namespace AegisDesk.PptSetup
{
    internal static partial class RequestWriter
    {
        internal static Dictionary<string, object> Read(IReservationNamespace lease, string id)
        {
            if (lease == null) throw new SetupFailure("lease_recheck", "policy");
            lease.Recheck(); string path = Path.Combine(lease.ChildPath, Basename(id));
            RequestFile target = null; Exception primary = null;
            try
            {
                TraceRequest("target_open"); target = RequestFile.Open(path, null);
                target.Inspect(path);
                var values = Decode(target.Read(), id);
                ValidateSecurity(target.Security(), (string)values["helper_sid"], null);
                TraceRequest("target_validated"); return values;
            }
            catch (Exception error) { primary = error; throw; }
            finally
            {
                if (target != null)
                {
                    try { try { TraceRequest("target_close"); } finally { target.Close(); } TraceRequest("target_closed"); }
                    catch (Exception error)
                    {
                        if (primary == null) throw;
                        primary.Data["request_target_close_failure"] = SetupFailure.From("request_commit", error).ErrorType;
                    }
                }
            }
        }
        private static Dictionary<string, object> Decode(byte[] bytes, string id)
        {
            int offset = bytes.Length >= 3 && bytes[0] == 0xef && bytes[1] == 0xbb && bytes[2] == 0xbf ? 3 : 0;
            string json = new UTF8Encoding(false, true).GetString(bytes, offset, bytes.Length - offset);
            var values = Contract.Json.Deserialize<Dictionary<string, object>>(json);
            if (values == null || !Reservation.Valid(values, id) || (string)values["request_id"] != id) throw new IOException("reservation_identity");
            return values;
        }
        private static bool Equal(byte[] first, byte[] second)
        {
            if (first.Length != second.Length) return false;
            for (int index = 0; index != first.Length; index++) if (first[index] != second[index]) return false;
            return true;
        }
        internal static void Write(IReservationNamespace lease, Dictionary<string, object> values, RequestWriteMode mode)
        {
            if (lease == null) throw new SetupFailure("lease_recheck", "policy");
            lease.Recheck();
            byte[] bytes;
            try { bytes = Encode(values, mode); }
            catch (Exception error) { throw SetupFailure.From("request_create", error); }
            string id = (string)values["request_id"], target = Path.Combine(lease.ChildPath, Basename(id));
            if (mode == RequestWriteMode.ReplaceExisting)
            {
                try
                {
                    // Read closes ALL validation handles, including its stream,
                    // before native replacement. Target open is not a CAS promise.
                    var previous = Read(lease, id); ValidateTransition(previous, values);
                }
                catch (Exception error) { throw SetupFailure.From("request_commit", error); }
            }
            System.Security.AccessControl.FileSecurity security;
            try { security = ExpectedSecurity((string)values["helper_sid"], true); }
            catch (Exception error) { throw SetupFailure.From("request_acl", error); }
            string temp = target + "." + Guid.NewGuid().ToString("N") + ".tmp";
            RequestFile source = null; Exception primary = null; bool returned = false; uint status = 0; uint? ioStatus = null;
            try
            {
                try
                {
                    TraceRequest("source_open"); source = RequestFile.Open(temp, security);
                    source.Inspect(temp);
                    TraceRequest("source_acl"); ValidateSecurity(source.Security(), (string)values["helper_sid"], security);
                    TraceRequest("source_write"); source.Stream.Write(bytes, 0, bytes.Length);
                    TraceRequest("source_flush"); source.Stream.Flush(true);
                    if (!Equal(source.Read(), bytes)) throw new IOException("request_content");
                    TraceRequest("source_written");
                }
                catch (Exception error) { throw SetupFailure.From("request_create", error); }
                RequestFile.Identity identity = source.Inspect(temp);
                lease.Recheck();
                Commit(source, id, mode == RequestWriteMode.ReplaceExisting, out status, out ioStatus); returned = true;
                try
                {
                    TraceRequest("commit_verify");
                    if (!RequestFile.Same(identity, source.Inspect(target)) || !Equal(source.Read(), bytes)) throw new IOException("request_content");
                    ValidateSecurity(source.Security(), (string)values["helper_sid"], security);
                    lease.Recheck();
                }
                catch (Exception error) { throw SetupFailure.WithNative(error, "commit_verify", status, ioStatus); }
            }
            catch (Exception error) { primary = SetupFailure.From("request_commit", error); throw primary; }
            finally
            {
                if (source != null)
                {
                    try { try { TraceRequest("source_close"); } finally { source.Close(); } TraceRequest("source_closed"); }
                    catch (Exception error)
                    {
                        SetupFailure close = returned ? SetupFailure.WithNative(error, "commit_close", status, ioStatus) : SetupFailure.From("request_commit", error);
                        if (primary == null) throw close;
                        // Preserve the first failure (especially actual NTSTATUS),
                        // while retaining bounded secondary evidence for tests.
                        primary.Data["request_close_failure"] = close.ErrorType;
                        TraceRequest("source_close_failed");
                    }
                }
            }
            // No path-based reopen while the source DELETE handle exists.
            // No failure fallback, deletion, ACL rewrite, or temp cleanup.
        }
    }
}
