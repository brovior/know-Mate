// Managed state-machine tests only; native Office/event ABI remains unverified.
using System;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Threading;

public sealed class ReaderCollection
{
    public int Value;
    public bool Fail;
    public bool Fatal;
    public int Count { get { if (Fatal) throw new COMException("fatal_count", unchecked((int)0x80010108)); if (Fail) throw new Exception("count_failure"); return Value; } }
}
public sealed class ReaderHost
{
    public ReaderCollection Presentations { get; set; }
    public ReaderCollection ProtectedViewWindows { get; set; }
    public ReaderCollection SlideShowWindows { get; set; }
    public ReaderCommands CommandBars { get; private set; }
    public Action OnQuit;
    public int QuitCount;
    public bool FailAlerts;
    private int alerts;
    public int DisplayAlerts
    {
        get { return alerts; }
        set { if (FailAlerts) throw new Exception("alerts_failure"); alerts = value; }
    }
    public ReaderHost()
    {
        CommandBars = new ReaderCommands(this);
        Presentations = new ReaderCollection(); ProtectedViewWindows = new ReaderCollection();
        SlideShowWindows = new ReaderCollection();
    }
    public void Quit() { throw new Exception("external_quit_forbidden"); }
    public void ExitCommand() { QuitCount++; if (OnQuit != null) OnQuit(); }
}
public sealed class ReaderCommands {
    private readonly ReaderHost host;
    public ReaderCommands(ReaderHost value) { host=value; }
    public void ExecuteMso(string command) { if(command!="FileExit")throw new Exception("wrong_command"); host.ExitCommand(); }
}
public sealed class ReaderAddin { public object Object { get; set; } }
public sealed class ReaderConnectionPoint : IConnectionPoint
{
    public object Sink;
    public bool FailAdvise;
    public bool FailUnadvise;
    public int Unadvised;
    public void GetConnectionInterface(out Guid iid) { iid = new Guid("914934C2-5A91-11CF-8700-00AA0060263B"); }
    public void GetConnectionPointContainer(out IConnectionPointContainer container) { container = null; }
    public void Advise(object sink, out int cookie)
    { if (FailAdvise) throw new Exception("advise_failure"); Sink = sink; cookie = 1; }
    public void Unadvise(int cookie) { Unadvised++; if (FailUnadvise) throw new Exception("unadvise_failure"); }
    public void EnumConnections(out IEnumConnections enumerator) { enumerator = null; }
    public bool Cancel(string method, bool initial)
    {
        object[] args = method == "PresentationBeforeClose" ? new object[] { null, initial }
            : new object[] { null, 0, initial };
        Sink.GetType().GetMethod(method).Invoke(Sink, args);
        return (bool)args[args.Length - 1];
    }
}
public static class ReaderHarness
{
    private static Type guardType;
    private static object Guard(ReaderHost host, ReaderConnectionPoint point)
    {
        Func<object, IConnectionPoint> resolver = delegate(object ignored) { return point; };
        return Activator.CreateInstance(guardType, BindingFlags.Instance | BindingFlags.NonPublic,
            null, new object[] { host, Thread.CurrentThread.ManagedThreadId, resolver,
                new Func<AegisDesk.PptReader.PptShutdownState, object>(state => new ManagedSink(state)) }, null);
    }
    private static string Request(object guard, bool reading)
    {
        return (string)guardType.GetMethod("TryQuitIfIdle", BindingFlags.Instance | BindingFlags.NonPublic)
            .Invoke(guard, new object[] { reading });
    }
    private static void Expect(bool condition, string code) { if (!condition) throw new Exception(code); }
    private static void ExpectUnknown(object guard, string message)
    {
        try { Request(guard, false); throw new Exception("unknown_hidden"); }
        catch (TargetInvocationException exc)
        {
            var com = exc.InnerException as COMException;
            Expect(com != null && com.ErrorCode == unchecked((int)0x80004005)
                && com.Message == message, "unknown_contract");
        }
    }

