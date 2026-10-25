using System;
using System.Collections.Generic;
using System.IO;
using System.Security.AccessControl;
using System.Security.Principal;

namespace AegisDesk.PptSetup
{
    /// <summary>기존 요청 예약을 유지하며 부모 복구에서 정상 설치로 인계한다.</summary>
    internal static class ParentRepair
    {
        private static void Enabled()
        {
            if (!Contract.AutoInstallEnabled || !Contract.PayloadValid()) throw new SetupFailure("parent_repair_check", "policy");
        }

        internal static void ValidateRequests(IReservationNamespace lease, string own)
        {
            lease.Recheck();
            string[] siblings = Directory.GetFileSystemEntries(Path.GetDirectoryName(lease.ChildPath));
            if (siblings.Length != 1 || !String.Equals(siblings[0], lease.ChildPath, StringComparison.OrdinalIgnoreCase))
                throw new SetupFailure("parent_repair_check", "policy");
            foreach (string path in Directory.GetFileSystemEntries(lease.ChildPath))
            {
                string id = Path.GetFileNameWithoutExtension(path);
                if (!File.Exists(path) || Path.GetFileName(path) != RequestWriter.Basename(id))
                    throw new SetupFailure("parent_repair_check", "policy");
                var data = RequestWriter.Read(lease, id);
                if ((string)data["state"] != "complete" && id != own) throw new IOException("reservation_unresolved");
            }
        }

        private static string OriginalSid(string id)
        {
            string path = Path.Combine(Machine.Requests, RequestWriter.Basename(id));
            Machine.NoLinks(path);
            if (!File.Exists(path) || new FileInfo(path).Length > 8192) throw new SetupFailure("parent_repair_check", "policy");
            var data = Contract.Read(path);
            if (!Reservation.Valid(data, id)) throw new SetupFailure("parent_repair_check", "policy");
            // 이 값은 lease 선택에만 쓰고 실제 토큰·보호된 요청을 다시 확인한다.
            return (string)data["helper_sid"];
        }

        internal static void ValidateBackupFreeOrigin(DirectorySecurity before, string sid)
        {
            if (before.AreAccessRulesProtected || !ParentSecurity.Legacy(before, sid))
                throw new SetupFailure("parent_repair_recover", "policy");
            var raw = new RawSecurityDescriptor(before.GetSecurityDescriptorBinaryForm(), 0);
            bool inherited = false;
            foreach (GenericAce ace in raw.DiscretionaryAcl) inherited |= (ace.AceFlags & AceFlags.Inherited) != 0;
            if (!inherited) throw new SetupFailure("parent_repair_recover", "policy");
        }

        internal static int Run(ref Result result, out RequestDirectoryLease normal, out Reservation installation)
        {
            Enabled(); normal = null; installation = null;
            RepairPreparationLease special = null;
            Reservation request = null;
            int observedCode = 12;
            try
            {
                using (var gate = new MachineLock())
                using (var legacy = LegacyInstallation.Acquire(true, null))
                {
                    using (var identity = WindowsIdentity.GetCurrent()) special = RepairPreparationLease.Acquire(identity.User.Value);
                    ValidateRequests(special, null);
                    request = Reservation.Create(special);
                    if (Reservation.Load(request.request_id, special).state != "reserved") throw new IOException("reservation_unresolved");
                }
                int code = Elevation.RunRepair(request); observedCode = code;
                if (code == 14) { result.status = "busy"; result.reason = "child_unconfirmed"; result.machine_safe = false; return 12; }
                using (var gate = new MachineLock())
                {
                    if (code == 11)
                    {
                        request = Reservation.Load(request.request_id, special);
                        if (request.state != "reserved" || request.child_pid != null || !RepairBackup.Absent(request.request_id))
                            throw new IOException("reservation_unresolved");
                        request.state = "complete"; request.Save();
                        result.status = "failed"; result.reason = "permission_declined"; result.machine_safe = false; return 11;
                    }
                    if (code != 0) throw new IOException("reservation_unresolved");
                    // 오래된 ACL snapshot을 가진 특수 lease를 writer에 다시 넘기지 않는다.
                    special.Dispose(); special = null;
                    normal = RequestDirectoryLease.AcquireExisting();
                    request = Reservation.Load(request.request_id, normal);
                    ValidateRequests(normal, request.request_id);
                    using (var legacy = LegacyInstallation.Acquire(true, request.request_id))
                    using (var parent = ParentSecurity.OpenSecurity(Path.GetDirectoryName(normal.ChildPath), false))
                    {
                        var backup = RepairBackup.Read(request);
                        if ((string)backup["stage"] != "child_verified" ||
                            ParentSecurity.Inspect(parent, Path.GetDirectoryName(normal.ChildPath), true) != (string)backup["parent_identity"] ||
                            ParentSecurity.Key(ParentSecurity.Read(parent)) != (string)backup["final"] || request.state != "elevated" ||
                            request.child_pid == null || !Reservation.ProcessGone(request.child_pid.Value, request.child_creation_ticks))
                            throw new IOException("reservation_unresolved");
                        result = Machine.Check("prepare", true, true, request.request_id);
                        if (!result.machine_safe || result.status == "disabled" || result.status == "conflict")
                            throw new IOException("reservation_unresolved");
                        request.state = "complete"; request.Save();
                        // 같은 mutex 안에서 다음 설치 예약을 만들어 machine-safe 공백을 없앤다.
                        installation = Reservation.Create(normal); return 0;
                    }
                }
            }
            catch (Exception error)
            {
                var failure = error as SetupFailure;
                if (failure != null) failure.Apply(result);
                result.status = request == null || observedCode == 11 ? "failed" : "busy";
                result.reason = observedCode == 11 ? "permission_declined" : request == null ? "acl_unavailable" : "reservation_unresolved"; result.machine_safe = false;
                if (normal != null) { normal.Dispose(); normal = null; }
                return observedCode == 11 ? 11 : 12;
            }
            finally { if (special != null) special.Dispose(); }
        }

