using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Security;
using System.Text.RegularExpressions;

namespace AegisDesk.PptSetup
{
    /// <summary>Bounded diagnostic vocabulary; never contains application data.</summary>
    internal static class ChildDiagnosticContract
    {
        internal static readonly string[] Stages = "anchor_open parent_open parent_policy child_open child_policy request_create request_acl request_commit lease_recheck parent_repair_check parent_repair_backup parent_repair_write parent_repair_verify parent_repair_recover child_payload_check child_admin_check child_machine_lock child_lease_acquire child_reservation_load child_reservation_enter mutation_office_check mutation_reservations_check mutation_lease_check mutation_version_check mutation_recovery mutation_machine_check mutation_ownership_check install_root_create install_root_policy install_journal install_payload_copy install_file_acl install_registry install_record install_verify install_rollback child_complete machine_record_check machine_office_check machine_registry_check".Split(' ');
        internal static readonly string[] Targets = "none request program_files_parent reader_root version_directory journal record payload registry".Split(' ');
        internal static readonly string[] Policies = "unknown_write unsupported_ace unsupported_scope unsupported_mask unsafe_owner missing_trusted_principal required_access_missing namespace_changed reservation_released_retry_required mutation_blocked installation_unknown installation_view_changed root_owner_untrusted root_write_untrusted root_create_failed".Split(' ');
        internal static readonly string[] Errors = "policy Win32Exception UnauthorizedAccessException IOException SecurityException InvalidOperationException ArgumentException NotSupportedException Exception".Split(' ');
        internal static readonly string[] Statuses = "ready missing outdated repairable disabled conflict busy unknown failed".Split(' ');
        internal static readonly string[] Reasons = "registration_ok not_installed version_old registration_missing disabled hkcu_shadow registration_foreign office_busy office_unknown mutex_busy mutex_unavailable reservation_unresolved journal_unresolved prototype_gate payload_invalid acl_unavailable permission_declined launch_failed child_unconfirmed mutation_failed removed check_failed".Split(' ');
        internal static bool Has(string[] values, string value) { return value != null && Array.IndexOf(values, value) >= 0; }
        internal static bool Hex(string value) { return value != null && Regex.IsMatch(value, "\\A[0-9A-F]{8}\\z"); }
        internal static string Phase(string operation)
        { return operation == "elevated-install" ? "install" : operation == "elevated-remove" ? "remove" : operation == "elevated-repair-parent" ? "parent_repair" : null; }
    }

    /// <summary>Only fixed scalar observations cross the elevation boundary.</summary>
    [Serializable]
    internal sealed class ChildDiagnosticError
    {
        internal string Stage, Target, ErrorType, PolicyReason, HResult, NativeStage, NtStatus, IoStatus, CheckStatus, CheckReason;
        internal int? WinError;
        internal ChildDiagnosticError Copy() { return (ChildDiagnosticError)MemberwiseClone(); }
        internal bool Valid()
        {
            if (!ChildDiagnosticContract.Has(ChildDiagnosticContract.Stages, Stage) || !ChildDiagnosticContract.Has(ChildDiagnosticContract.Targets, Target) || !ChildDiagnosticContract.Has(ChildDiagnosticContract.Errors, ErrorType)) return false;
            if (PolicyReason != null && !ChildDiagnosticContract.Has(ChildDiagnosticContract.Policies, PolicyReason)) return false;
            if (WinError.HasValue && (WinError.Value < 0 || ErrorType != "Win32Exception")) return false;
            if (HResult != null && !ChildDiagnosticContract.Hex(HResult)) return false;
            if (ErrorType == "policy" && (WinError.HasValue || HResult != null)) return false;
            if ((CheckStatus == null) != (CheckReason == null) || (CheckStatus != null && (!ChildDiagnosticContract.Has(ChildDiagnosticContract.Statuses, CheckStatus) || !ChildDiagnosticContract.Has(ChildDiagnosticContract.Reasons, CheckReason)))) return false;
            if (NativeStage == null) return NtStatus == null && IoStatus == null;
            return Stage == "request_commit" && ChildDiagnosticContract.Hex(NtStatus) && (IoStatus == null || ChildDiagnosticContract.Hex(IoStatus)) &&
                ((NativeStage == "commit_return" && ErrorType == "IOException" && WinError == null) || (NtStatus == "00000000" && (NativeStage == "commit_verify" || NativeStage == "commit_close")));
        }
        internal Dictionary<string, object> Values()
        {
            return new Dictionary<string, object> { {"stage",Stage},{"target",Target},{"error_type",ErrorType},{"policy_reason",PolicyReason},
                {"winerror",WinError},{"hresult",HResult},{"native_stage",NativeStage},{"ntstatus",NtStatus},{"io_status",IoStatus},{"check_status",CheckStatus},{"check_reason",CheckReason} };
        }
        internal static ChildDiagnosticError From(Exception error, string stage, string target)
        {
            var result = new ChildDiagnosticError { Stage = stage, Target = target };
            var failure = error as SetupFailure;
            if (failure != null)
            {
                result.Stage = failure.Stage; result.ErrorType = failure.ErrorType; result.WinError = failure.WinError;
                result.HResult = failure.OriginHResult; result.PolicyReason = failure.PolicyReason;
                result.NativeStage = failure.NativeStage; result.NtStatus = failure.NtStatus; result.IoStatus = failure.IoStatus;
            }
            else
            {
                var native = error as Win32Exception;
                result.ErrorType = native != null ? "Win32Exception" : error is UnauthorizedAccessException ? "UnauthorizedAccessException" :
                    error is SecurityException ? "SecurityException" : error is InvalidOperationException ? "InvalidOperationException" :
                    error is ArgumentException ? "ArgumentException" : error is NotSupportedException ? "NotSupportedException" : error is IOException ? "IOException" : "Exception";
                result.HResult = unchecked((uint)error.HResult).ToString("X8");
                if (native != null && native.NativeErrorCode >= 0) result.WinError = native.NativeErrorCode;
            }
            return result;
        }
    }

    /// <summary>A child result never carries a machine-safety decision.</summary>
    internal sealed class ChildDiagnosticObservation
    {
        internal string Phase, Transport = "unavailable", LastStage, LastTarget;
        internal uint? ExitCode;
        internal ChildDiagnosticError Primary, Secondary;
        internal Dictionary<string, object> Values()
        {
            return new Dictionary<string, object> { {"phase",Phase},{"transport",Transport},{"child_exit_code",ExitCode},
                {"last_stage",LastStage},{"last_target",LastTarget},{"primary",Primary == null ? null : Primary.Values()},{"secondary",Secondary == null ? null : Secondary.Values()} };
        }
        internal bool ValidReceived()
        {
            return ExitCode.HasValue && ChildDiagnosticContract.Has(ChildDiagnosticContract.Stages,LastStage) && ChildDiagnosticContract.Has(ChildDiagnosticContract.Targets,LastTarget) &&
                (ExitCode == 0 ? Primary == null && Secondary == null : Primary != null && Primary.Valid() && (Secondary == null || Secondary.Valid()));
        }
    }
}
