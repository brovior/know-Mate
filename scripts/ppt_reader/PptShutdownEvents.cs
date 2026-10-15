// Explicit PowerPoint event ABI. No Office PIA is required at runtime.
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;

using TYPEATTR = System.Runtime.InteropServices.ComTypes.TYPEATTR;
using TYPEKIND = System.Runtime.InteropServices.ComTypes.TYPEKIND;
using FUNCDESC = System.Runtime.InteropServices.ComTypes.FUNCDESC;
using INVOKEKIND = System.Runtime.InteropServices.ComTypes.INVOKEKIND;
using ELEMDESC = System.Runtime.InteropServices.ComTypes.ELEMDESC;
using PARAMFLAG = System.Runtime.InteropServices.ComTypes.PARAMFLAG;
using TYPEDESC = System.Runtime.InteropServices.ComTypes.TYPEDESC;

namespace AegisDesk.PptReader
{
    [ComVisible(true), Guid("914934C2-5A91-11CF-8700-00AA0060263B")]
    [InterfaceType(ComInterfaceType.InterfaceIsIDispatch)]
    public interface IPptCloseEvents
    {
        [DispId(2025)] void PresentationBeforeClose([In, MarshalAs(UnmanagedType.IDispatch)] object presentation,
            [In, Out, MarshalAs(UnmanagedType.VariantBool)] ref bool cancel);
        [DispId(2028)] void ProtectedViewWindowBeforeClose([In, MarshalAs(UnmanagedType.IDispatch)] object window,
            [In] int reason, [In, Out, MarshalAs(UnmanagedType.VariantBool)] ref bool cancel);
    }

    [ComVisible(true), ClassInterface(ClassInterfaceType.None)]
    [ComDefaultInterface(typeof(IPptCloseEvents))]
    public sealed class PptCloseEventSink : IPptCloseEvents
    {
        internal PptCloseEventSink() { }
        internal bool Active;
        internal bool Cancelled;
        public void PresentationBeforeClose(object presentation, ref bool cancel)
        {
            if (Active) { cancel = true; Cancelled = true; }
        }
        public void ProtectedViewWindowBeforeClose(object window, int reason, ref bool cancel)
        {
            if (Active) { cancel = true; Cancelled = true; }
        }
    }

