// Test-only native-layout COM object. This pointer is never a managed CCW.
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

internal sealed class NativeMock : IDisposable
{
    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate int Query(IntPtr self, ref Guid iid, out IntPtr result);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate int Ref(IntPtr self);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate int InfoCount(IntPtr self, out uint count);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate int Info(IntPtr self, uint index, uint lcid, out IntPtr result);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate int Names(IntPtr self, IntPtr iid, IntPtr names, uint count, uint lcid, IntPtr ids);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate int Invoke(IntPtr self, int id, IntPtr iid, uint lcid, ushort flags,
        IntPtr parameters, IntPtr result, IntPtr exception, IntPtr error);

    public readonly IntPtr Pointer;
    private readonly IntPtr table;
    private readonly Delegate[] roots;
    public readonly Guid Supported;
    public int References = 1, Adds, Releases, UnexpectedCalls;
    public readonly List<Guid> Queries = new List<Guid>();
    public NativeMock(string iid)
    {
        Supported = new Guid(iid);
        roots = new Delegate[] {
            new Query(QueryInterface), new Ref(AddRef), new Ref(Release),
            new InfoCount(GetTypeInfoCount), new Info(GetTypeInfo),
            new Names(GetNames), new Invoke(Dispatch)
        };
        table = Marshal.AllocHGlobal(IntPtr.Size * roots.Length);
        for (int i = 0; i < roots.Length; i++)
            Marshal.WriteIntPtr(table, i * IntPtr.Size, Marshal.GetFunctionPointerForDelegate(roots[i]));
        Pointer = Marshal.AllocHGlobal(IntPtr.Size);
        Marshal.WriteIntPtr(Pointer, table);
    }
    private int QueryInterface(IntPtr self, ref Guid iid, out IntPtr result)
    {
        Queries.Add(iid);
        if (iid == Supported || iid == new Guid("00000000-0000-0000-C000-000000000046")
            || iid == new Guid("00020400-0000-0000-C000-000000000046"))
        { result = Pointer; AddRef(self); return 0; }
        result = IntPtr.Zero; return unchecked((int)0x80004002);
    }
    private int AddRef(IntPtr self) { Adds++; return ++References; }
    private int Release(IntPtr self) { Releases++; return --References; }
    private int GetTypeInfoCount(IntPtr self, out uint count) { UnexpectedCalls++; count = 0; return 0; }
    private int GetTypeInfo(IntPtr self, uint index, uint lcid, out IntPtr result)
    { UnexpectedCalls++; result = IntPtr.Zero; return unchecked((int)0x80004001); }
    private int GetNames(IntPtr self, IntPtr iid, IntPtr names, uint count, uint lcid, IntPtr ids)
    { UnexpectedCalls++; return unchecked((int)0x80020006); }
    private int Dispatch(IntPtr self, int id, IntPtr iid, uint lcid, ushort flags,
        IntPtr parameters, IntPtr result, IntPtr exception, IntPtr error)
    { UnexpectedCalls++; return unchecked((int)0x80020003); }
    public void Dispose()
    {
        if (References != 1 || Adds != Releases)
            throw new InvalidOperationException("native_reference_leak:" + References + ":" + Adds + ":" + Releases);
        Marshal.FreeHGlobal(Pointer); Marshal.FreeHGlobal(table);
        GC.KeepAlive(roots);
    }
}
