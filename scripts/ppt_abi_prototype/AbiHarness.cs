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

internal static partial class AbiHarness
{
    private static int checks, calls, mockCount, qiCount, unavailableTypeInfo, marshalerTypedQI, negativeFailures;
    private static bool embedded;
    private static void Check(bool value, string code)
    { checks++; if (!value) throw new Exception(code); }
    private static IntPtr Zero(int bytes)
    { IntPtr p = Marshal.AllocHGlobal(bytes); for (int i=0;i<bytes;i++) Marshal.WriteByte(p,i,0); return p; }
    // Types here come from the independently captured host JSON, never sink reflection.
    private static int Call(IntPtr instance, int slot, Type[] types, object[] values)
    {
        var method = new DynamicMethod("NativeCall", typeof(int), new[] { typeof(IntPtr), typeof(IntPtr), typeof(object[]) }, typeof(AbiHarness).Module, true);
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
    private static void Metadata(Contract contract)
    {
        Assembly a = typeof(PrototypeSink).Assembly;
        bool piaDependency=a.GetReferencedAssemblies().Any(x=>x.Name=="office" || x.Name=="Microsoft.Office.Interop.PowerPoint");
        Check(piaDependency!=embedded,"pia_dependency_expectation");
        Type type = typeof(PrototypeSink).GetInterfaces().Single(x=>x.GUID==new Guid(contract.iid));
        var methods = type.GetMethods().OrderBy(x=>x.MetadataToken).ToArray();
        Check(methods.Length==33,"embedded_method_count");
        for(int i=0;i<33;i++) {
            Check(methods[i].Name==contract.events[i].name,"embedded_method_order:"+i);
            Check(((DispIdAttribute)Attribute.GetCustomAttribute(methods[i],typeof(DispIdAttribute))).Value==contract.events[i].dispid,"embedded_dispid:"+i);
            Check(methods[i].ReturnType==typeof(void),"embedded_return:"+i);
        }
        Console.WriteLine("METADATA PASS members33 embedded="+embedded+" pia_dependency="+piaDependency);
    }
    private static void RuntimeBindings()
    {
        foreach(string name in new[]{"Microsoft.Office.Interop.PowerPoint","office"}) {
            Assembly a=AppDomain.CurrentDomain.GetAssemblies().SingleOrDefault(x=>x.GetName().Name==name);
            if(embedded) { Check(a==null,"embedded_loaded_pia"); continue; }
            Check(a!=null && a.FullName==name+", Version=15.0.0.0, Culture=neutral, PublicKeyToken=71e9bce111e9429c","runtime_pia_identity");
            Check(a.GlobalAssemblyCache,"runtime_pia_not_gac");
            string hash;
            using(var file=File.OpenRead(a.Location)) using(var sha=System.Security.Cryptography.SHA256.Create())
                hash=BitConverter.ToString(sha.ComputeHash(file)).Replace("-","");
            Console.WriteLine("BINDING PASS identity="+a.FullName+" path="+a.Location+" sha256="+hash);
        }
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
    private static void One(PrototypeSink sink, IntPtr pointer, Event entry, bool active, short initial, bool dispatch, bool throwing)
    {
        var mocks=new List<NativeMock>(); var observed=new List<object>();
        var types=new List<Type>(); var values=new List<object>();
        IntPtr memory=Zero(18); for(int i=0;i<18;i++) Marshal.WriteByte(memory,i,0xA5);
        IntPtr cancel=IntPtr.Add(memory,8); Marshal.WriteInt16(cancel,initial);
        int seen=0; bool hasCancel=entry.args.Any(x=>x.kind=="boolptr");
        sink.Active=active; sink.Cancelled=false; sink.Broken=false; sink.ThrowIndex=throwing?entry.index:-1;
        for(int i=0;i<entry.args.Length;i++) {
            Argument arg=entry.args[i];
            if(arg.kind=="interface") {
                var mock=new NativeMock(arg.iid); mocks.Add(mock);
                // The native caller must supply the declared interface, not an arbitrary IUnknown.
                Guid expected=new Guid(arg.iid); IntPtr typed;
                Check(Marshal.QueryInterface(mock.Pointer,ref expected,out typed)==0 && typed==mock.Pointer,"caller_typed_qi");
                Marshal.Release(typed);
                types.Add(typeof(IntPtr)); values.Add(mock.Pointer);
            }
            else if(arg.kind=="boolptr") { types.Add(typeof(IntPtr)); values.Add(cancel); }
            else if(arg.kind=="i4") { types.Add(typeof(int)); values.Add(2); }
            else if(arg.kind=="r4") { types.Add(typeof(float)); values.Add(i==1?1.25f:-2.5f); }
            else throw new Exception("unknown_contract_argument");
        }
        sink.Observe=delegate(int index,object[] args) {
            Check(index==entry.index,"slot_misroute:"+entry.index+":"+index); seen++;
            int mockIndex=0;
            for(int i=0;i<args.Length;i++) {
                Argument arg=entry.args[i];
                if(arg.kind=="interface") {
                    Check(Marshal.IsComObject(args[i]),"not_rcw"); observed.Add(args[i]);
                    IntPtr unknown=Marshal.GetIUnknownForObject(args[i]);
                    try { Check(unknown==mocks[mockIndex++].Pointer,"native_object_identity"); }
                    finally { Marshal.Release(unknown); }
                } else if(arg.kind=="boolptr") Check((bool)args[i]==(initial!=0),"incoming_cancel");
                else if(arg.kind=="i4") Check(Convert.ToInt32(args[i])==2,"enum_value");
                else if(arg.kind=="r4") Check((float)args[i]==(float)values[i],"float_value");
            }
            GC.Collect(); GC.WaitForPendingFinalizers(); // sink and native callback roots must remain live
        };
        try {
            int hr=dispatch ? Dispatch(pointer,entry,values.ToArray()) : Call(pointer,entry.slot,types.ToArray(),values.ToArray());
            bool target=entry.dispid==2025||entry.dispid==2028;
            Check(hr==(throwing&&!target?(dispatch?unchecked((int)0x80020009):unchecked((int)0x80040201)):0),"hresult:"+entry.index+":"+hr.ToString("X8"));
            Check(seen==1,"event_not_seen:"+entry.index);
            short expected=(short)(hasCancel&&target&&active?-1:initial);
            Check(Marshal.ReadInt16(cancel)==expected,"cancel_write:"+entry.index);
            Check(sink.Cancelled==(hasCancel&&target&&active),"cancel_latch");
            Check(sink.Broken==(throwing&&target),"callback_failure_latch");
            for(int i=0;i<18;i++) if(i!=8&&i!=9) Check(Marshal.ReadByte(memory,i)==0xA5,"canary_corrupt:"+i);
            calls++;
            if(!active && initial==0 && !throwing) Console.WriteLine("EVENT PASS route="+(dispatch?"dispatch":"vtable")+" slot="+entry.slot+" dispid="+entry.dispid+" name="+entry.name);
        } finally {
            sink.Observe=null;
            foreach(object rcw in observed.Distinct()) Marshal.FinalReleaseComObject(rcw);
            foreach(var mock in mocks) {
                Check(mock.UnexpectedCalls==0,"object_method_called");
                // Typed vtable parameters need not trigger another QI in the CLR.
                // Separate that observation from the caller's mandatory QI above.
                marshalerTypedQI+=mock.Queries.Count(x=>x==mock.Supported)-1;
                Check(mock.Queries.Contains(mock.Supported),"caller_qi_missing");
                qiCount+=mock.Queries.Count; mockCount++; mock.Dispose();
            }
            Marshal.FreeHGlobal(memory);
        }
    }
    [System.Runtime.CompilerServices.MethodImpl(System.Runtime.CompilerServices.MethodImplOptions.NoInlining)]
    private static IntPtr RetainedSink(Guid iid,out WeakReference weak)
    {
        var sink=new PrototypeSink(); weak=new WeakReference(sink);
        IntPtr unknown=Marshal.GetIUnknownForObject(sink), pointer;
        try { Check(Marshal.QueryInterface(unknown,ref iid,out pointer)==0,"lifetime_qi"); }
        finally { Marshal.Release(unknown); }
        return pointer;
    }
    private static void Lifetime(Contract contract)
    {
        WeakReference weak; IntPtr pointer=RetainedSink(new Guid(contract.iid),out weak);
        try {
            GC.Collect(); GC.WaitForPendingFinalizers(); GC.Collect();
            Check(weak.IsAlive,"com_reference_did_not_retain_sink");
            using(var mock=new NativeMock(contract.events[0].args[0].iid)) {
                Check(Call(pointer,7,new[]{typeof(IntPtr)},new object[]{mock.Pointer})==0,"retained_sink_call");
                GC.Collect(); GC.WaitForPendingFinalizers(); GC.Collect();
            }
        } finally { Marshal.Release(pointer); }
        GC.Collect(); GC.WaitForPendingFinalizers(); GC.Collect();
        Check(!weak.IsAlive,"sink_leaked_after_final_release");
        Console.WriteLine("LIFETIME PASS external_COM_ref_survives_GC final_release_collects");
    }
    [STAThread]
    public static int Main(string[] args)
    {
        try {
            Contract contract=new JavaScriptSerializer().Deserialize<Contract>(File.ReadAllText(args[0]));
            embedded=args.Length<2 || args[1]!="reference";
            Metadata(contract); var sink=new PrototypeSink();
            IntPtr unknown=Marshal.GetIUnknownForObject(sink), pointer=IntPtr.Zero; Guid iid=new Guid(contract.iid);
            try {
                Check(Marshal.QueryInterface(unknown,ref iid,out pointer)==0,"sink_qi"); BaseSlots(pointer,iid);
                foreach(bool dispatch in new[]{false,true}) {
                    foreach(Event entry in contract.events) {
                        foreach(bool active in new[]{false,true}) foreach(short initial in new short[]{0,-1}) One(sink,pointer,entry,active,initial,dispatch,false);
                    }
                    One(sink,pointer,contract.events[0],false,0,dispatch,true);
                    foreach(int closeIndex in new[]{24,27}) foreach(bool active in new[]{false,true}) foreach(short initial in new short[]{0,-1})
                        One(sink,pointer,contract.events[closeIndex],active,initial,dispatch,true);
                    Console.WriteLine((dispatch?"DISPATCH":"VTABLE")+" PASS all33 active_initial_matrix exception_hresult");
                    Console.WriteLine("CLOSE_FAILURE PASS route="+(dispatch?"dispatch":"vtable")+" cases=8 cancellation_before_fault retained_no_throw");
                }
                NegativeDispatch(sink,pointer,contract.events[24]);
                Coercion(sink,pointer,contract.events[24]);
            } finally { if(pointer!=IntPtr.Zero) Marshal.Release(pointer); Marshal.Release(unknown); GC.KeepAlive(sink); }
            Lifetime(contract);
            RuntimeBindings();
            ContractFixtures.Run(contract);
            Console.WriteLine("RESULT "+(negativeFailures>0?"FAIL":unavailableTypeInfo==0?"PASS":"PARTIAL")+" arch="+(IntPtr.Size*8)+" calls="+calls+" checks="+checks+" nativeMocks="+mockCount+" nativeQI="+qiCount+" marshalerTypedQI="+marshalerTypedQI+" typeinfo_unavailable="+unavailableTypeInfo+" negative_failures="+negativeFailures);
            return negativeFailures>0?1:unavailableTypeInfo==0?0:2;
        } catch(Exception exc) { Console.WriteLine("RESULT FAIL arch="+(IntPtr.Size*8)+" type="+exc.GetType().Name+" reason="+exc.Message); return 1; }
    }
}
