using System;
using System.IO;
using System.Reflection;

[assembly: AssemblyVersion("1.0.0.0")]
[assembly: AssemblyFileVersion("1.0.0.0")]

namespace AegisDesk.PptSetup
{
    internal static class Program
    {
        private static int Main(string[] args)
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
                        Reservation request = Reservation.Load(args[1]);
                        request.EnterChild();
                        Mutation.Apply(operation == "elevated-remove", request.request_id);
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
                        if (args[1] != "journal") Reservation.Load(args[1]).Recover();
                        if (!Machine.ReservationsSafe()) throw new IOException("reservation_unresolved");
                        Mutation.Recover();
                        result = Machine.Check(operation, true);
                        code = result.machine_safe ? 0 : 13;
                    }
                }
                else if ((operation == "prepare" || operation == "remove") &&
                         (args.Length == 1 || (args.Length == 2 && args[1] == "/prototype")))
                {
                    bool prototype = args.Length == 2;
                    if (!Contract.AutoInstallValidated && !prototype && operation == "prepare")
                    { result = Machine.Check(operation); result.status = "failed"; result.reason = "prototype_gate"; code = 12; }
                    else code = Prepare(operation == "remove", ref result);
                }
            }
            catch (IOException ex)
            {
                result.status = "failed";
                result.reason = ex.Message == "payload_invalid" ? "payload_invalid" : "mutation_failed";
                result.machine_safe = false; code = 13;
            }
            catch { result.status = "unknown"; result.reason = "check_failed"; result.machine_safe = false; code = 12; }
            result.Emit();
            return code;
        }
        private static int Prepare(bool remove, ref Result result)
        {
            string operation = remove ? "remove" : "prepare";
            if (!Contract.PayloadValid()) { result.reason = "payload_invalid"; return 12; }
            Reservation request;
            using (var gate = new MachineLock())
            {
                result = Machine.Check(operation, true);
                if (!result.machine_safe || result.office_bitness == null) return 12;
                if (!Machine.OfficeClosed()) { result.status = "busy"; result.reason = "office_busy"; return 10; }
                if (result.status == "conflict" || (!remove && result.status == "disabled")) return 12;
                if (!remove && result.status == "ready") return 0;
                try { request = Reservation.Create(); }
                catch { result.status = "failed"; result.reason = "acl_unavailable"; return 12; }
            }
            int code = Elevation.Run(remove, request);
            if (code == 14)
            {
                result.status = "busy"; result.reason = "child_unconfirmed"; result.machine_safe = false;
                // Preserve reserved/elevated state so future app launches wait.
                return 12;
            }
            using (var gate = new MachineLock())
            {
                result = Machine.Check(operation, true, true, request.request_id);
                if (!result.machine_safe) return code == 0 ? 13 : code;
                request = Reservation.Load(request.request_id);
                request.state = "complete"; request.Save();
                result = Machine.Check(operation, true);
            }
            if (code == 11) { result.status = "failed"; result.reason = "permission_declined"; }
            else if (code != 0) { result.status = "failed"; result.reason = "mutation_failed"; }
            else if (remove) { result.status = "missing"; result.reason = "removed"; }
            return code == 0 || code == 10 || code == 11 || code == 12 || code == 13 ? code : 13;
        }
    }
}
