using System;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace AegisDesk.PptSetup
{
    // One native handle owner; FileStream's SafeFileHandle wrapper never owns it.
    // Explicit CloseHandle provides a checked close result without double close.
    internal sealed class RequestFile
    {
        [StructLayout(LayoutKind.Sequential)]
        internal struct Identity
        {
            public uint attributes;
            public System.Runtime.InteropServices.ComTypes.FILETIME creation, access, write;
            public uint volume, sizeHigh, sizeLow, links, indexHigh, indexLow;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct SecurityAttributes
        { public int length; public IntPtr descriptor; [MarshalAs(UnmanagedType.Bool)] public bool inherit; }
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateFile(string path, uint access, uint share, ref SecurityAttributes security, uint disposition, uint flags, IntPtr template);
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)] private static extern bool CloseHandle(IntPtr handle);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetFileInformationByHandle(SafeFileHandle handle, out Identity info);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern uint GetFinalPathNameByHandle(SafeFileHandle handle, StringBuilder path, uint size, uint flags);
        [DllImport("advapi32.dll")]
        private static extern uint GetSecurityInfo(SafeFileHandle handle, int type, uint information,
            out IntPtr owner, out IntPtr group, out IntPtr dacl, out IntPtr sacl, out IntPtr descriptor);
        [DllImport("advapi32.dll")] private static extern uint GetSecurityDescriptorLength(IntPtr descriptor);
        [DllImport("kernel32.dll")] private static extern IntPtr LocalFree(IntPtr memory);
        private IntPtr native;
        internal readonly SafeFileHandle Handle;
        internal readonly FileStream Stream;
        private RequestFile(IntPtr native, bool write)
        {
            this.native = native; Handle = new SafeFileHandle(native, false);
            try { Stream = new FileStream(Handle, write ? FileAccess.ReadWrite : FileAccess.Read, 4096, false); }
            catch { Close(); throw; }
        }
        internal static RequestFile Open(string path, FileSecurity security)
        {
            byte[] descriptor = security == null ? null : security.GetSecurityDescriptorBinaryForm();
            GCHandle pin = default(GCHandle);
            try
            {
                var attributes = new SecurityAttributes { length = Marshal.SizeOf(typeof(SecurityAttributes)) };
                if (descriptor != null) { pin = GCHandle.Alloc(descriptor, GCHandleType.Pinned); attributes.descriptor = pin.AddrOfPinnedObject(); }
                IntPtr handle = CreateFile(path, security == null ? 0x80120080 : 0xC0130080, 1, ref attributes,
                    security == null ? 3u : 1u, 0x00200000, IntPtr.Zero);
                if (handle == new IntPtr(-1)) throw new Win32Exception(Marshal.GetLastWin32Error());
                return new RequestFile(handle, security != null);
            }
            finally { if (pin.IsAllocated) pin.Free(); }
        }
        internal Identity Inspect(string expectedPath)
        {
            Identity info;
            if (!GetFileInformationByHandle(Handle, out info)) throw new Win32Exception(Marshal.GetLastWin32Error());
            if ((info.attributes & (0x10 | 0x400 | 1)) != 0 || info.links != 1) throw new IOException("request_not_regular");
            var path = new StringBuilder(32768);
            uint size = GetFinalPathNameByHandle(Handle, path, (uint)path.Capacity, 0);
            if (size == 0) throw new Win32Exception(Marshal.GetLastWin32Error());
            if (size >= path.Capacity || !path.ToString().StartsWith(@"\\?\", StringComparison.Ordinal) ||
                !String.Equals(path.ToString().Substring(4), Path.GetFullPath(expectedPath), StringComparison.OrdinalIgnoreCase))
                throw new IOException("request_path");
            return info;
        }
        internal FileSecurity Security()
        {
            IntPtr owner, group, dacl, sacl, descriptor;
            uint code = GetSecurityInfo(Handle, 1, 5, out owner, out group, out dacl, out sacl, out descriptor);
            if (code != 0) throw new Win32Exception((int)code);
            try
            {
                uint length = GetSecurityDescriptorLength(descriptor);
                if (length == 0 || length > 65536) throw new IOException("request_security");
                var bytes = new byte[(int)length]; Marshal.Copy(descriptor, bytes, 0, bytes.Length);
                var security = new FileSecurity(); security.SetSecurityDescriptorBinaryForm(bytes); return security;
            }
            finally { LocalFree(descriptor); }
        }
        internal byte[] Read()
        {
            if (Stream.Length == 0 || Stream.Length > 8192) throw new IOException("reservation_size");
            Stream.Position = 0; var bytes = new byte[(int)Stream.Length]; int offset = 0;
            while (offset != bytes.Length) { int count = Stream.Read(bytes, offset, bytes.Length - offset); if (count == 0) throw new IOException("reservation_size"); offset += count; }
            return bytes;
        }
        internal void Close()
        {
            if (native == IntPtr.Zero) return;
            IntPtr closing = native; native = IntPtr.Zero;
            try { if (Stream != null) Stream.Dispose(); }
            finally
            {
                Handle.SetHandleAsInvalid();
                if (!CloseHandle(closing)) throw new Win32Exception(Marshal.GetLastWin32Error());
            }
        }
        internal static bool Same(Identity first, Identity second)
        { return first.volume == second.volume && first.indexHigh == second.indexHigh && first.indexLow == second.indexLow; }
    }
}
