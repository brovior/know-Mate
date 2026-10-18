// Clone and mutate ITypeInfo descriptors without modifying the registered typelib.
using System;
using System.Collections.Generic;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Runtime.Remoting.Messaging;
using System.Runtime.Remoting.Proxies;
using TYPEATTR=System.Runtime.InteropServices.ComTypes.TYPEATTR;
using TYPEKIND=System.Runtime.InteropServices.ComTypes.TYPEKIND;
using FUNCDESC=System.Runtime.InteropServices.ComTypes.FUNCDESC;
using ELEMDESC=System.Runtime.InteropServices.ComTypes.ELEMDESC;
using TYPEDESC=System.Runtime.InteropServices.ComTypes.TYPEDESC;
using IMPLTYPEFLAGS=System.Runtime.InteropServices.ComTypes.IMPLTYPEFLAGS;
using TYPEFLAGS=System.Runtime.InteropServices.ComTypes.TYPEFLAGS;
using PARAMFLAG=System.Runtime.InteropServices.ComTypes.PARAMFLAG;
using FUNCKIND=System.Runtime.InteropServices.ComTypes.FUNCKIND;
using INVOKEKIND=System.Runtime.InteropServices.ComTypes.INVOKEKIND;
using CALLCONV=System.Runtime.InteropServices.ComTypes.CALLCONV;

internal sealed class MutatedTypeInfo : RealProxy
{
    private readonly ITypeInfo source;
    private readonly string fault;
    private readonly bool root;
    private readonly Dictionary<IntPtr,Tuple<IntPtr,List<IntPtr>>> allocated=new Dictionary<IntPtr,Tuple<IntPtr,List<IntPtr>>>();
    internal MutatedTypeInfo(ITypeInfo source,string fault,bool root=true):base(typeof(ITypeInfo)) { this.source=source; this.fault=fault; this.root=root; }
    private static IntPtr Copy(object value,Type type,List<IntPtr> allocations)
    { IntPtr p=Marshal.AllocHGlobal(Marshal.SizeOf(type)); allocations.Add(p); Marshal.StructureToPtr(value,p,false); return p; }
    public override IMessage Invoke(IMessage message)
    {
        var call=(IMethodCallMessage)message; object[] args=call.Args; string name=call.MethodName;
        try {
            if(name=="GetRefTypeInfo" && !root && fault=="alias_cycle") {
                args[1]=GetTransparentProxy(); return new ReturnMessage(null,args,args.Length,call.LogicalCallContext,call);
            }
            if(name=="ReleaseTypeAttr" || name=="ReleaseFuncDesc") {
                Tuple<IntPtr,List<IntPtr>> item;
                if(allocated.TryGetValue((IntPtr)args[0],out item)) {
                    allocated.Remove((IntPtr)args[0]); foreach(IntPtr p in item.Item2) Marshal.FreeHGlobal(p); args[0]=item.Item1;
                }
            }
            object result=((MethodInfo)call.MethodBase).Invoke(source,args);
            if(name=="GetRefTypeInfo") args[1]=new MutatedTypeInfo((ITypeInfo)args[1],fault,false).GetTransparentProxy();
            if(name=="GetImplTypeFlags" && !root && fault=="coclass_default") args[1]=(IMPLTYPEFLAGS)0;
            if(name=="GetTypeAttr") {
                IntPtr original=(IntPtr)args[0]; TYPEATTR a=(TYPEATTR)Marshal.PtrToStructure(original,typeof(TYPEATTR));
                if(root) {
                    if(fault=="iid") a.guid=Guid.Empty;
                    if(fault=="kind") a.typekind=TYPEKIND.TKIND_DISPATCH;
                    if(fault=="flags") a.wTypeFlags=(TYPEFLAGS)0;
                    if(fault=="missing") a.cFuncs--;
                    if(fault=="extra") a.cFuncs++;
                    if(fault=="base_count") a.cImplTypes=0;
                    if(fault=="architecture") a.cbSizeVft=(short)(40*(IntPtr.Size==8?4:8));
                } else {
                    if(fault=="base_iid" && a.guid==new Guid("00020400-0000-0000-C000-000000000046")) a.guid=Guid.Empty;
                    if(fault=="object_iid" && a.typekind==TYPEKIND.TKIND_DISPATCH) a.guid=Guid.Empty;
                    if(fault=="enum_size" && a.typekind==TYPEKIND.TKIND_ENUM) a.cbSizeInstance=8;
                    if(fault=="enum_identity" && a.typekind==TYPEKIND.TKIND_ENUM) a.guid=Guid.NewGuid();
                    if(fault=="alias_cycle" && (a.typekind==TYPEKIND.TKIND_DISPATCH || a.typekind==TYPEKIND.TKIND_COCLASS)) {
                        a.typekind=TYPEKIND.TKIND_ALIAS; a.tdescAlias.vt=(short)VarEnum.VT_USERDEFINED; a.tdescAlias.lpValue=IntPtr.Zero;
                    }
                }
                var blocks=new List<IntPtr>(); IntPtr clone=Copy(a,typeof(TYPEATTR),blocks); allocated.Add(clone,Tuple.Create(original,blocks)); args[0]=clone;
            }
            if(name=="GetFuncDesc" && root) {
                IntPtr original=(IntPtr)args[1]; FUNCDESC f=(FUNCDESC)Marshal.PtrToStructure(original,typeof(FUNCDESC));
                var blocks=new List<IntPtr>(); int count=f.cParams;
                IntPtr parameters=Marshal.AllocHGlobal(count*Marshal.SizeOf(typeof(ELEMDESC))); blocks.Add(parameters);
                for(int j=0;j<count;j++) {
                    ELEMDESC p=(ELEMDESC)Marshal.PtrToStructure(IntPtr.Add(f.lprgelemdescParam,j*Marshal.SizeOf(typeof(ELEMDESC))),typeof(ELEMDESC));
                    if((int)args[0]==24 && j==count-1) {
                        if(fault=="direction_in") p.desc.paramdesc.wParamFlags=PARAMFLAG.PARAMFLAG_FOUT;
                        if(fault=="direction_out") p.desc.paramdesc.wParamFlags=PARAMFLAG.PARAMFLAG_FIN;
                        if(fault=="retval") p.desc.paramdesc.wParamFlags|=PARAMFLAG.PARAMFLAG_FRETVAL;
                        if(fault=="cancel_scalar") p.tdesc.vt=(short)VarEnum.VT_BOOL;
                        if(fault=="cancel_variant" || fault=="cancel_i4") {
                            var inner=new TYPEDESC(); inner.vt=(short)(fault=="cancel_variant"?VarEnum.VT_VARIANT:VarEnum.VT_I4);
                            p.tdesc.lpValue=Copy(inner,typeof(TYPEDESC),blocks);
                        }
                    }
                    Marshal.StructureToPtr(p,IntPtr.Add(parameters,j*Marshal.SizeOf(typeof(ELEMDESC))),false);
                }
                f.lprgelemdescParam=parameters;
                if((int)args[0]==24) {
                    if(fault=="order" || fault=="dispid") f.memid=2024;
                    if(fault=="offset") f.oVft+= (short)IntPtr.Size;
                    if(fault=="funckind") f.funckind=FUNCKIND.FUNC_DISPATCH;
                    if(fault=="invkind") f.invkind=INVOKEKIND.INVOKE_PROPERTYGET;
                    if(fault=="callconv") f.callconv=CALLCONV.CC_CDECL;
                    if(fault=="return") f.elemdescFunc.tdesc.vt=(short)VarEnum.VT_VOID;
                    if(fault=="argcount") f.cParams--;
                    if(fault=="optional") f.cParamsOpt=1;
                }
                IntPtr clone=Copy(f,typeof(FUNCDESC),blocks); allocated.Add(clone,Tuple.Create(original,blocks)); args[1]=clone;
            }
            return new ReturnMessage(result,args,args.Length,call.LogicalCallContext,call);
        } catch(TargetInvocationException error) { return new ReturnMessage(error.InnerException,call); }
        catch(Exception error) { return new ReturnMessage(error,call); }
    }
}

