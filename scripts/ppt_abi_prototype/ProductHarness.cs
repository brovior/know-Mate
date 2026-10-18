// Isolated proof, not product code. No Office activation, connection or registry writes.
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Reflection.Emit;
using System.Runtime.InteropServices;
using System.Web.Script.Serialization;

public sealed class Contract { public string iid; public Event[] events; }
public sealed class Event { public int index, dispid, slot; public string name; public Argument[] args; }
public sealed class Argument { public string kind, iid, name, type_iid, library_iid; public int flags; }

internal static partial class ProductHarness
{
    private static int checks, calls, mockCount, qiCount, unavailableTypeInfo;

    private static void Check(bool value, string code)
    { checks++; if (!value) throw new Exception(code); }
    private static IntPtr Zero(int bytes)
    { IntPtr p = Marshal.AllocHGlobal(bytes); for (int i=0;i<bytes;i++) Marshal.WriteByte(p,i,0); return p; }
    // Types here come from the independently captured host JSON, never sink reflection.
    private static int Call(IntPtr instance, int slot, Type[] types, object[] values)
    {
        var method = new DynamicMethod("NativeCall", typeof(int), new[] { typeof(IntPtr), typeof(IntPtr), typeof(object[]) }, typeof(ProductHarness).Module, true);
        ILGenerator il = method.GetILGenerator(); il.Emit(OpCodes.Ldarg_0);
        for (int i=0;i<types.Length;i++) {
            il.Emit(OpCodes.Ldarg_2); il.Emit(OpCodes.Ldc_I4,i); il.Emit(OpCodes.Ldelem_Ref); il.Emit(OpCodes.Unbox_Any,types[i]);
        }
        il.Emit(OpCodes.Ldarg_1);
        il.EmitCalli(OpCodes.Calli, CallingConvention.StdCall, typeof(int), new[] { typeof(IntPtr) }.Concat(types).ToArray());
        il.Emit(OpCodes.Ret);
        var call = (Func<IntPtr,IntPtr,object[],int>)method.CreateDelegate(typeof(Func<IntPtr,IntPtr,object[],int>));
        return call(instance, Marshal.ReadIntPtr(Marshal.ReadIntPtr(instance),slot*IntPtr.Size), values);
    }
    private static void BaseSlots(IntPtr sink, Guid iid)
    {
        IntPtr iidMemory=Zero(16), result=Zero(IntPtr.Size);
        try {
            Marshal.StructureToPtr(iid,iidMemory,false);
            Check(Call(sink,0,new[]{typeof(IntPtr),typeof(IntPtr)},new object[]{iidMemory,result})==0,"base_qi");
            IntPtr queried=Marshal.ReadIntPtr(result); Check(queried==sink,"base_identity");
            Call(queried,2,Type.EmptyTypes,new object[0]);
            int added=Call(sink,1,Type.EmptyTypes,new object[0]);
            int released=Call(sink,2,Type.EmptyTypes,new object[0]); Check(added==released+1,"base_refcount");
            Marshal.StructureToPtr(new Guid("22616AB3-F868-47B7-9C5C-61F81E9DAA21"),iidMemory,false);
            Check(Call(sink,0,new[]{typeof(IntPtr),typeof(IntPtr)},new object[]{iidMemory,result})==unchecked((int)0x80004002),"unknown_iid");
            Check(Marshal.ReadIntPtr(result)==IntPtr.Zero,"unknown_iid_null");
            int countHr=Call(sink,3,new[]{typeof(IntPtr)},new object[]{result});
            Console.WriteLine("BASE typeinfo_hr="+countHr.ToString("X8")+" count="+Marshal.ReadInt32(result));
            // Record unavailable type information; still probe event slots independently.
            if(countHr!=0) unavailableTypeInfo++;
            Marshal.WriteIntPtr(result,IntPtr.Zero);
            int typeHr=Call(sink,4,new[]{typeof(uint),typeof(uint),typeof(IntPtr)},new object[]{0u,0u,result});
            IntPtr typeinfo=Marshal.ReadIntPtr(result);
            Console.WriteLine("BASE get_typeinfo_hr="+typeHr.ToString("X8")+" present="+(typeinfo!=IntPtr.Zero));
            if(typeHr!=0) unavailableTypeInfo++;
            if(typeinfo!=IntPtr.Zero) Marshal.Release(typeinfo);
        } finally { Marshal.FreeHGlobal(iidMemory); Marshal.FreeHGlobal(result); }
        Console.WriteLine("BASE slots0-2=PASS typeinfo_unavailable="+unavailableTypeInfo+"; slots5-6 tested separately");
    }