    [ComImport, Guid("00020400-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IHostDispatch
    {
        [PreserveSig] int GetTypeInfoCount(out uint count);
        [PreserveSig] int GetTypeInfo(uint index, uint lcid, out ITypeInfo info);
        [PreserveSig] int GetIDsOfNames(ref Guid iid, IntPtr names, uint count, uint lcid, IntPtr ids);
        [PreserveSig] int Invoke(int id, ref Guid iid, uint lcid, ushort flags, IntPtr parameters,
            IntPtr result, IntPtr exception, IntPtr argumentError);
    }

    internal static class PptEventContract
    {
        // VBA documents cancellation, not its COM ABI. Verify the supported host's
        // own typelib before Advise; unknown/missing/ref-VARIANT ABI stays disabled.
        internal static IConnectionPoint Resolve(object host)
        {
            ITypeInfo hostInfo;
            if (((IHostDispatch)host).GetTypeInfo(0, 0, out hostInfo) != 0 || hostInfo == null)
                throw new COMException("ppt_event_typeinfo_missing");
            ITypeLib library;
            int index;
            hostInfo.GetContainingTypeLib(out library, out index);
            ITypeInfo events;
            Guid iid = typeof(IPptCloseEvents).GUID;
            library.GetTypeInfoOfGuid(ref iid, out events);
            RequireEvent(events, 2025, new VarEnum[] { VarEnum.VT_DISPATCH, VarEnum.VT_BOOL });
            RequireEvent(events, 2028, new VarEnum[] { VarEnum.VT_DISPATCH, VarEnum.VT_I4, VarEnum.VT_BOOL });
            IConnectionPoint point;
            ((IConnectionPointContainer)host).FindConnectionPoint(ref iid, out point);
            if (point == null) throw new COMException("ppt_event_connection_missing");
            return point;
        }

        private static void RequireEvent(ITypeInfo info, int id, VarEnum[] types)
        {
            IntPtr attributes;
            info.GetTypeAttr(out attributes);
            int count;
            try
            {
                TYPEATTR type = (TYPEATTR)Marshal.PtrToStructure(attributes, typeof(TYPEATTR));
                if (type.guid != typeof(IPptCloseEvents).GUID || type.typekind != TYPEKIND.TKIND_DISPATCH)
                    throw new COMException("ppt_event_type_mismatch");
                count = type.cFuncs;
            }
            finally { info.ReleaseTypeAttr(attributes); }
            for (int index = 0; index < count; index++)
            {
                IntPtr function;
                info.GetFuncDesc(index, out function);
                try
                {
                    FUNCDESC method = (FUNCDESC)Marshal.PtrToStructure(function, typeof(FUNCDESC));
                    if (method.memid != id) continue;
                    if (method.cParams != types.Length || method.invkind != INVOKEKIND.INVOKE_FUNC
                        || (VarEnum)method.elemdescFunc.tdesc.vt != VarEnum.VT_VOID)
                        throw new COMException("ppt_event_signature_mismatch");
                    for (int argument = 0; argument < types.Length; argument++)
                    {
                        IntPtr address = new IntPtr(method.lprgelemdescParam.ToInt64()
                            + argument * Marshal.SizeOf(typeof(ELEMDESC)));
                        ELEMDESC parameter = (ELEMDESC)Marshal.PtrToStructure(address, typeof(ELEMDESC));
                        bool byref = argument == types.Length - 1;
                        TYPEDESC value = parameter.tdesc;
                        if (byref)
                        {
                            if ((VarEnum)value.vt != VarEnum.VT_PTR
                                || (parameter.desc.paramdesc.wParamFlags & PARAMFLAG.PARAMFLAG_FOUT) == 0)
                                throw new COMException("ppt_event_cancel_mismatch");
                            value = (TYPEDESC)Marshal.PtrToStructure(value.lpValue, typeof(TYPEDESC));
                        }
                        if (!byref && types[argument] == VarEnum.VT_I4 && (VarEnum)value.vt == VarEnum.VT_USERDEFINED)
                        {
                            ITypeInfo enumInfo;
                            info.GetRefTypeInfo(value.lpValue.ToInt32(), out enumInfo);
                            IntPtr enumAttributes;
                            enumInfo.GetTypeAttr(out enumAttributes);
                            try
                            {
                                TYPEATTR enumeration = (TYPEATTR)Marshal.PtrToStructure(enumAttributes, typeof(TYPEATTR));
                                if (enumeration.typekind != TYPEKIND.TKIND_ENUM)
                                    throw new COMException("ppt_event_reason_mismatch");
                            }
                            finally { enumInfo.ReleaseTypeAttr(enumAttributes); }
                        }
                        else if (!byref && types[argument] == VarEnum.VT_DISPATCH && (VarEnum)value.vt == VarEnum.VT_PTR)
                        {
                            TYPEDESC pointer = (TYPEDESC)Marshal.PtrToStructure(value.lpValue, typeof(TYPEDESC));
                            if ((VarEnum)pointer.vt != VarEnum.VT_USERDEFINED)
                                throw new COMException("ppt_event_object_mismatch");
                            ITypeInfo objectInfo;
                            info.GetRefTypeInfo(pointer.lpValue.ToInt32(), out objectInfo);
                            IntPtr objectAttributes;
                            objectInfo.GetTypeAttr(out objectAttributes);
                            try
                            {
                                TYPEATTR objectType = (TYPEATTR)Marshal.PtrToStructure(objectAttributes, typeof(TYPEATTR));
                                if (objectType.typekind != TYPEKIND.TKIND_DISPATCH)
                                    throw new COMException("ppt_event_object_mismatch");
                            }
                            finally { objectInfo.ReleaseTypeAttr(objectAttributes); }
                        }
                        else if ((VarEnum)value.vt != types[argument])
                            throw new COMException("ppt_event_parameter_mismatch");
                    }
                    return;
                }
                finally { info.ReleaseFuncDesc(function); }
            }
            throw new COMException("ppt_event_member_missing");
        }
    }
}
