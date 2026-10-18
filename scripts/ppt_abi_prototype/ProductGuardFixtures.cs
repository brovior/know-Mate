// Compile with unchanged product guard/events; injected host and connection point are synthetic.
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Threading;
using AegisDesk.PptReader;

public sealed class FakeCounts
{
    public int Value; public Action Read, Rpc;
    public int Count { get { if(Rpc!=null) Rpc(); if(Read!=null) Read(); return Value; } }
}
public sealed class FakeHost
{
    private FakeCounts presentations,protectedViews,slideShows;
    public FakeCounts Presentations { get { Touch(); return presentations; } }
    public FakeCounts ProtectedViewWindows { get { Touch(); return protectedViews; } }
    public FakeCounts SlideShowWindows { get { Touch(); return slideShows; } }
    public int AfterQuitRpc; private bool returned;
    private void Touch() { if(returned) AfterQuitRpc++; }
    public Action Quitting; public int QuitCalls; public bool RejectAlerts;
    private int alerts=1;
    public int DisplayAlerts { get { Touch(); return alerts; } set { Touch(); if(!RejectAlerts) alerts=value; } }
    public FakeHost() { presentations=new FakeCounts{Rpc=Touch}; protectedViews=new FakeCounts{Rpc=Touch}; slideShows=new FakeCounts{Rpc=Touch}; }
    public void Quit() { QuitCalls++; if(Quitting!=null) Quitting(); returned=true; }
}
internal sealed class FakePoint:IConnectionPoint
{
    internal object Sink; internal PptShutdownState State; internal bool BreakOnAdvise; internal int AdviseCalls,UnadviseCalls;
    internal bool Partial,FailUnadvise; internal Exception AdviseError;
    public void Advise(object sink,out int cookie)
    { AdviseCalls++; Sink=sink; if(BreakOnAdvise) State.MarkBroken(); cookie=Partial || AdviseError==null?1:0; if(AdviseError!=null) throw AdviseError; }
    public void Unadvise(int cookie) { UnadviseCalls++; if(FailUnadvise) throw new COMException("synthetic"); Sink=null; }
    public void GetConnectionInterface(out Guid iid) { iid=new Guid("914934C2-5A91-11CF-8700-00AA0060263B"); }
    public void GetConnectionPointContainer(out IConnectionPointContainer container) { container=null; }
    public void EnumConnections(out IEnumConnections connections) { connections=null; }
    internal bool Close()
    { return ProductHarness.Close(Sink); }
}
internal static class GuardFixtures
{
    [DllImport("oleaut32.dll",PreserveSig=false)]
    private static extern void LoadRegTypeLib(ref Guid guid,ushort major,ushort minor,int lcid,out ITypeLib library);
    private static void ProductContractCheck(bool corrupt=false) {
        Guid libid=new Guid("91493440-5A91-11CF-8700-00AA0060263B"),iid=new Guid("914934C2-5A91-11CF-8700-00AA0060263B");
        ITypeLib library; LoadRegTypeLib(ref libid,2,12,0,out library); ITypeInfo events; library.GetTypeInfoOfGuid(ref iid,out events);
        ProductHarness.Validate(corrupt?(ITypeInfo)new MutatedTypeInfo(events,"cancel_variant").GetTransparentProxy():events);
    }
    private static int passed;
    private static void Check(bool value,string code) { if(!value) throw new Exception(code); }
    private static void Run(string scenario)
    {
        var host=new FakeHost(); var point=new FakePoint(); int resolved=0;
        var guard=new ProductGuard(host,Thread.CurrentThread.ManagedThreadId,delegate(object ignored) {
            resolved++; if(scenario=="resolve_failure") throw new COMException("synthetic"); ProductContractCheck(scenario=="contract_mutant"); return point;
        }, delegate(PptShutdownState state) { point.State=state; if(scenario=="factory_failure") throw new Exception("synthetic_factory"); return ProductHarness.Load(state); });
        if(scenario=="advise_broken") point.BreakOnAdvise=true;
        if(scenario=="count_broken") host.Presentations.Read=delegate { point.State.MarkBroken(); };
        if(scenario=="presentation") host.Presentations.Value=1;
        if(scenario=="protected_view") host.ProtectedViewWindows.Value=1;
        if(scenario=="slideshow") host.SlideShowWindows.Value=1;
        if(scenario=="alerts") host.RejectAlerts=true;
        if(scenario=="disconnected") guard.Disconnect();
        if(scenario=="count_disconnect") host.Presentations.Read=guard.Disconnect;
        if(scenario=="count_cancel") host.Presentations.Read=delegate { Check(point.Close(),"active_count_cancel"); };
        if(scenario=="reentry") host.Presentations.Read=delegate { Check(guard.TryQuitIfIdle(false)=="protected","reentry_not_protected"); GC.Collect(); GC.WaitForPendingFinalizers(); };
        if(scenario=="quit_broken") host.Quitting=delegate { point.State.MarkBroken(); };
        if(scenario=="quit_cancel") host.Quitting=delegate { Check(point.Close(),"active_quit_cancel"); };
        if(scenario=="quit_fatal") host.Quitting=delegate { throw new COMException("synthetic",unchecked((int)0x80010108)); };
        if(scenario=="quit_unknown") host.Quitting=delegate { throw new InvalidOperationException("synthetic"); };
        if(scenario=="unadvise") point.FailUnadvise=true;
        if(scenario=="advise" || scenario=="partial_advise" || scenario=="advise_fatal") {
            point.Partial=scenario!="advise";
            point.AdviseError=new COMException("synthetic",scenario=="advise_fatal"?unchecked((int)0x80010108):unchecked((int)0x80004005));
        }
        string result=null;
        try { result=guard.TryQuitIfIdle(scenario=="reading"); }
        catch(COMException error) {
            Check(scenario=="quit_fatal" || scenario=="quit_unknown" || scenario=="advise_fatal" || scenario=="quit_broken","unexpected_com_error");
            Check(error.Message==(scenario=="quit_unknown" || scenario=="quit_broken"?"ppt_quit_outcome_unknown":"ppt_transport_failed"),"transport_reason");
            Check(point.UnadviseCalls==0,"rpc_after_unknown_or_fatal");
            result="transport";
        }
        bool mayQuit=scenario=="quit_broken" || scenario=="idle" || scenario=="reentry" || scenario=="quit_cancel" || scenario=="quit_fatal" || scenario=="quit_unknown" || scenario=="unadvise";
        Check(host.QuitCalls==(mayQuit?1:0),"quit_count:"+scenario);
        string expected=mayQuit?"quit_requested":"not_supported";
        if(scenario=="presentation" || scenario=="protected_view" || scenario=="slideshow" || scenario=="reading" || scenario=="count_cancel" || scenario=="quit_cancel") expected="protected";
        if(scenario=="alerts") expected="failed";
        if(scenario=="quit_fatal" || scenario=="quit_unknown" || scenario=="advise_fatal" || scenario=="quit_broken") expected="transport";
        Check(result==expected,"result:"+scenario+":"+result);
        if(scenario=="partial_advise") Check(point.UnadviseCalls==1,"partial_advise_cleanup");
        if(scenario=="count_disconnect") Check(point.UnadviseCalls==0,"unadvise_after_disconnect");
        if(scenario=="reading" || scenario=="disconnected") Check(resolved==0,"resolve_before_protection");
        if(point.Sink!=null) Check(!point.State.Active && !point.Close(),"retained_sink_blocks_manual_close");
        if(scenario=="idle") Check(guard.TryQuitIfIdle(false)=="protected" && host.QuitCalls==1,"duplicate_quit");
        if(scenario=="unadvise" || scenario=="advise_broken" || scenario=="count_broken" || scenario=="factory_failure" || scenario=="quit_broken" || scenario=="contract_mutant") Check(guard.TryQuitIfIdle(false)=="not_supported","broken_capability_reused");
        Check(host.AfterQuitRpc==0,"host_rpc_after_quit");
        passed++; Console.WriteLine("GUARD PASS "+scenario);
    }
    private static void ThreadChecks()
    {
        var host=new FakeHost(); int resolved=0;
        var guard=new ProductGuard(host,Thread.CurrentThread.ManagedThreadId,delegate { resolved++; return new FakePoint(); });
        string wrong=null,mta=null; Exception failure=null;
        var thread=new Thread(delegate() { try { wrong=guard.TryQuitIfIdle(false); } catch(Exception e) { failure=e; } });
        thread.SetApartmentState(ApartmentState.STA); thread.Start(); Check(thread.Join(5000),"thread_timeout");
        Check(failure==null && wrong=="not_supported" && resolved==0,"wrong_thread");
        thread=new Thread(delegate() { try {
            var local=new ProductGuard(host,Thread.CurrentThread.ManagedThreadId,delegate { resolved++; return new FakePoint(); });
            mta=local.TryQuitIfIdle(false);
        } catch(Exception e) { failure=e; } });
        thread.SetApartmentState(ApartmentState.MTA); thread.Start(); Check(thread.Join(5000),"mta_timeout");
        Check(failure==null && mta=="not_supported" && resolved==0 && host.QuitCalls==0,"wrong_apartment");
        passed+=2; Console.WriteLine("GUARD PASS wrong_thread wrong_apartment");
    }
    [STAThread]
    public static int RunAll()
    {
        try {
            foreach(string scenario in new[]{"idle","presentation","protected_view","slideshow","alerts","disconnected","count_disconnect","count_cancel","reentry","quit_cancel","quit_fatal","quit_unknown","unadvise","advise","partial_advise","advise_fatal","reading","resolve_failure","advise_broken","count_broken","factory_failure","quit_broken","contract_mutant"}) Run(scenario);
            ThreadChecks(); Console.WriteLine("GUARD_RESULT PASS arch="+(IntPtr.Size*8)+" cases="+passed); return 0;
        } catch(Exception e) { throw new Exception("GUARD_RESULT FAIL "+e.Message,e); }
    }
}

// Reflection is limited to exercising internal production entry points from this test executable.
internal sealed class ProductGuard
{
    private readonly object guard; private readonly Type type=ProductHarness.Core("PptShutdownGuard");
    internal ProductGuard(object host,int thread,Func<object,IConnectionPoint> resolver):this(host,thread,resolver,ProductHarness.Load) { }
    internal ProductGuard(object host,int thread,Func<object,IConnectionPoint> resolver,Func<PptShutdownState,object> factory)
    { guard=Activator.CreateInstance(type,System.Reflection.BindingFlags.Instance|System.Reflection.BindingFlags.NonPublic,null,new object[]{host,thread,resolver,factory},null); }
    internal string TryQuitIfIdle(bool reading) { return (string)ProductHarness.Invoke(type,"TryQuitIfIdle",guard,reading); }
    internal void Disconnect() { ProductHarness.Invoke(type,"Disconnect",guard); }
}