internal static class ContractFixtures
{
    [DllImport("oleaut32.dll",PreserveSig=false)]
    private static extern void LoadRegTypeLib(ref Guid guid,ushort major,ushort minor,int lcid,out ITypeLib library);
    internal static void Run(Contract expected)
    {
        Guid libraryId=new Guid("91493440-5A91-11CF-8700-00AA0060263B"), iid=new Guid(expected.iid);
        ITypeLib library; LoadRegTypeLib(ref libraryId,2,12,0,out library); ITypeInfo events; library.GetTypeInfoOfGuid(ref iid,out events);
        CandidateContractValidator.Validate(events,expected);
        CandidateContractValidator.Validate((ITypeInfo)new MutatedTypeInfo(events,"none").GetTransparentProxy(),expected);
        int rejected=0;
        foreach(string fault in new[]{"iid","kind","flags","missing","extra","base_count","architecture","base_iid","object_iid","enum_size","enum_identity","alias_cycle","coclass_default","order","dispid","offset","funckind","invkind","callconv","return","argcount","optional","direction_in","direction_out","retval","cancel_scalar","cancel_variant","cancel_i4"}) {
            int simulatedAdvise=0;
            try { CandidateContractValidator.Validate((ITypeInfo)new MutatedTypeInfo(events,fault).GetTransparentProxy(),expected); simulatedAdvise++; }
            catch(InvalidOperationException error) {
                if(!error.Message.StartsWith("contract_")) throw;
                rejected++; Console.WriteLine("CONTRACT_REJECT "+fault+" reason="+error.Message);
            }
            if(simulatedAdvise!=0) throw new Exception("contract_accepted_bad_fixture:"+fault);
        }
        Console.WriteLine("CONTRACT PASS actual_typelib_and_proxy negative_fixtures="+rejected+" no_Advise");
    }
}
