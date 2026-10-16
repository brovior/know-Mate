using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

namespace AegisDesk.PptSetup
{
    internal static partial class RequestWriter
    {
        [StructLayout(LayoutKind.Sequential)]
        private struct RenameInformation
        { public byte ReplaceIfExists; public IntPtr RootDirectory; public uint FileNameLength; public ushort FileName; }
        [StructLayout(LayoutKind.Sequential)]
        private struct IoStatusBlock { public IntPtr Status; public UIntPtr Information; }
        [DllImport("ntdll.dll", ExactSpelling = true)]
        private static extern uint NtSetInformationFile(IntPtr source, IntPtr io, IntPtr information, uint length, int informationClass);
        static partial void TraceRequest(string stage);
        // Implemented only in isolated test executables; shipping calls disappear.
        static partial void InterceptNative(IntPtr source, IntPtr io, IntPtr rename, uint length, ref bool handled, ref uint status);
        private const uint IoSentinel = 0x7fffffff;

        internal static void NativeLayout()
        {
            if ((int)Marshal.OffsetOf(typeof(RenameInformation), "RootDirectory") != IntPtr.Size ||
                (int)Marshal.OffsetOf(typeof(RenameInformation), "FileNameLength") != 2 * IntPtr.Size ||
                (int)Marshal.OffsetOf(typeof(RenameInformation), "FileName") != 2 * IntPtr.Size + 4 ||
                Marshal.SizeOf(typeof(RenameInformation)) != (IntPtr.Size == 8 ? 24 : 16) ||
                Marshal.SizeOf(typeof(IoStatusBlock)) != 2 * IntPtr.Size) throw new IOException("request_native_layout");
        }
        private static void ParkUnconfirmed(RequestFile source, IntPtr io, IntPtr rename, uint status, uint? ioStatus)
        {
            TraceRequest("native_unconfirmed");
            // This is a contract violation, not a supported async completion path.
            // Keep this stack (caller mutex + lease included), unmanaged buffers,
            // and FileStream alive. No return, disposal, retry, wait-on-file or kill.
            while (true)
            {
                try { Thread.Sleep(Timeout.Infinite); }
                catch (ThreadInterruptedException) { }
                GC.KeepAlive(source); GC.KeepAlive(io); GC.KeepAlive(rename);
                GC.KeepAlive(status); GC.KeepAlive(ioStatus);
            }
        }
        private static void Commit(RequestFile source, string id, bool replace, out uint status, out uint? ioStatus)
        {
            NativeLayout();
            byte[] name = Encoding.Unicode.GetBytes(Basename(id));
            int size = Marshal.SizeOf(typeof(RenameInformation)) + name.Length;
            IntPtr rename = IntPtr.Zero, io = IntPtr.Zero;
            status = IoSentinel; ioStatus = null;
            try
            {
                rename = Marshal.AllocHGlobal(size);
                io = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(IoStatusBlock)));
                Marshal.Copy(new byte[size], 0, rename, size);
                Marshal.Copy(new byte[2 * IntPtr.Size], 0, io, 2 * IntPtr.Size);
                Marshal.WriteIntPtr(io, new IntPtr((long)IoSentinel));
                Marshal.WriteByte(rename, replace ? (byte)1 : (byte)0);
                Marshal.WriteInt32(rename, 2 * IntPtr.Size, name.Length);
                Marshal.Copy(name, 0, IntPtr.Add(rename, 2 * IntPtr.Size + 4), name.Length);
                TraceRequest("commit_call");
                bool handled = false;
                InterceptNative(source.Handle.DangerousGetHandle(), io, rename, (uint)size, ref handled, ref status);
                if (!handled) status = NtSetInformationFile(source.Handle.DangerousGetHandle(), io, rename, (uint)size, 10);
                uint actualIo = unchecked((uint)Marshal.ReadIntPtr(io).ToInt64());
                ioStatus = actualIo == IoSentinel ? (uint?)null : actualIo;
                TraceRequest("commit_return");
                if (status == 0x103 || (status == 0 && ioStatus != 0)) ParkUnconfirmed(source, io, rename, status, ioStatus);
                if (status != 0) throw SetupFailure.Native("commit_return", status, ioStatus);
            }
            finally
            {
                if (io != IntPtr.Zero) Marshal.FreeHGlobal(io);
                if (rename != IntPtr.Zero) Marshal.FreeHGlobal(rename);
            }
        }
    }
}