        internal static void Elevated(string id)
        {
            Enabled();
            if (!Machine.Admin) throw new SetupFailure("parent_repair_check", "policy");
            using (var gate = new MachineLock())
            using (var special = RepairPreparationLease.Acquire(OriginalSid(id)))
            {
                Reservation request = Reservation.Load(id, special);
                using (var helper = RepairHelper.Acquire(request))
                using (var legacy = LegacyInstallation.Acquire(false, id))
                {
                    ValidateRequests(special, id); helper.Recheck();
                    request.EnterChild();
                    using (var writable = special.OpenMutationHandle())
                    {
                        var before = ParentSecurity.Read(writable);
                        if (ParentSecurity.Inspect(writable, special.ParentPath, true) != special.ParentIdentity)
                            throw new SetupFailure("parent_repair_verify", "policy");
                        var backup = RepairBackup.Create(request, special.ParentIdentity, before);
                        helper.Recheck(); special.RecheckMutation((string)backup["before"]);
                        if (!Machine.OfficeClosed()) throw new IOException("office_busy");
                        ParentSecurity.Write(writable, ParentSecurity.Transform(before, request.helper_sid, false));
                        helper.Recheck(); special.RecheckMutation((string)backup["intermediate"]);
                        backup["stage"] = "protected"; RepairBackup.Save(backup);
                        helper.Recheck(); special.RecheckMutation((string)backup["intermediate"]);
                        if (!Machine.OfficeClosed()) throw new IOException("office_busy");
                        ParentSecurity.Write(writable, ParentSecurity.Transform(before, request.helper_sid, true));
                        helper.Recheck(); special.RecheckMutation((string)backup["final"]);
                        using (var normal = RequestDirectoryLease.AcquireExisting())
                        {
                            ValidateRequests(normal, id); helper.Recheck();
                            backup["stage"] = "child_verified"; RepairBackup.Save(backup);
                        }
                    }
                }
            }
        }

        internal static void Recover(string id)
        {
            Enabled();
            if (!Machine.Admin || !Machine.OfficeClosed()) throw new SetupFailure("parent_repair_recover", "policy");
            using (var gate = new MachineLock())
            {
                string sid = OriginalSid(id);
                IReservationNamespace lease;
                IDisposable disposable;
                try { var normal = RequestDirectoryLease.AcquireExisting(); lease = normal; disposable = normal; }
                catch (SetupFailure) { var special = RepairPreparationLease.Acquire(sid); lease = special; disposable = special; }
                using (disposable)
                {
                    Reservation request = Reservation.Load(id, lease);
                    if (request.state == "complete" || request.required_version != Contract.Version ||
                        !Reservation.ProcessGone(request.helper_pid, request.helper_creation_ticks) ||
                        (request.child_pid != null && !Reservation.ProcessGone(request.child_pid.Value, request.child_creation_ticks)))
                        throw new SetupFailure("parent_repair_recover", "policy");
                    ValidateRequests(lease, id);
                    using (var legacy = LegacyInstallation.Acquire(false, id))
                    {
                        if (RepairBackup.Absent(id))
                        {
                            var special = lease as RepairPreparationLease;
                            if (special == null) throw new SetupFailure("parent_repair_recover", "policy");
                            using (var parent = ParentSecurity.OpenSecurity(special.ParentPath, false))
                            {
                                var before = ParentSecurity.Read(parent);
                                // protected 원본은 중간 저장과 구별할 수 없어 backup 없이 완료하지 않는다.
                                ValidateBackupFreeOrigin(before, request.helper_sid);
                            }
                            request.state = "complete"; request.Save(); return;
                        }
                        var backup = RepairBackup.Read(request);
                        string parentPath = Path.GetDirectoryName(lease.ChildPath);
                        using (var parent = ParentSecurity.OpenSecurity(parentPath, true))
                        {
                            if (ParentSecurity.Inspect(parent, parentPath, true) != (string)backup["parent_identity"])
                                throw new SetupFailure("parent_repair_recover", "policy");
                            string current = ParentSecurity.Key(ParentSecurity.Read(parent));
                            if (current != (string)backup["before"] && current != (string)backup["intermediate"] && current != (string)backup["final"])
                                throw new SetupFailure("parent_repair_recover", "policy");
                            var special = lease as RepairPreparationLease;
                            if (special != null) special.RecheckMutation(current);
                            if (current != (string)backup["final"])
                            {
                                if (special == null) throw new SetupFailure("parent_repair_recover", "policy");
                                var final = new DirectorySecurity(); final.SetSecurityDescriptorSddlForm((string)backup["final"]);
                                ParentSecurity.Write(parent, final); special.RecheckMutation((string)backup["final"]);
                            }
                            using (var normal = RequestDirectoryLease.AcquireExisting())
                            {
                                ValidateRequests(normal, id);
                                if (!Machine.OfficeClosed()) throw new SetupFailure("parent_repair_recover", "policy");
                                backup["stage"] = "child_verified"; RepairBackup.Save(backup);
                                Reservation.Load(id, normal).Recover();
                            }
                        }
                    }
                }
            }
        }
    }
}
