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
        internal SetupFailure(string stage, string type, int? winerror = null) : base("acl_unavailable")
        { Stage = stage; ErrorType = type; WinError = winerror; }
        internal static SetupFailure From(string stage, Exception error)
        {
            var existing = error as SetupFailure;
            if (existing != null) return existing;
            var native = error as Win32Exception;
            if (native != null) return new SetupFailure(stage, "Win32Exception", native.NativeErrorCode >= 0 ? (int?)native.NativeErrorCode : null);
            string type = error is UnauthorizedAccessException ? "UnauthorizedAccessException" :
                error is SecurityException ? "SecurityException" : error is InvalidOperationException ? "InvalidOperationException" :
                error is ArgumentException ? "ArgumentException" : error is NotSupportedException ? "NotSupportedException" :
                error is IOException ? "IOException" : "Exception";
            // HResult is deliberately never truncated into a claimed Win32 code.
            return new SetupFailure(stage, type);
        }
        internal void Apply(Result result)
        { result.stage = Stage; result.error_type = ErrorType; result.winerror = WinError; }
    }
}