    [STAThread]
    public static int Main(string[] args)
    {
        Assembly assembly = Assembly.LoadFrom(args[0]);
        Expect(assembly.GetName().Version.ToString() == "1.0.5.10", "version_mismatch");
        Type type = assembly.GetType("AegisDesk.PptReader.Connect", true);
        Expect(type.GUID == new Guid("287A80D2-804A-46D3-A0E9-CA31C6FC3D09"), "clsid_mismatch");
        Expect(((ProgIdAttribute)Attribute.GetCustomAttribute(type, typeof(ProgIdAttribute))).Value == "AegisDesk.PptReader", "progid_mismatch");
        Type reader = assembly.GetType("AegisDesk.PptReader.IPptReader", true);
        Expect(reader.GUID == new Guid("65152412-DFD3-47EC-8346-3F2675ED0C15") && reader.GetMethods().Length == 3,
            "legacy_reader_abi_mismatch");
        Type lifecycle = assembly.GetType("AegisDesk.PptReader.IPptReaderLifecycle", true);
        Type clientShutdown = assembly.GetType("AegisDesk.PptReader.IPptReaderClientShutdown", true);
        Expect(lifecycle.GUID == new Guid("9A20F4A6-6364-4BA7-930B-3E8857BEAF60")
            && lifecycle.GetMethods().Length == 4
            && clientShutdown.GUID == new Guid("EC5AF9D4-8022-48CD-9D9F-1EF5CC647CC7")
            && clientShutdown.GetMethods().Length == 7
            && ((ComDefaultInterfaceAttribute)Attribute.GetCustomAttribute(type, typeof(ComDefaultInterfaceAttribute))).Value == clientShutdown,
            "lifecycle_abi_mismatch");
        Expect(((DispIdAttribute)Attribute.GetCustomAttribute(lifecycle.GetMethod("TryQuitIfIdle"), typeof(DispIdAttribute))).Value == 4,
            "quit_dispid_mismatch");
        Type office = assembly.GetType("AegisDesk.PptReader.IDTExtensibility2", true);
        Expect(office.GUID == new Guid("B65AD801-ABAF-11D0-BB8B-00A0C90F2744")
            && ((InterfaceTypeAttribute)Attribute.GetCustomAttribute(office, typeof(InterfaceTypeAttribute))).Value == ComInterfaceType.InterfaceIsDual,
            "office_abi_mismatch");
        object instance = Activator.CreateInstance(type);
        try { type.GetMethod("ReaderVersion").Invoke(instance, null); throw new Exception("disconnected_accepted"); }
        catch (TargetInvocationException exc) { if (!(exc.InnerException is COMException)) throw; }
        var addin = new ReaderAddin();
        type.GetMethod("OnConnection").Invoke(instance, new object[] { new ReaderHost(), 1, addin, null });
        Expect(Object.ReferenceEquals(instance, addin.Object)
            && (int)type.GetMethod("ProtocolVersion").Invoke(instance, null) == 3
            && (string)type.GetMethod("ReaderVersion").Invoke(instance, null) == "1.0.5.10", "production_connection_mismatch");
        // A fake host has no native typelib: it must never reach Quit even in candidate builds.
        Expect((string)type.GetMethod("TryQuitIfIdle").Invoke(instance, null) == "not_supported", "unverified_host_accepted");
        type.GetMethod("OnDisconnection").Invoke(instance, new object[] { 1, null });
        guardType = assembly.GetType("AegisDesk.PptReader.PptShutdownGuard", true);
        TestGuard();
        foreach (AssemblyName reference in assembly.GetReferencedAssemblies())
            Expect(reference.Name != "office" && reference.Name != "Microsoft.Office.Interop.PowerPoint"
                && reference.Name != "AegisDesk.PptReader.Events", "core_optional_dependency");
        // The real PIA sink is covered by run_product.py on x86 and x64.
        Console.WriteLine("PASS production ABI/version/managed guard; native Office not verified");
        return 0;
    }

