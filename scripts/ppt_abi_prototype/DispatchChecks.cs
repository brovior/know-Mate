using System;
using System.Runtime.InteropServices;

internal static partial class AbiHarness
{
    private static int Dispatch(IntPtr pointer, Event entry, object[] values, int fault=0,
        int scalar=123, Action<byte[],byte[]> inspect=null)
    {
        int variantSize=IntPtr.Size==8?24:16;
        int variantBytes=variantSize*values.Length;
        IntPtr variantAllocation=Zero(variantBytes+16), variants=IntPtr.Add(variantAllocation,8);
        for(int i=0;i<8;i++) { Marshal.WriteByte(variantAllocation,i,0xA5); Marshal.WriteByte(variantAllocation,variantBytes+8+i,0xA5); }
        IntPtr parameters=Zero(IntPtr.Size*2+8), iid=Zero(16), error=Zero(4), exception=Zero(64), invalidString=IntPtr.Zero;
        IntPtr name=Marshal.StringToCoTaskMemUni(entry.name), names=Zero(IntPtr.Size), id=Zero(4);
        try {
            Marshal.WriteIntPtr(names,name);
            Check(Call(pointer,5,new[]{typeof(IntPtr),typeof(IntPtr),typeof(uint),typeof(uint),typeof(IntPtr)},new object[]{iid,names,1u,0u,id})==0,"get_dispid");
            Check(Marshal.ReadInt32(id)==entry.dispid,"dispatch_id");
            for(int i=0;i<values.Length;i++) {
                IntPtr v=IntPtr.Add(variants,(values.Length-1-i)*variantSize); string kind=entry.args[i].kind;
                if(kind=="interface") { Marshal.WriteInt16(v,9); Marshal.WriteIntPtr(v,8,(IntPtr)values[i]); }
                if(kind=="boolptr") { Marshal.WriteInt16(v,0x400B); Marshal.WriteIntPtr(v,8,(IntPtr)values[i]); }
                if(kind=="boolptr" && fault==3) { Marshal.WriteInt16(v,3); Marshal.WriteInt32(v,8,scalar); }
                if(kind=="boolptr" && fault==4) {
                    invalidString=Marshal.StringToBSTR("not-a-boolean");
                    Marshal.WriteInt16(v,8); Marshal.WriteIntPtr(v,8,invalidString);
                }
                if(kind=="i4") { Marshal.WriteInt16(v,3); Marshal.WriteInt32(v,8,(int)values[i]); }
                if(kind=="r4") { Marshal.WriteInt16(v,4); Marshal.Copy(BitConverter.GetBytes((float)values[i]),0,IntPtr.Add(v,8),4); }
            }
            Marshal.WriteIntPtr(parameters,variants); Marshal.WriteInt32(parameters,IntPtr.Size*2,values.Length);
            if(fault==2) Marshal.WriteInt32(parameters,IntPtr.Size*2,0);
            byte[] before=new byte[variantBytes],after=new byte[variantBytes]; Marshal.Copy(variants,before,0,variantBytes);
            int hr=Call(pointer,6,new[]{typeof(int),typeof(IntPtr),typeof(uint),typeof(ushort),typeof(IntPtr),typeof(IntPtr),typeof(IntPtr),typeof(IntPtr)},new object[]{fault==1?9999:entry.dispid,iid,0u,(ushort)1,parameters,IntPtr.Zero,exception,error});
            Marshal.Copy(variants,after,0,variantBytes);
            for(int i=0;i<8;i++) Check(Marshal.ReadByte(variantAllocation,i)==0xA5 && Marshal.ReadByte(variantAllocation,variantBytes+8+i)==0xA5,"variant_array_canary");
            if(inspect!=null) inspect(before,after);
            return hr;
        } finally {
            // EXCEPINFO owns up to three BSTRs; test failures must not leak them.
            foreach(int offset in new[]{IntPtr.Size==8?8:4,IntPtr.Size==8?16:8,IntPtr.Size==8?24:12}) {
                IntPtr b=Marshal.ReadIntPtr(exception,offset); if(b!=IntPtr.Zero) Marshal.FreeBSTR(b);
            }
            foreach(var p in new[]{variantAllocation,parameters,iid,error,exception,names,id}) Marshal.FreeHGlobal(p);
            if(invalidString!=IntPtr.Zero) Marshal.FreeBSTR(invalidString);
            Marshal.FreeCoTaskMem(name);
        }
    }
    private static void NegativeDispatch(PrototypeSink sink,IntPtr pointer,Event entry)
    {
        foreach(int fault in new[]{1,2,4}) {
            using(var mock=new NativeMock(entry.args[0].iid)) {
                IntPtr memory=Zero(18); for(int i=0;i<18;i++) Marshal.WriteByte(memory,i,0xA5);
                IntPtr cancel=IntPtr.Add(memory,8); Marshal.WriteInt16(cancel,-1);
                int seen=0; sink.Active=true; sink.ThrowIndex=-1; sink.Observe=delegate { seen++; };
                try {
                    int hr=Dispatch(pointer,entry,new object[]{mock.Pointer,cancel},fault,123,delegate(byte[] before,byte[] after) {
                        Check(System.Linq.Enumerable.SequenceEqual(before,after),"rejected_variant_writeback");
                    });
                    Console.WriteLine("NEGATIVE_OBSERVATION fault="+fault+" hr="+hr.ToString("X8")+" entered="+seen+" cancel="+Marshal.ReadInt16(cancel));
                    int expected=fault==1?unchecked((int)0x80020003):fault==2?unchecked((int)0x8002000E):unchecked((int)0x80020005);
                    // CLR dispatch error details vary; rejection before entry and intact buffers are the gate.
                    Check((fault==4 && hr<0) || hr==expected || (fault==2 && hr==unchecked((int)0x80070057)),"negative_dispatch_hresult:"+fault+":"+hr.ToString("X8"));
                    Check(seen==0,"negative_dispatch_entered");
                    for(int i=0;i<18;i++) Check(Marshal.ReadByte(memory,i)==(i==8||i==9?0xFF:0xA5),"negative_dispatch_memory");
                    Console.WriteLine("NEGATIVE_DISPATCH PASS fault="+fault+" hr="+hr.ToString("X8"));
                } catch(Exception exc) { negativeFailures++; Console.WriteLine("NEGATIVE_DISPATCH FAIL "+exc.Message); }
                finally { sink.Observe=null; Marshal.FreeHGlobal(memory); GC.Collect(); GC.WaitForPendingFinalizers(); }
            }
        }
    }
    private static void Coercion(PrototypeSink sink,IntPtr pointer,Event entry)
    {
        foreach(bool active in new[]{false,true}) foreach(int scalar in new[]{0,123,-1}) {
            using(var mock=new NativeMock(entry.args[0].iid)) {
                IntPtr memory=Zero(18); for(int i=0;i<18;i++) Marshal.WriteByte(memory,i,0xA5);
                IntPtr cancel=IntPtr.Add(memory,8); Marshal.WriteInt16(cancel,-1);
                int seen=0; bool incoming=false; int afterTag=-1,afterValue=0;
                sink.Active=active; sink.Cancelled=false; sink.ThrowIndex=-1;
                sink.Observe=delegate(int index,object[] args) { seen++; incoming=(bool)args[1]; };
                try {
                    int hr=Dispatch(pointer,entry,new object[]{mock.Pointer,cancel},3,scalar,delegate(byte[] before,byte[] after) {
                        afterTag=BitConverter.ToInt16(after,0); afterValue=BitConverter.ToInt32(after,8);
                        // Scalar input supplies no caller bool pointer. The original VARIANT is unchanged.
                        Check(System.Linq.Enumerable.SequenceEqual(before,after),"coercion_variant_writeback");
                    });
                    Check(hr==0 && seen==1 && incoming==(scalar!=0),"coercion_conversion");
                    Check(sink.Cancelled==active,"coercion_active_latch");
                    for(int i=0;i<18;i++) Check(Marshal.ReadByte(memory,i)==(i==8||i==9?0xFF:0xA5),"coercion_unrelated_buffer");
                    Console.WriteLine("COERCION CHARACTERIZED active="+active+" scalar="+scalar+" incoming="+incoming+" tag_after="+afterTag+" value_after="+afterValue+" no_scalar_writeback=True unrelated_true_preserved=True");
                } finally { sink.Observe=null; Marshal.FreeHGlobal(memory); GC.Collect(); GC.WaitForPendingFinalizers(); }
            }
        }
    }
}
