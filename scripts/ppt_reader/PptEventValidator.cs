// Validate the complete host event contract before any connection or shutdown.
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using TYPEATTR=System.Runtime.InteropServices.ComTypes.TYPEATTR;
using TYPEKIND=System.Runtime.InteropServices.ComTypes.TYPEKIND;
using FUNCDESC=System.Runtime.InteropServices.ComTypes.FUNCDESC;
using ELEMDESC=System.Runtime.InteropServices.ComTypes.ELEMDESC;
using TYPEDESC=System.Runtime.InteropServices.ComTypes.TYPEDESC;
using IMPLTYPEFLAGS=System.Runtime.InteropServices.ComTypes.IMPLTYPEFLAGS;
using TYPELIBATTR=System.Runtime.InteropServices.ComTypes.TYPELIBATTR;

namespace AegisDesk.PptReader
{
internal static class PptEventValidator
{
    private static void Need(bool condition,string code) { if(!condition) throw new InvalidOperationException("contract_"+code); }
    private static TYPEATTR Attributes(ITypeInfo info)
    {
        IntPtr pointer; info.GetTypeAttr(out pointer);
        try { return (TYPEATTR)Marshal.PtrToStructure(pointer,typeof(TYPEATTR)); }
        finally { info.ReleaseTypeAttr(pointer); }
    }
    private static ITypeInfo ResolveObject(ITypeInfo info,int depth)
    {
        Need(depth<8,"reference_depth"); TYPEATTR type=Attributes(info);
        if(type.typekind==TYPEKIND.TKIND_ALIAS) {
            Need((VarEnum)type.tdescAlias.vt==VarEnum.VT_USERDEFINED,"alias_shape");
            ITypeInfo target; info.GetRefTypeInfo(unchecked((int)type.tdescAlias.lpValue.ToInt64()),out target);
            return ResolveObject(target,depth+1);
        }
        if(type.typekind==TYPEKIND.TKIND_COCLASS) {
            ITypeInfo chosen=null; int count=0;
            for(int i=0;i<type.cImplTypes;i++) {
                IMPLTYPEFLAGS flags; info.GetImplTypeFlags(i,out flags);
                if((flags&IMPLTYPEFLAGS.IMPLTYPEFLAG_FDEFAULT)==0 || (flags&IMPLTYPEFLAGS.IMPLTYPEFLAG_FSOURCE)!=0) continue;
                int href; info.GetRefTypeOfImplType(i,out href); info.GetRefTypeInfo(href,out chosen); count++;
            }
            Need(count==1 && chosen!=null,"coclass_default"); return ResolveObject(chosen,depth+1);
        }
        Need(type.typekind==TYPEKIND.TKIND_DISPATCH,"object_kind"); return info;
    }
    internal static void Validate(ITypeInfo info)
    {
        TYPEATTR type=Attributes(info);
        Need(type.guid==PptEventSignatures.Iid,"iid");
        Need(type.typekind==TYPEKIND.TKIND_INTERFACE && (int)type.wTypeFlags==4096,"interface_kind");
        Need(type.cFuncs==33 && type.cImplTypes==1 && type.cbSizeVft==40*IntPtr.Size,"layout");
        int baseRef; info.GetRefTypeOfImplType(0,out baseRef); ITypeInfo baseInfo; info.GetRefTypeInfo(baseRef,out baseInfo);
        TYPEATTR baseType=Attributes(baseInfo);
        Need(baseType.guid==new Guid("00020400-0000-0000-C000-000000000046") && baseType.typekind==TYPEKIND.TKIND_INTERFACE,"base");
        for(int i=0;i<33;i++) {
            EventSignature e=PptEventSignatures.Events[i]; IntPtr pointer; info.GetFuncDesc(i,out pointer);
            try {
                FUNCDESC method=(FUNCDESC)Marshal.PtrToStructure(pointer,typeof(FUNCDESC));
                Need(e.index==i && method.memid==e.dispid && method.oVft==e.slot*IntPtr.Size,"member_order_offset");
                Need((int)method.funckind==1 && (int)method.invkind==1 && (int)method.callconv==4,"calling_convention");
                Need((VarEnum)method.elemdescFunc.tdesc.vt==VarEnum.VT_HRESULT && method.cParams==e.args.Length && method.cParamsOpt==0,"signature");
                string name,doc,help; int context; info.GetDocumentation(method.memid,out name,out doc,out context,out help);
                Need(name==e.name,"member_name");
                for(int j=0;j<e.args.Length;j++) {
                    ELEMDESC arg=(ELEMDESC)Marshal.PtrToStructure(IntPtr.Add(method.lprgelemdescParam,j*Marshal.SizeOf(typeof(ELEMDESC))),typeof(ELEMDESC));
                    EventArgument wanted=e.args[j]; TYPEDESC value=arg.tdesc;
                    Need((int)arg.desc.paramdesc.wParamFlags==wanted.flags,"direction");
                    if(wanted.kind=="boolptr") {
                        Need((VarEnum)value.vt==VarEnum.VT_PTR && value.lpValue!=IntPtr.Zero,"cancel_pointer");
                        TYPEDESC pointee=(TYPEDESC)Marshal.PtrToStructure(value.lpValue,typeof(TYPEDESC));
                        Need((VarEnum)pointee.vt==VarEnum.VT_BOOL,"cancel_type");
                    } else if(wanted.kind=="r4") Need((VarEnum)value.vt==VarEnum.VT_R4,"float_type");
                    else if(wanted.kind=="interface") {
                        Need((VarEnum)value.vt==VarEnum.VT_PTR && value.lpValue!=IntPtr.Zero,"object_pointer");
                        TYPEDESC inner=(TYPEDESC)Marshal.PtrToStructure(value.lpValue,typeof(TYPEDESC));
                        Need((VarEnum)inner.vt==VarEnum.VT_USERDEFINED,"object_reference");
                        ITypeInfo referenced; info.GetRefTypeInfo(unchecked((int)inner.lpValue.ToInt64()),out referenced);
                        Need(Attributes(ResolveObject(referenced,0)).guid==new Guid(wanted.iid),"object_iid");
                    } else if(wanted.kind=="i4") {
                        Need((VarEnum)value.vt==VarEnum.VT_USERDEFINED,"enum_reference"); ITypeInfo enumInfo; info.GetRefTypeInfo(unchecked((int)value.lpValue.ToInt64()),out enumInfo);
                        TYPEATTR enumType=Attributes(enumInfo); ITypeLib lib; int index; enumInfo.GetContainingTypeLib(out lib,out index);
                        IntPtr libPointer; lib.GetLibAttr(out libPointer);
                        try {
                            TYPELIBATTR library=(TYPELIBATTR)Marshal.PtrToStructure(libPointer,typeof(TYPELIBATTR));
                            enumInfo.GetDocumentation(-1,out name,out doc,out context,out help);
                            Need(enumType.typekind==TYPEKIND.TKIND_ENUM && enumType.cbSizeInstance==4 && enumType.guid==new Guid(wanted.type_iid)
                                && library.guid==new Guid(wanted.library_iid) && name==wanted.name,"enum_identity");
                        } finally { lib.ReleaseTLibAttr(libPointer); }
                    } else Need(false,"unknown_type");
                }
            } finally { info.ReleaseFuncDesc(pointer); }
        }
    }
}

}
