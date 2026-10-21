#if AEGIS_PPT_LIFETIME_DIAGNOSTIC
#if AEGIS_PPT_DEFERRED_DIAGNOSTIC || AEGIS_PPT_EXTERNAL_DIAGNOSTIC || AEGIS_PPT_CAPTURE_DIAGNOSTIC
#error Lifetime diagnostics cannot include the shutdown/event diagnostic variants.
#endif
using System;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Threading;

namespace AegisDesk.PptReader
{
    /// <summary>Separate identity-only addition to the shared fast Reader contract.</summary>
    [ComVisible(true), Guid("F71674D8-CB02-405A-87D3-59177E1A1487")]
    [InterfaceType(ComInterfaceType.InterfaceIsDual)]
    public interface IPptReaderLifetimeDiagnostic
    {
        [DispId(1)] int ProtocolVersion();
        [DispId(2)] string ExtractSlide([MarshalAs(UnmanagedType.BStr)] string presentationPath, int slide);
        [DispId(3)] string ReaderVersion();
        [DispId(4)] string GetHostProcessIdentity();
    }

    public sealed partial class Connect
    {
        [StructLayout(LayoutKind.Sequential)]
        private struct LifetimeFileTime { internal uint Low; internal uint High; }

        [DllImport("kernel32.dll")]
        private static extern IntPtr GetCurrentProcess();
        [DllImport("kernel32.dll")]
        private static extern uint GetCurrentProcessId();
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool GetProcessTimes(IntPtr process, out LifetimeFileTime creation,
            out LifetimeFileTime exit, out LifetimeFileTime kernel, out LifetimeFileTime user);

        private void ConnectLifetimeHost(object app, object addin)
        {
            DisconnectHost();
            if (Thread.CurrentThread.GetApartmentState() != ApartmentState.STA)
                throw new COMException("probe_requires_office_sta");
            if (app == null || addin == null) throw new COMException("probe_missing_host");
            application = app;
            mainThread = Thread.CurrentThread.ManagedThreadId;
            try { ((dynamic)addin).Object = this; }
            catch (Exception error)
            {
                DisconnectHost();
                throw new COMException("probe_connect_failed", Marshal.GetHRForException(error));
            }
        }

        /// <summary>Native host identity only; it grants no document/quit authority.</summary>
        public string GetHostProcessIdentity()
        {
            RequireHostThread();
            LifetimeFileTime creation, exit, kernel, user;
            if (!GetProcessTimes(GetCurrentProcess(), out creation, out exit, out kernel, out user))
                throw new COMException("lifetime_host_identity_failed", Marshal.GetHRForLastWin32Error());
            ulong value = ((ulong)creation.High << 32) | creation.Low;
            uint pid = GetCurrentProcessId();
            if (pid == 0 || value == 0) throw new COMException("lifetime_host_identity_failed");
            return "{\"pid\":" + pid.ToString(CultureInfo.InvariantCulture)
                + ",\"creation_filetime\":" + value.ToString(CultureInfo.InvariantCulture) + "}";
        }
    }
}
#endif
