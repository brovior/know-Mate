// No Office activation: exercise unchanged core CCWs and a reflection-based optional loading boundary.
using System;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using AegisDesk.PptReader;

internal static class LoaderProbe
{
    private static void Check(bool value,string code) { if(!value) throw new Exception(code); }
    private static void CoreContract(Connect core,string phase)
    {
        Check(!typeof(Connect).Assembly.GetReferencedAssemblies().Any(x=>x.Name=="office" || x.Name=="Microsoft.Office.Interop.PowerPoint"),"core_has_pia_dependency");
        var attribute=(ComDefaultInterfaceAttribute)Attribute.GetCustomAttribute(typeof(Connect),typeof(ComDefaultInterfaceAttribute));
        Check(attribute.Value==typeof(IPptReaderLifecycle),"default_interface_changed");
        IntPtr unknown=Marshal.GetIUnknownForObject(core), lifecycle=IntPtr.Zero, dispatch=IntPtr.Zero;
        try {
            foreach(string text in new[]{"65152412-DFD3-47EC-8346-3F2675ED0C15","9A20F4A6-6364-4BA7-930B-3E8857BEAF60","B65AD801-ABAF-11D0-BB8B-00A0C90F2744"}) {
                Guid iid=new Guid(text); IntPtr pointer;
                Check(Marshal.QueryInterface(unknown,ref iid,out pointer)==0,"core_qi:"+text);
                Marshal.Release(pointer);
            }
            Guid life=new Guid("9A20F4A6-6364-4BA7-930B-3E8857BEAF60"), disp=new Guid("00020400-0000-0000-C000-000000000046");
            Check(Marshal.QueryInterface(unknown,ref life,out lifecycle)==0 && Marshal.QueryInterface(unknown,ref disp,out dispatch)==0,"default_dispatch");
            Check(lifecycle==dispatch,"default_dispatch_pointer");
            Check(core.TryQuitIfIdle()=="not_supported","disconnected_core_state");
            Console.WriteLine("CORE PASS phase="+phase+" no_PIA_reference legacy_lifecycle_officeconnect_QI default_dispatch_unchanged");
        } finally {
            if(lifecycle!=IntPtr.Zero) Marshal.Release(lifecycle); if(dispatch!=IntPtr.Zero) Marshal.Release(dispatch); Marshal.Release(unknown);
        }
    }
    // No optional assembly/PIA type appears in a core field, signature or base/interface list.
    private static string LoadOptional(string path)
    {
        try {
            foreach(string name in new[]{"Microsoft.Office.Interop.PowerPoint","office"}) {
                string identity=name+", Version=15.0.0.0, Culture=neutral, PublicKeyToken=71e9bce111e9429c";
                Assembly dependency=Assembly.Load(identity);
                if(dependency.FullName!=identity || !dependency.GlobalAssemblyCache) return "not_supported:dependency_identity";
            }
            Assembly optional=Assembly.LoadFrom(path);
            object sink=Activator.CreateInstance(optional.GetType("PrototypeSink",true));
            IntPtr unknown=Marshal.GetIUnknownForObject(sink), pointer=IntPtr.Zero;
            try {
                Guid iid=new Guid("914934C2-5A91-11CF-8700-00AA0060263B");
                Check(Marshal.QueryInterface(unknown,ref iid,out pointer)==0,"optional_sink_qi");
            } finally { if(pointer!=IntPtr.Zero) Marshal.Release(pointer); Marshal.Release(unknown); GC.KeepAlive(sink); }
            return "ready";
        } catch(FileNotFoundException) { return "not_supported:dependency_missing"; }
        catch(FileLoadException) { return "not_supported:dependency_load_failed"; }
        catch(TypeLoadException) { return "not_supported:dependency_type_failed"; }
        catch(ReflectionTypeLoadException) { return "not_supported:dependency_types_failed"; }
    }
    [STAThread]
    public static int Main(string[] args)
    {
        try {
            var core=new Connect(); CoreContract(core,"before");
            string result=LoadOptional(args[0]); Console.WriteLine("OPTIONAL "+result);
            Check(args[1]=="ready"?result=="ready":result.StartsWith("not_supported:"),"unexpected_optional_result");
            CoreContract(core,"after"); GC.KeepAlive(core);
            Console.WriteLine("LOADER_RESULT PASS arch="+(IntPtr.Size*8)+" expected="+args[1]+" outcome="+result); return 0;
        } catch(Exception e) { Console.WriteLine("LOADER_RESULT FAIL type="+e.GetType().Name+" reason="+e.Message); return 1; }
    }
}
