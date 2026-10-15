// Pure managed production metadata/lifecycle contract; no registration or Office.
using System;
using System.Reflection;
using System.Runtime.InteropServices;
public sealed class ReaderHost { }
public sealed class ReaderAddin { public object Object { get; set; } }
public static class ReaderHarness
{
    [STAThread]
    public static int Main(string[] args)
    {
        Assembly assembly = Assembly.LoadFrom(args[0]);
        if (assembly.GetName().Version.ToString() != "1.0.1.0") throw new Exception("version_mismatch");
        Type type = assembly.GetType("AegisDesk.PptReader.Connect", true);
        if (type.GUID != new Guid("287A80D2-804A-46D3-A0E9-CA31C6FC3D09")) throw new Exception("clsid_mismatch");
        if (((ProgIdAttribute)Attribute.GetCustomAttribute(type, typeof(ProgIdAttribute))).Value != "AegisDesk.PptReader")
            throw new Exception("progid_mismatch");
        Type reader = assembly.GetType("AegisDesk.PptReader.IPptReader", true);
        if (reader.GUID != new Guid("65152412-DFD3-47EC-8346-3F2675ED0C15")) throw new Exception("reader_iid_mismatch");
        Type office = assembly.GetType("AegisDesk.PptReader.IDTExtensibility2", true);
        if (office.GUID != new Guid("B65AD801-ABAF-11D0-BB8B-00A0C90F2744")
            || ((InterfaceTypeAttribute)Attribute.GetCustomAttribute(office, typeof(InterfaceTypeAttribute))).Value != ComInterfaceType.InterfaceIsDual)
            throw new Exception("office_abi_mismatch");
        object instance = Activator.CreateInstance(type);
        try { type.GetMethod("ReaderVersion").Invoke(instance, null); throw new Exception("disconnected_accepted"); }
        catch (TargetInvocationException exc) { if (!(exc.InnerException is COMException)) throw; }
        var addin = new ReaderAddin();
        type.GetMethod("OnConnection").Invoke(instance, new object[] { new ReaderHost(), 1, addin, null });
        if (!Object.ReferenceEquals(instance, addin.Object)
            || (int)type.GetMethod("ProtocolVersion").Invoke(instance, null) != 2
            || (string)type.GetMethod("ReaderVersion").Invoke(instance, null) != "1.0.1.0")
            throw new Exception("production_connection_mismatch");
        type.GetMethod("OnDisconnection").Invoke(instance, new object[] { 1, null });
        Console.WriteLine("PASS production identity/version/dual ABI/managed lifecycle; native Office not verified");
        return 0;
    }
}
