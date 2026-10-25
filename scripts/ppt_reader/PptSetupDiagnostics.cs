using System;
using System.ComponentModel;
using System.IO;
using System.Security;

namespace AegisDesk.PptSetup
{
    // Body-free, bounded diagnostic vocabulary shared with Python's parser.
    internal sealed class SetupFailure : IOException
    {
        internal readonly string Stage, ErrorType;
        internal readonly int? WinError;
        internal string NativeStage, NtStatus, IoStatus, PolicyReason;
        internal string OriginHResult;
        internal SetupFailure(string stage, string type, int? winerror = null) : base("acl_unavailable")
        { Stage = stage; ErrorType = type; WinError = winerror; }
        internal static SetupFailure From(string stage, Exception error)
        {
            var existing = error as SetupFailure;
            if (existing != null) return existing;
            var native = error as Win32Exception;
            if (native != null) return new SetupFailure(stage, "Win32Exception", native.NativeErrorCode >= 0 ? (int?)native.NativeErrorCode : null) { OriginHResult=unchecked((uint)error.HResult).ToString("X8") };
            string type = error is UnauthorizedAccessException ? "UnauthorizedAccessException" :
                error is SecurityException ? "SecurityException" : error is InvalidOperationException ? "InvalidOperationException" :
                error is ArgumentException ? "ArgumentException" : error is NotSupportedException ? "NotSupportedException" :
                error is IOException ? "IOException" : "Exception";
            // HResult is deliberately never truncated into a claimed Win32 code.
            var failure=new SetupFailure(stage, type) { OriginHResult=unchecked((uint)error.HResult).ToString("X8") };
            ChildDiagnostics.Pending(error);ChildDiagnostics.Attach(failure);return failure;
        }
        internal static SetupFailure Native(string nativeStage, uint status, uint? ioStatus)
        { return new SetupFailure("request_commit", "IOException") { NativeStage = nativeStage, NtStatus = status.ToString("X8"), IoStatus = ioStatus.HasValue ? ioStatus.Value.ToString("X8") : null }; }
        internal static SetupFailure WithNative(Exception error, string nativeStage, uint status, uint? ioStatus)
        {
            SetupFailure failure = From("request_commit", error);
            failure.NativeStage = nativeStage; failure.NtStatus = status.ToString("X8");
            failure.IoStatus = ioStatus.HasValue ? ioStatus.Value.ToString("X8") : null; ChildDiagnostics.AttachNative(failure);return failure;
        }
        internal void Apply(Result result)
        { result.stage = Stage; result.error_type = ErrorType; result.winerror = WinError;
          result.native_stage = NativeStage; result.ntstatus = NtStatus; result.io_status = IoStatus; result.policy_reason = PolicyReason; }
    }
}