    private static void TestGuard()
    {
        foreach(string close in new[]{"PresentationBeforeClose","ProtectedViewWindowBeforeClose"}) {
            var host=new ReaderHost();var point=new ReaderConnectionPoint();object guard=Guard(host,point);
            Expect(Request(guard,false)=="quit_requested"&&point.Unadvised==0,"deferred_guard_dropped_early");
            Expect(point.Cancel(close,false),"deferred_close_not_cancelled");
            Expect(!point.Cancel(close,false),"later_manual_close_blocked");
            var query=guardType.GetMethod("QueryOutcome",BindingFlags.Instance|BindingFlags.NonPublic);
            Expect((string)query.Invoke(guard,null)=="protected"&&point.Unadvised==1,"deferred_cancellation_not_observed");
            Expect((string)query.Invoke(guard,null)=="protected"&&point.Unadvised==1,"duplicate_deferred_cleanup");
        }

        foreach (string close in new string[] { "PresentationBeforeClose", "ProtectedViewWindowBeforeClose" })
        {
            var host = new ReaderHost(); var point = new ReaderConnectionPoint(); object guard = Guard(host, point);
            int calls = 0;
            Action callback = delegate {
                calls++;
                Expect(point.Cancel(close, false), "callback_late_document_unprotected");
                Expect(Request(guard, false) == "protected", "callback_reentry");
                guardType.GetMethod("Disconnect", BindingFlags.Instance | BindingFlags.NonPublic).Invoke(guard, null);
                Expect(point.Cancel(close, false), "callback_disconnect_cleared_guard");
            };
            object[] args = { false, 0, callback };
            string outcome = (string)guardType.GetMethod("TryQuitIfIdleTracked", BindingFlags.Instance | BindingFlags.NonPublic).Invoke(guard, args);
            Expect(outcome == "protected" && calls == 1 && host.QuitCount == 0, "callback_guard_result");
            Expect(!point.Cancel(close, false), "callback_guard_retained");
        }
        foreach (string collection in new string[] { "Presentations", "ProtectedViewWindows", "SlideShowWindows" })
        {
            var host = new ReaderHost(); var point = new ReaderConnectionPoint();
            ((ReaderCollection)typeof(ReaderHost).GetProperty(collection).GetValue(host, null)).Value = 1;
            Expect(Request(Guard(host, point), false) == "protected" && host.QuitCount == 0
                && host.DisplayAlerts == 2 && point.Unadvised == 1, "nonempty_" + collection);
            Expect(!point.Cancel("PresentationBeforeClose", false), "manual_close_blocked");
            Expect(point.Cancel("PresentationBeforeClose", true), "prior_cancel_reset");
        }
        foreach (string close in new string[] { "PresentationBeforeClose", "ProtectedViewWindowBeforeClose" })
        {
            var host = new ReaderHost(); var point = new ReaderConnectionPoint(); object guard = Guard(host, point);
            host.OnQuit = delegate {
                Expect(point.Cancel(close, false), "late_document_unprotected");
                Expect(Request(guard, false) == "protected", "reentrant_quit");
                guardType.GetMethod("Disconnect", BindingFlags.Instance | BindingFlags.NonPublic).Invoke(guard, null);
                Expect(point.Cancel(close, false), "disconnect_cleared_active_guard");
            };
            Expect(Request(guard, false) == "protected" && host.QuitCount == 1 && point.Unadvised == 0, "late_document_result");
            Expect(!point.Cancel(close, false), "finally_guard_active");
        }
        {
            var host = new ReaderHost(); var point = new ReaderConnectionPoint(); object guard = Guard(host, point);
            Expect(Request(guard, true) == "protected" && host.QuitCount == 0, "reading_quit");
            string other = null;
            var thread = new Thread(delegate() { other = Request(guard, false); });
            thread.SetApartmentState(ApartmentState.MTA); thread.Start(); thread.Join();
            Expect(other == "not_supported" && host.QuitCount == 0, "foreign_thread_quit");
            Expect(Request(guard, false) == "quit_requested" && Request(guard, false) == "protected"
                && host.QuitCount == 1, "duplicate_quit");
        }
        {
            var host = new ReaderHost(); var point = new ReaderConnectionPoint { FailAdvise = true };
            object guard = Guard(host, point);
            ExpectUnknown(guard, "ppt_shutdown_outcome_unknown");
            Expect(host.QuitCount == 0 && point.Unadvised == 0
                && Request(guard, false) == "not_supported", "advise_failure_quit");
        }
        {
            var host = new ReaderHost { FailAlerts = true }; var point = new ReaderConnectionPoint();
            Expect(Request(Guard(host, point), false) == "failed" && host.QuitCount == 0 && point.Unadvised == 1,
                "alerts_failure_quit");
        }
        {
            var host = new ReaderHost(); host.Presentations.Fail = true; var point = new ReaderConnectionPoint();
            Expect(Request(Guard(host, point), false) == "failed" && host.QuitCount == 0
                && !point.Cancel("PresentationBeforeClose", false), "count_failure_quit");
        }
        {
            var host = new ReaderHost(); host.Presentations.Fatal = true;
            var point = new ReaderConnectionPoint(); object guard = Guard(host, point);
            try { Request(guard, false); throw new Exception("fatal_hidden"); }
            catch (TargetInvocationException exc)
            { Expect(exc.InnerException is COMException && ((COMException)exc.InnerException).ErrorCode == unchecked((int)0x80010108), "fatal_hresult_lost"); }
            Expect(point.Unadvised == 0 && host.QuitCount == 0 && !point.Cancel("PresentationBeforeClose", false)
                && Request(guard, false) == "not_supported", "fatal_extra_rpc");
        }
        {
            var host = new ReaderHost(); var point = new ReaderConnectionPoint { FailUnadvise = true };
            object guard = Guard(host, point);
            Expect(Request(guard, false) == "quit_requested" && point.Unadvised == 0, "deferred_unadvise_too_early");
            Expect(point.Cancel("PresentationBeforeClose", false), "deferred_close_failure_unprotected");
            var query=guardType.GetMethod("QueryOutcome",BindingFlags.Instance|BindingFlags.NonPublic);
            Expect((string)query.Invoke(guard,null) == "failed", "unadvise_failure_reported_as_protected");
            Expect(Request(guard, false) == "not_supported" && host.QuitCount == 1 && point.Unadvised == 1
                && !point.Cancel("PresentationBeforeClose", false), "unadvise_failure_guard_active");
        }
        {
            var host = new ReaderHost(); var point = new ReaderConnectionPoint(); object guard = Guard(host, point);
            host.OnQuit = delegate { throw new COMException("disconnect"); };
            try { Request(guard, false); throw new Exception("quit_exception_hidden"); }
            catch (TargetInvocationException exc)
            { Expect(exc.InnerException is COMException && exc.InnerException.Message == "ppt_quit_outcome_unknown", "quit_exception_type"); }
            Expect(point.Unadvised == 0 && !point.Cancel("PresentationBeforeClose", false)
                && Request(guard, false) == "not_supported" && host.QuitCount == 1, "failed_quit_retried");
        }
    }
}

// PIA-free managed guard fixture. It makes no claim about the native event ABI.
public sealed class ManagedSink
{
    private readonly AegisDesk.PptReader.PptShutdownState state;
    public ManagedSink(AegisDesk.PptReader.PptShutdownState value) { state=value; }
    public void UserOpen() { state.ObserveUserActivity(); }
    public void PresentationBeforeClose(object value,ref bool cancel) { if(state.Active) { cancel=true; state.Cancelled=true; state.CloseCancelled=true; if(state.DeferredProtection)state.Active=false; } }
    public void ProtectedViewWindowBeforeClose(object value,int reason,ref bool cancel) { PresentationBeforeClose(value,ref cancel); }
}
