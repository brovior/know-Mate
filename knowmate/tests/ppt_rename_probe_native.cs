using System;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

// Experimental test executable only. This is not an installer commit fallback.
internal static class RenameProbeNative
{
    internal sealed class ProbeFailure : Exception
    {
        internal readonly string Code;
        internal ProbeFailure(string code) { Code = code; }
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct IoStatusBlock { public IntPtr Status; public UIntPtr Information; }
    [StructLayout(LayoutKind.Sequential)]
    private struct RenameInformation
    {
        public byte ReplaceIfExists;
        public IntPtr RootDirectory;
        public uint FileNameLength;
        public ushort FileName;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct FileInfo
    {
        public uint Attributes;
        public System.Runtime.InteropServices.ComTypes.FILETIME Creation, Access, Write;
        public uint Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
    }
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateFile(string path, uint access, uint share,
        IntPtr security, uint disposition, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetFileInformationByHandle(SafeFileHandle handle, out FileInfo info);
    [DllImport("ntdll.dll", ExactSpelling = true)]
    private static extern int NtSetInformationFile(SafeFileHandle handle, IntPtr status,
        IntPtr information, uint length, int informationClass);

    internal static void Require(bool value, string code)
    { if (!value) throw new ProbeFailure(code); }

    internal static SafeFileHandle OpenSource(string path)
    {
        // DELETE | READ_CONTROL | FILE_READ_ATTRIBUTES | SYNCHRONIZE.
        // No OVERLAPPED flag: the native rename is synchronous. Only this source
        // file uses share 7; RequestDirectoryLease remains unchanged, share 1.
        var handle = CreateFile(path, 0x00130080, 7, IntPtr.Zero, 3, 0x00200000, IntPtr.Zero);
        if (handle.IsInvalid) { int code = Marshal.GetLastWin32Error(); handle.Dispose(); throw new Win32Exception(code); }
        return handle;
    }

    private static FileInfo Info(SafeFileHandle handle)
    {
        FileInfo info;
        if (!GetFileInformationByHandle(handle, out info)) throw new Win32Exception(Marshal.GetLastWin32Error());
        Require((info.Attributes & (0x10 | 0x400)) == 0 && info.Links == 1, "source_not_single_regular_file");
        return info;
    }

    internal static string Identity(SafeFileHandle handle)
    {
        FileInfo info = Info(handle);
        return info.Volume + ":" + info.IndexHigh + ":" + info.IndexLow;
    }

    internal static string Identity(string path)
    {
        using (SafeFileHandle handle = CreateFile(path, 0x80, 7, IntPtr.Zero, 3, 0x00200000, IntPtr.Zero))
        {
            if (handle.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error());
            return Identity(handle);
        }
    }

    internal static void DirectoryWriteProbe(string path, bool leased)
    {
        using (SafeFileHandle handle = CreateFile(path, 2, 7, IntPtr.Zero, 3, 0x02200000, IntPtr.Zero))
        {
            int code = handle.IsInvalid ? Marshal.GetLastWin32Error() : 0;
            Console.WriteLine("PPT_RENAME_SHARE result=" + (handle.IsInvalid ? "denied" : "opened")
                + " winerror=" + code + " leased=" + (leased ? "1" : "0"));
            Console.Out.Flush();
            Require(leased ? handle.IsInvalid && code == 32 : !handle.IsInvalid, "directory_write_sharing_probe");
        }
    }

    internal static int Rename(SafeFileHandle source, string basename, bool replace)
    {
        Require(basename == "request.json", "rename_basename_not_fixed");
        int rootOffset = (int)Marshal.OffsetOf(typeof(RenameInformation), "RootDirectory");
        int lengthOffset = (int)Marshal.OffsetOf(typeof(RenameInformation), "FileNameLength");
        int nameOffset = (int)Marshal.OffsetOf(typeof(RenameInformation), "FileName");
        Require(rootOffset == IntPtr.Size && lengthOffset == 2 * IntPtr.Size && nameOffset == 2 * IntPtr.Size + 4,
            "rename_layout_invalid");
        Require(Marshal.SizeOf(typeof(RenameInformation)) == (IntPtr.Size == 8 ? 24 : 16)
            && Marshal.SizeOf(typeof(IoStatusBlock)) == 2 * IntPtr.Size, "native_layout_invalid");
        byte[] name = Encoding.Unicode.GetBytes(basename);
        int size = Marshal.SizeOf(typeof(RenameInformation)) + name.Length;
        IntPtr buffer = Marshal.AllocHGlobal(size);
        IntPtr ioBuffer = IntPtr.Zero;
        try
        {
            int ioSize = Marshal.SizeOf(typeof(IoStatusBlock));
            ioBuffer = Marshal.AllocHGlobal(ioSize);
            Marshal.Copy(new byte[ioSize], 0, ioBuffer, ioSize);
            Marshal.Copy(new byte[size], 0, buffer, size);
            Marshal.WriteByte(buffer, replace ? (byte)1 : (byte)0);
            Marshal.WriteIntPtr(buffer, rootOffset, IntPtr.Zero);
            Marshal.WriteInt32(buffer, lengthOffset, name.Length);
            Marshal.Copy(name, 0, IntPtr.Add(buffer, nameOffset), name.Length);
            int status = NtSetInformationFile(source, ioBuffer, buffer, (uint)size, 10);
            // STATUS_PENDING is explicitly not success. No asynchronous request
            // is expected from this synchronous source handle; do not wait forever.
            if (status == 0x103)
            {
                // Do not free buffers or dispose handles while an unexpected
                // pending operation might still reference them. Terminate only
                // this isolated probe process; this is never installer behavior.
                Console.WriteLine("PPT_RENAME_PENDING ntstatus=00000103 io_status=unavailable io_information=unavailable");
                Console.WriteLine("PPT_RENAME_ERROR kind=contract code=native_pending_unproven stage=native_rename_call hresult=80004005 winerror=none");
                Console.WriteLine("PPT_RENAME_RESULT FAIL"); Console.Out.Flush();
                Environment.Exit(1);
            }
            var io = (IoStatusBlock)Marshal.PtrToStructure(ioBuffer, typeof(IoStatusBlock));
            Console.WriteLine("PPT_RENAME_STATUS ntstatus=" + unchecked((uint)status).ToString("X8")
                + " io_status=" + unchecked((uint)io.Status.ToInt64()).ToString("X8")
                + " io_information=" + io.Information.ToUInt64());
            Console.Out.Flush();
            if (status == 0) Require(io.Status == IntPtr.Zero, "native_io_status_not_success");
            return status;
        }
        finally { if (ioBuffer != IntPtr.Zero) Marshal.FreeHGlobal(ioBuffer); Marshal.FreeHGlobal(buffer); }
    }
}
