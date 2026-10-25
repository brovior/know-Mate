using System;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace AegisDesk.PptSetup
{
    /// <summary>예약에 기록된 원래 helper의 실제 토큰과 실행 이미지를 고정한다.</summary>
    internal sealed class RepairHelper : IDisposable
    {
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetProcessTimes(IntPtr handle, out long creation, out long exit, out long kernel, out long user);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool QueryFullProcessImageName(IntPtr handle, uint flags, StringBuilder path, ref uint length);
        [DllImport("kernel32.dll")]
        private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
        [DllImport("kernel32.dll")]
        private static extern bool CloseHandle(IntPtr handle);
        private IntPtr process;
        private FileStream image;

        internal static RepairHelper Acquire(Reservation request)
        {
            var result = new RepairHelper();
            try
            {
                if (request.required_version != Contract.Version || request.state != "reserved" || request.child_pid != null)
                    throw new SetupFailure("parent_repair_check", "policy");
                result.process = OpenProcess(0x00101000, false, request.helper_pid);
                if (result.process == IntPtr.Zero) throw new SetupFailure("parent_repair_check", "policy");
                long creation, exit, kernel, user;
                if (!GetProcessTimes(result.process, out creation, out exit, out kernel, out user) ||
                    DateTime.FromFileTimeUtc(creation).Ticks.ToString() != request.helper_creation_ticks)
                    throw new SetupFailure("parent_repair_check", "policy");
                IntPtr token;
                if (!OpenProcessToken(result.process, 8, out token)) throw new SetupFailure("parent_repair_check", "policy");
                try
                {
                    using (var identity = new WindowsIdentity(token))
                        if (identity.User.Value != request.helper_sid) throw new SetupFailure("parent_repair_check", "policy");
                }
                finally { CloseHandle(token); }
                var path = new StringBuilder(32768); uint length = (uint)path.Capacity;
                string expected = Assembly.GetExecutingAssembly().Location;
                if (!QueryFullProcessImageName(result.process, 0, path, ref length) ||
                    !String.Equals(Path.GetFullPath(path.ToString()), Path.GetFullPath(expected), StringComparison.OrdinalIgnoreCase))
                    throw new SetupFailure("parent_repair_check", "policy");
                Machine.NoLinks(expected);
                result.image = new FileStream(expected, FileMode.Open, FileAccess.Read, FileShare.Read);
                if (!Contract.PayloadValid()) throw new SetupFailure("parent_repair_check", "policy");
                result.Recheck(); return result;
            }
            catch { result.Dispose(); throw; }
        }

        internal void Recheck()
        { if (process == IntPtr.Zero || WaitForSingleObject(process, 0) != 258) throw new SetupFailure("parent_repair_verify", "policy"); }

        public void Dispose()
        { if (image != null) { image.Dispose(); image = null; } if (process != IntPtr.Zero) { CloseHandle(process); process = IntPtr.Zero; } }
    }
}
