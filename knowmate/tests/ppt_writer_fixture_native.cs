using System;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using Microsoft.Win32.SafeHandles;

// Test-only metadata intentionally permits link counts other than one.
internal static class WriterFixtureNative
{
    [StructLayout(LayoutKind.Sequential)]
    internal struct Info
    {
        internal uint Attributes; internal System.Runtime.InteropServices.ComTypes.FILETIME Creation, Access, Write;
        internal uint Volume, SizeHigh, SizeLow, Links, High, Low;
        internal string Id { get { return Volume + ":" + High + ":" + Low; } }
    }
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true, EntryPoint = "CreateFileW")]
    internal static extern SafeFileHandle Open(string path, uint access, uint share, IntPtr security, uint creation, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetFileInformationByHandle(SafeFileHandle handle, out Info info);
    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern uint GetSecurityInfo(SafeFileHandle handle, int type, uint information, out IntPtr owner,
        out IntPtr group, out IntPtr dacl, out IntPtr sacl, out IntPtr descriptor);
    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern uint SetSecurityInfo(SafeFileHandle handle, int type, uint information, IntPtr owner,
        IntPtr group, IntPtr dacl, IntPtr sacl);
    [DllImport("advapi32.dll")] private static extern uint GetSecurityDescriptorLength(IntPtr descriptor);
    [DllImport("kernel32.dll")] private static extern IntPtr LocalFree(IntPtr memory);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true, EntryPoint = "CreateDirectoryW")]
    private static extern bool CreateDirectory(string path, IntPtr security);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true, EntryPoint = "RemoveDirectoryW")]
    private static extern bool RemoveDirectory(string path);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true, EntryPoint = "DeleteFileW")]
    private static extern bool DeleteFile(string path);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true, EntryPoint = "CreateHardLinkW")]
    private static extern bool CreateHardLink(string name, string existing, IntPtr security);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true, EntryPoint = "CreateSymbolicLinkW")]
    [return: MarshalAs(UnmanagedType.I1)] private static extern bool CreateSymbolicLink(string name, string existing, uint flags);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool DeviceIoControl(SafeFileHandle handle, uint control, byte[] input, int length,
        IntPtr output, int outputLength, out int returned, IntPtr overlapped);

    internal static SafeFileHandle Pin(string path)
    {
        // OPEN_REPARSE_POINT means metadata/owner checks inspect the link itself.
        SafeFileHandle handle = Open(path, 0x20080, 3, IntPtr.Zero, 3, 0x02200000, IntPtr.Zero);
        int error = Marshal.GetLastWin32Error();
        if (handle.IsInvalid) { handle.Dispose(); throw new Win32Exception(error); }
        return handle;
    }
    internal static Info Inspect(SafeFileHandle handle)
    {
        Info info;
        bool success = GetFileInformationByHandle(handle, out info); int error = Marshal.GetLastWin32Error();
        if (!success) throw new Win32Exception(error);
        return info;
    }
    internal static Info Inspect(string path)
    { using (SafeFileHandle handle = Pin(path)) return Inspect(handle); }
    internal static string Security(SafeFileHandle handle)
    {
        IntPtr owner, group, dacl, sacl, descriptor;
        uint code = GetSecurityInfo(handle, 1, 5, out owner, out group, out dacl, out sacl, out descriptor);
        if (code != 0) throw new Win32Exception((int)code);
        try
        {
            byte[] bytes = new byte[GetSecurityDescriptorLength(descriptor)]; Marshal.Copy(descriptor, bytes, 0, bytes.Length);
            var acl = new FileSecurity(); acl.SetSecurityDescriptorBinaryForm(bytes);
            return acl.GetSecurityDescriptorSddlForm(AccessControlSections.Owner | AccessControlSections.Access);
        }
        finally { LocalFree(descriptor); }
    }
    internal static string Security(string path)
    { using (SafeFileHandle handle = Pin(path)) return Security(handle); }
    internal static bool Owned(SafeFileHandle handle)
    {
        var descriptor = new RawSecurityDescriptor(Security(handle));
        using (WindowsIdentity identity = WindowsIdentity.GetCurrent()) return descriptor.Owner == identity.User;
    }
    internal static void ProtectLink(string path)
    {
        // Own link only: a path-based ACL operation could modify the external target.
        using (SafeFileHandle handle = Open(path, 0x60000, 3, IntPtr.Zero, 3, 0x02200000, IntPtr.Zero))
        {
            int error = Marshal.GetLastWin32Error(); if (handle.IsInvalid) throw new Win32Exception(error);
            var security = new FileSecurity(); security.SetAccessRuleProtection(true, false);
            using (WindowsIdentity identity = WindowsIdentity.GetCurrent())
                security.AddAccessRule(new FileSystemAccessRule(identity.User, FileSystemRights.FullControl,
                    InheritanceFlags.None, PropagationFlags.None, AccessControlType.Allow));
            var raw = new RawSecurityDescriptor(security.GetSecurityDescriptorBinaryForm(), 0);
            byte[] bytes = new byte[raw.DiscretionaryAcl.BinaryLength]; raw.DiscretionaryAcl.GetBinaryForm(bytes, 0);
            GCHandle pinned = GCHandle.Alloc(bytes, GCHandleType.Pinned);
            try
            {
                uint code = SetSecurityInfo(handle, 1, 0x80000004, IntPtr.Zero, IntPtr.Zero, pinned.AddrOfPinnedObject(), IntPtr.Zero);
                if (code != 0) throw new Win32Exception((int)code);
            }
            finally { pinned.Free(); }
        }
    }
    internal static void NewDirectory(string path)
    {
        bool success = CreateDirectory(path, IntPtr.Zero); int error = Marshal.GetLastWin32Error();
        if (!success) throw new Win32Exception(error);
    }
    internal static void Remove(string path, bool directory)
    {
        bool success = directory ? RemoveDirectory(path) : DeleteFile(path); int error = Marshal.GetLastWin32Error();
        if (!success) throw new Win32Exception(error);
    }
    internal static void Hardlink(string name, string existing)
    {
        bool success = CreateHardLink(name, existing, IntPtr.Zero); int error = Marshal.GetLastWin32Error();
        if (!success) throw new Win32Exception(error);
    }
    internal static void Symlink(string name, string existing)
    {
        bool success = CreateSymbolicLink(name, existing, 2); int error = Marshal.GetLastWin32Error();
        if (!success) throw new Win32Exception(error);
    }
    internal static void Junction(string name, string existing, bool create = true)
    {
        if (create) NewDirectory(name);
        string substitute = @"\??\" + Path.GetFullPath(existing);
        byte[] first = System.Text.Encoding.Unicode.GetBytes(substitute), second = System.Text.Encoding.Unicode.GetBytes(existing);
        byte[] buffer = new byte[16 + first.Length + 2 + second.Length + 2];
        Array.Copy(BitConverter.GetBytes(0xA0000003u), 0, buffer, 0, 4);
        Array.Copy(BitConverter.GetBytes((ushort)(buffer.Length - 8)), 0, buffer, 4, 2);
        Array.Copy(BitConverter.GetBytes((ushort)first.Length), 0, buffer, 10, 2);
        Array.Copy(BitConverter.GetBytes((ushort)(first.Length + 2)), 0, buffer, 12, 2);
        Array.Copy(BitConverter.GetBytes((ushort)second.Length), 0, buffer, 14, 2);
        Array.Copy(first, 0, buffer, 16, first.Length); Array.Copy(second, 0, buffer, 18 + first.Length, second.Length);
        using (SafeFileHandle handle = Open(name, 0x40000000, 0, IntPtr.Zero, 3, 0x02200000, IntPtr.Zero))
        {
            int error = Marshal.GetLastWin32Error(); if (handle.IsInvalid) throw new Win32Exception(error);
            int returned; bool success = DeviceIoControl(handle, 0x900A4, buffer, buffer.Length, IntPtr.Zero, 0, out returned, IntPtr.Zero);
            error = Marshal.GetLastWin32Error(); if (!success) throw new Win32Exception(error);
        }
    }
}