    internal static string DirectoryPath;
    internal static Guid? CachedPackageMvid;
    internal static Assembly CachedAssembly;
    internal static object Invoke(Type type,string name,object target,params object[] args)
    {
        try { return type.GetMethod(name,BindingFlags.Static|BindingFlags.Instance|BindingFlags.NonPublic|BindingFlags.Public).Invoke(target,args); }
        catch(TargetInvocationException error) { throw error.InnerException; }
    }
    internal static Type Core(string name) { return typeof(AegisDesk.PptReader.Connect).Assembly.GetType("AegisDesk.PptReader."+name,true); }
    internal static object Load(AegisDesk.PptReader.PptShutdownState state)
    { return Invoke(Core("PptSinkLoader"),"LoadValidated",null,DirectoryPath,Path.Combine(DirectoryPath,"installation.json"),state); }
    private static void Failure(string mode)
    {
        var reader=new AegisDesk.PptReader.Connect(); Array custom=null;
        reader.OnConnection(new SyntheticExtractionHost(),1,new SyntheticAddin(),ref custom);
        string before=reader.ExtractSlide(@"C:\Synthetic\Empty.ppt",1);
        var host=new FakeHost(); var point=new FakePoint();
        var guard=new ProductGuard(host,System.Threading.Thread.CurrentThread.ManagedThreadId,delegate { return point; },delegate(AegisDesk.PptReader.PptShutdownState state) {
            point.State=state;
            if(CachedPackageMvid.HasValue) {
                try { Invoke(Core("PptPackageIdentity"),"RequireLoaded",null,CachedAssembly,CachedPackageMvid.Value); }
                catch(IOException error) { Check(error.Message=="ppt_event_loaded_mvid_mismatch","cached_build_rejection_reason"); throw; }
                throw new Exception("cached_build_accepted");
            }
            try { return Load(state); }
            catch(IOException error) {
                if(mode=="events-cached-build") Check(error.Message=="ppt_event_loaded_mvid_mismatch","cached_event_rejection_reason");
                throw;
            }
        });
        if(mode.StartsWith("callback-fault")) {
            point.FailUnadvise=true;
            Action close=delegate { Check(point.Close(),"fault_cancel_before_throw"); Check(point.State.Broken,"callback_fault_not_latched"); };
            if(mode=="callback-fault-quit") host.Quitting=close; else host.Presentations.Read=close;
        }
        bool inQuit=mode=="callback-fault-quit";
        if(inQuit) {
            bool unknown=false;
            try { guard.TryQuitIfIdle(false); }
            catch(COMException error) { unknown=error.Message=="ppt_quit_outcome_unknown"; }
            Check(unknown && point.State.Broken && point.State.Cancelled,"broken_quit_unknown_cancel_preserved");
            Check(point.UnadviseCalls==0 && host.AfterQuitRpc==0,"broken_quit_extra_rpc");
        } else Check(guard.TryQuitIfIdle(false)=="not_supported","dependency_quit");
        Check(host.QuitCalls==(inQuit?1:0),"dependency_quit_count");
        Check(guard.TryQuitIfIdle(false)=="not_supported" && host.QuitCalls==(inQuit?1:0),"dependency_not_sticky");
        Check(host.AfterQuitRpc==0,"host_rpc_after_broken_retry");
        Check(point.AdviseCalls==(mode.StartsWith("callback-fault")?1:0),"dependency_advise");
        if(mode.StartsWith("callback-fault")) Check(!point.State.Active && !point.Close(),"fault_retained_active");
        string after=reader.ExtractSlide(@"C:\Synthetic\Empty.ppt",1);
        Check(before.Contains("nodes") && after.Contains("nodes"),"extraction_unavailable");
        reader.OnDisconnection(1,ref custom);
        Console.WriteLine("DEPENDENCY_RESULT PASS "+mode+" extraction_before_after sticky_disabled quit_calls="+host.QuitCalls+"");
    }
    internal static void Validate(System.Runtime.InteropServices.ComTypes.ITypeInfo info)
    { Invoke(Core("PptEventValidator"),"Validate",null,info); }
    internal static bool Close(object sink)
    {
        IntPtr unknown=Marshal.GetIUnknownForObject(sink), pointer=IntPtr.Zero, cancel=Zero(2);
        Guid iid=new Guid("914934C2-5A91-11CF-8700-00AA0060263B");
        try {
            Check(Marshal.QueryInterface(unknown,ref iid,out pointer)==0,"guard_sink_qi");
            Check(Call(pointer,31,new[]{typeof(IntPtr),typeof(IntPtr)},new object[]{IntPtr.Zero,cancel})==0,"guard_close_hresult");
            return Marshal.ReadInt16(cancel)==-1;
        } finally { if(pointer!=IntPtr.Zero) Marshal.Release(pointer); Marshal.Release(unknown); Marshal.FreeHGlobal(cancel); }
    }
    private static void One(object sink,AegisDesk.PptReader.PptShutdownState state,IntPtr pointer,Event entry,bool active,short initial,bool dispatch)
    {
        var mocks=new List<NativeMock>(); var types=new List<Type>(); var values=new List<object>();
        IntPtr buffer=Zero(18), cancel=IntPtr.Add(buffer,8);
        for(int i=0;i<18;i++) Marshal.WriteByte(buffer,i,0xA5);
        Marshal.WriteInt16(cancel,initial); state.Active=active; state.Cancelled=false;
        foreach(Argument arg in entry.args) {
            if(arg.kind=="interface") { var mock=new NativeMock(arg.iid); mocks.Add(mock); types.Add(typeof(IntPtr)); values.Add(mock.Pointer); }
            else if(arg.kind=="boolptr") { types.Add(typeof(IntPtr)); values.Add(cancel); }
            else if(arg.kind=="i4") { types.Add(typeof(int)); values.Add(2); }
            else if(arg.kind=="r4") { types.Add(typeof(float)); values.Add(1.25f); }
            else throw new Exception("argument_kind");
        }
        try {
            int hr=dispatch?Dispatch(pointer,entry,values.ToArray()):Call(pointer,entry.slot,types.ToArray(),values.ToArray());
            Check(hr==0,"event_hresult:"+entry.name);
            bool closing=entry.index==24||entry.index==27;
            Check(state.Cancelled==(active&&closing),"event_state:"+entry.name);
            Check(Marshal.ReadInt16(cancel)==(active&&closing?(short)-1:initial),"cancel_writeback:"+entry.name);
            for(int i=0;i<18;i++) if(i!=8&&i!=9) Check(Marshal.ReadByte(buffer,i)==0xA5,"cancel_canary");
            calls++;
        } finally {
            // The shipping no-op sink does not retain arguments; collect transient RCWs.
            GC.Collect(); GC.WaitForPendingFinalizers(); GC.Collect(); GC.WaitForPendingFinalizers();
            foreach(var mock in mocks) { Check(mock.UnexpectedCalls==0,"argument_method_call"); mockCount++; qiCount+=mock.Queries.Count; mock.Dispose(); }
            Marshal.FreeHGlobal(buffer); GC.KeepAlive(sink);
        }
    }
    [STAThread]
    public static int Main(string[] args)
    {
        try {
            DirectoryPath=Path.GetDirectoryName(typeof(AegisDesk.PptReader.Connect).Assembly.Location);
            if(args.Length>1) {
                if(args[1]=="events-cached-build") {
                    Assembly cached=Assembly.LoadFrom(args[2]);
                    Guid disk=(Guid)Invoke(Core("PptPackageIdentity"),"DiskMvid",null,args[3],args[4]);
                    Check(cached.FullName==AssemblyName.GetAssemblyName(args[3]).FullName && cached.ManifestModule.ModuleVersionId!=disk,"different_event_build_required");
                    CachedAssembly=cached; CachedPackageMvid=disk;
                }
                if(args[1]=="core-cached-build") {
                    Assembly cached=typeof(AegisDesk.PptReader.Connect).Assembly;
                    Guid disk=(Guid)Invoke(Core("PptPackageIdentity"),"DiskMvid",null,args[2],args[3]);
                    Check(cached.FullName==AssemblyName.GetAssemblyName(args[2]).FullName && cached.ManifestModule.ModuleVersionId!=disk,"different_core_build_required");
                    CachedAssembly=cached; CachedPackageMvid=disk;
                }
                if(CachedPackageMvid.HasValue) Console.WriteLine("MVID_GATE actual_product_comparison preloaded="+CachedAssembly.ManifestModule.ModuleVersionId+" disk="+CachedPackageMvid.Value+" full_loader_cache_not_simulated");
                Failure(args[1]); return 0;
            }
            var contract=new JavaScriptSerializer().Deserialize<Contract>(File.ReadAllText(args[0]));
            var state=new AegisDesk.PptReader.PptShutdownState(); object sink=Load(state);
            Check(!typeof(AegisDesk.PptReader.Connect).Assembly.GetReferencedAssemblies().Any(x=>x.Name=="office"||x.Name=="Microsoft.Office.Interop.PowerPoint"||x.Name=="AegisDesk.PptReader.Events"),"core_dependency");
            var members=sink.GetType().GetInterfaces().Single(x=>x.GUID==new Guid(contract.iid)).GetMethods().OrderBy(x=>x.MetadataToken).ToArray();
            Check(members.Length==33,"members");
            for(int i=0;i<33;i++) Check(members[i].Name==contract.events[i].name && ((DispIdAttribute)Attribute.GetCustomAttribute(members[i],typeof(DispIdAttribute))).Value==contract.events[i].dispid,"metadata_order");
            IntPtr unknown=Marshal.GetIUnknownForObject(sink),pointer=IntPtr.Zero; Guid iid=new Guid(contract.iid);
            try {
                Check(Marshal.QueryInterface(unknown,ref iid,out pointer)==0,"sink_qi"); BaseSlots(pointer,iid);
                foreach(bool dispatch in new[]{false,true}) foreach(var entry in contract.events)
                    foreach(bool active in new[]{false,true}) foreach(short initial in new short[]{0,-1}) One(sink,state,pointer,entry,active,initial,dispatch);
            } finally { if(pointer!=IntPtr.Zero) Marshal.Release(pointer); Marshal.Release(unknown); }
            ContractFixtures.Run(contract); GuardFixtures.RunAll();
            Check(unavailableTypeInfo==0,"typeinfo_missing");
            Console.WriteLine("PRODUCT_RESULT PASS arch="+IntPtr.Size*8+" calls="+calls+" checks="+checks+" mocks="+mockCount+" qi="+qiCount); return 0;
        } catch(Exception error) { Console.WriteLine("PRODUCT_RESULT FAIL "+error.GetType().Name+":"+error.Message); return 1; }
    }
}

// Empty in-memory presentation; no user file or Office object is opened.
public sealed class SyntheticAddin { public object Object { get; set; } }
public sealed class SyntheticExtractionHost { public SyntheticPresentations Presentations { get { return new SyntheticPresentations(); } } }
public sealed class SyntheticPresentations { public int Count { get { return 1; } } public SyntheticPresentation this[int index] { get { return new SyntheticPresentation(); } } }
public sealed class SyntheticPresentation { public string FullName { get { return @"C:\Synthetic\Empty.ppt"; } } public SyntheticSlides Slides { get { return new SyntheticSlides(); } } }
public sealed class SyntheticSlides { public int Count { get { return 1; } } public SyntheticSlide this[int index] { get { return new SyntheticSlide(); } } }
public sealed class SyntheticSlide { public object[] Shapes { get { return new object[0]; } } }
