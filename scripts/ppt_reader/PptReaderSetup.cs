using System;
using System.IO;
using System.Reflection;

[assembly: AssemblyVersion("1.0.4.0")]
[assembly: AssemblyFileVersion("1.0.4.0")]

namespace AegisDesk.PptSetup
{
    internal static class Program
    {
        internal static int Main(string[] args)
        {
            string operation = args.Length == 0 ? "check" : args[0].TrimStart('/');
            Result result = new Result(operation);
            int code = 12;
            try
            {
                if (args.Length == 1 && operation == "check")
                {
                    result = Machine.Check("check");
                    code = result.machine_safe ? 0 : (result.status == "busy" ? 10 : 12);
                }
                else if (args.Length == 2 && (operation == "elevated-install" || operation == "elevated-remove"))
                {
                    if (!Machine.Admin || !Contract.PayloadValid()) throw new IOException("payload_invalid");
                    using (var gate = new MachineLock())
                    {
                        using (var lease = RequestDirectoryLease.AcquireExisting())
                        {
                            Reservation request = Reservation.Load(args[1], lease);
                            request.EnterChild();
                            lease.Recheck();
                            Mutation.Apply(operation == "elevated-remove", request.request_id, lease);
                        }
                    }
                    // The ordinary helper owns reservation completion and the
                    // final original-user check. No administrator HKCU writes.
                    return 0;
                }
                else if (operation == "recover" && args.Length == 2)
                {
                    if (!Machine.Admin) throw new IOException("recovery_admin_required");
                    using (var gate = new MachineLock())
                    {
                        if (!Machine.OfficeClosed()) throw new IOException("office_busy");
                        using (var lease = RequestDirectoryLease.Acquire())
                        {
                            if (args[1] != "journal") Reservation.Load(args[1], lease).Recover();
                            if (!Machine.ReservationsSafe()) throw new IOException("reservation_unresolved");
                            lease.Recheck();
                            Mutation.Recover();
                            result = Machine.Check(operation, true);
                            code = result.machine_safe ? 0 : 13;
                        }
                    }
                }
                else if ((operation == "prepare" || operation == "remove") &&
                         (args.Length == 1 || (args.Length == 2 && args[1] == "/prototype")))
                {
                    bool prototype = args.Length == 2;
                    if (!Contract.AutoInstallEnabled && !prototype && operation == "prepare")
                    { result = Machine.Check(operation); result.status = "failed"; result.reason = "prototype_gate"; code = 12; }
                    else code = Prepare(operation == "remove", ref result);
                }
            }
            catch (SetupFailure ex)
            {
                ex.Apply(result); result.status = "failed"; result.reason = "acl_unavailable";
                result.machine_safe = false; code = 12;
            }
            catch (IOException ex)
            {
                code = FailureCode(ex);
                result.status = code == 10 ? "busy" : "failed";
                result.reason = code == 10 || ex.Message == "payload_invalid" ? ex.Message : "mutation_failed";
                result.machine_safe = false;
            }
            catch { result.status = "unknown"; result.reason = "check_failed"; result.machine_safe = false; code = 12; }
            result.Emit();
            return code;
        }
        internal static int FailureCode(IOException error)
        { return error.Message == "mutex_busy" || error.Message == "office_busy" ? 10 : 13; }

        internal static int CompletionCode(int observedCode, int completionFailure)
        { return observedCode == 11 ? 11 : completionFailure; }

        private static int Prepare(bool remove, ref Result result)
        {
            if (!Contract.PayloadValid()) { result.reason = "payload_invalid"; return 12; }
            RequestDirectoryLease lease = null;
            try { return PrepareLeased(remove, ref result, ref lease); }
            finally { if (lease != null) lease.Dispose(); }
        }

        private static int PrepareLeased(bool remove, ref Result result, ref RequestDirectoryLease lease)
        {
            string operation = remove ? "remove" : "prepare";
            Reservation request;
            using (var gate = new MachineLock())
            {
                result = Machine.Check(operation, true);
                if (!result.machine_safe || result.office_bitness == null) return result.reason == "mutex_busy" || result.reason == "office_busy" ? 10 : 12;
                if (!Machine.OfficeClosed()) { result.status = "busy"; result.reason = "office_busy"; return 10; }
                if (result.status == "conflict" || (!remove && result.status == "disabled")) return 12;
                if (!remove && result.status == "ready") return 0;
                try
                {
                    lease = RequestDirectoryLease.Acquire();
                    // Re-read reservations in the leased namespace: the path
                    // observed by the earlier read-only check may have raced.
                    result = Machine.Check(operation, true);
                    if (!result.machine_safe || result.office_bitness == null || result.status == "conflict" ||
                        (!remove && result.status == "disabled")) return 12;
                    if (!Machine.OfficeClosed()) { result.status = "busy"; result.reason = "office_busy"; return 10; }
                    request = Reservation.Create(lease);
                }
                catch (SetupFailure error)
                { error.Apply(result); result.status = "failed"; result.reason = "acl_unavailable"; result.machine_safe = false; return 12; }
                catch (Exception error)
                { SetupFailure.From("request_create", error).Apply(result); result.status = "failed"; result.reason = "acl_unavailable"; result.machine_safe = false; return 12; }
            }
            lease.Recheck();
            int code = Elevation.Run(remove, request);
            if (code == 14)
            {
                result.status = "busy"; result.reason = "child_unconfirmed"; result.machine_safe = false;
                // Preserve reserved/elevated state so future app launches wait.
                return 12;
            }
            try
            {
                using (var gate = new MachineLock())
                {
                    lease.Recheck();
                    result = Machine.Check(operation, true, true, request.request_id);
                    if (result.machine_safe)
                    {
                        request = Reservation.Load(request.request_id, lease);
                        request.state = "complete"; request.Save();
                        result = Machine.Check(operation, true);
                    }
                    else if (code == 0) code = 13;
                }
            }
            catch (SetupFailure error)
            {
                error.Apply(result); result.machine_safe = false;
                result.status = "busy"; result.reason = "reservation_unresolved";
                code = CompletionCode(code, 13);
            }
            catch (IOException ex)
            {
                result.machine_safe = false;
                result.status = "busy";
                result.reason = FailureCode(ex) == 10 ? ex.Message : "reservation_unresolved";
                code = CompletionCode(code, FailureCode(ex));
            }
            catch
            {
                result.machine_safe = false; result.status = "unknown"; result.reason = "check_failed";
                code = CompletionCode(code, 13);
            }
            // Explicit ERROR_CANCELLED remains evidence even when reservation
            // completion or the original user's check cannot establish safety.
            if (code == 11) { result.status = "failed"; result.reason = "permission_declined"; }
            else if (code == 10) { result.status = "busy"; if (result.reason != "mutex_busy") result.reason = "office_busy"; }
            else if (code != 0) { result.status = "failed"; result.reason = "mutation_failed"; }
            else if (remove) { result.status = "missing"; result.reason = "removed"; }
            return code == 0 || code == 10 || code == 11 || code == 12 || code == 13 ? code : 13;
        }
    }
}
