// Managed metadata adapters own native acquisitions without creating metadata RCWs.
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using IMPLTYPEFLAGS=System.Runtime.InteropServices.ComTypes.IMPLTYPEFLAGS;
using DISPPARAMS=System.Runtime.InteropServices.ComTypes.DISPPARAMS;
using INVOKEKIND=System.Runtime.InteropServices.ComTypes.INVOKEKIND;
using TYPEKIND=System.Runtime.InteropServices.ComTypes.TYPEKIND;
namespace AegisDesk.PptReader {
sealed class PptMetadataScope {
 internal sealed class Unresolved:PptCleanupOutcomeUnknown {
  // Preserve the ownership ledger for inspection. No retry/teardown API is exposed.
  internal readonly PptMetadataScope Ledger;
  internal Unresolved(PptMetadataScope ledger,Exception cause):base(cause){Ledger=ledger;}
 }
 sealed class Entry {internal IntPtr Pointer,Owner;internal int Slot;internal PointerPhase Phase;}
 readonly List<Entry> entries=new List<Entry>();readonly PptCleanupErrors errors=new PptCleanupErrors();
 bool finishing,finished;int cleanupDepth;
 [ThreadStatic]static PptMetadataScope current;
 internal static void Operational(){if(current!=null&&(current.errors.Poison||current.finishing||current.finished))throw new InvalidOperationException("metadata_operation_blocked");}
 internal void CheckHr(int h){if(h<0){var e=new COMException("owned_metadata_rpc",h);errors.Observe(e,true);throw e;}}
 internal IntPtr Own(IntPtr p,string kind){Register(p,-1,IntPtr.Zero);return p;}
 internal IntPtr Descriptor(IntPtr owner,int slot,IntPtr p){Register(p,slot,owner);return p;}
 Entry Register(IntPtr p,int slot,IntPtr owner){if(p==IntPtr.Zero)return null;var e=new Entry{Pointer=p,Slot=slot,Owner=owner};entries.Add(e);return e;}
 bool OwnerBlocked(Entry e){if(e.Slot!=-1)return false;foreach(var d in entries)if(d.Slot>=0&&d.Owner==e.Pointer&&d.Phase!=PointerPhase.Returned)return true;return false;}
 void Release(Entry e){
  if(e==null||e.Phase!=PointerPhase.Owned||OwnerBlocked(e))return;
  e.Phase=PointerPhase.ReleaseStarted;cleanupDepth++;
  try{
   if(e.Slot==-1)Marshal.Release(e.Pointer);
   else if(e.Slot==-2)Marshal.FreeBSTR(e.Pointer);
   else if(e.Slot==-3)Marshal.FreeHGlobal(e.Pointer);
   else PptNativeMetadata.Slot<PptNativeMetadata.Free>(e.Owner,e.Slot)(e.Owner,e.Pointer);
   e.Phase=PointerPhase.Returned;
  }catch(Exception e2){e.Phase=PointerPhase.OutcomeUnknown;errors.Unknown=true;errors.Observe(e2,false);throw;}
  finally{cleanupDepth--;}
 }
 internal void ReturnDescriptor(IntPtr owner,int slot,IntPtr p){
  for(int i=entries.Count-1;i>=0;i--){var e=entries[i];if(e.Phase==PointerPhase.Owned&&e.Owner==owner&&e.Slot==slot&&e.Pointer==p){Release(e);return;}}
  throw new InvalidOperationException("unowned_or_attempted_descriptor");
 }
 void Finish(){
  if(finishing||finished||cleanupDepth!=0)return;finishing=true;
  try{for(int pass=0;pass<2;pass++)for(int i=entries.Count-1;i>=0;i--){var e=entries[i];if((pass==0)!=(e.Slot>=0))continue;try{Release(e);}catch(Exception x){errors.Observe(x,false);}}}
  finally{finishing=false;finished=true;}
 }
 internal void Documentation(IntPtr owner,int slot,int id,out string name,out string doc,out int context,out string help){
  name=doc=help=null;context=0;var localErrors=new PptCleanupErrors();var strings=new List<Entry>();
  IntPtr buffer=Marshal.AllocHGlobal(IntPtr.Size*4);Entry allocation=Register(buffer,-3,IntPtr.Zero);
  try{
   for(int i=0;i<IntPtr.Size*4;i++)Marshal.WriteByte(buffer,i,0);
   int h=0;
   try{h=PptNativeMetadata.Slot<PptNativeMetadata.Doc>(owner,slot)(owner,id,buffer,IntPtr.Add(buffer,IntPtr.Size),IntPtr.Add(buffer,IntPtr.Size*2),IntPtr.Add(buffer,IntPtr.Size*3));}
   finally{foreach(int index in new[]{0,1,3})strings.Add(Register(Marshal.ReadIntPtr(buffer,index*IntPtr.Size),-2,IntPtr.Zero));}
   CheckHr(h);
   IntPtr a=Marshal.ReadIntPtr(buffer),b=Marshal.ReadIntPtr(buffer,IntPtr.Size),c=Marshal.ReadIntPtr(buffer,IntPtr.Size*3);
   name=a==IntPtr.Zero?null:Marshal.PtrToStringBSTR(a);doc=b==IntPtr.Zero?null:Marshal.PtrToStringBSTR(b);help=c==IntPtr.Zero?null:Marshal.PtrToStringBSTR(c);context=Marshal.ReadInt32(buffer,IntPtr.Size*2);
  }catch(Exception x){localErrors.Observe(x,true);errors.Observe(x,true);}
  finally{foreach(var e in strings)try{Release(e);}catch(Exception x){localErrors.Observe(x,false);}try{Release(allocation);}catch(Exception x){localErrors.Observe(x,false);}}
  localErrors.Throw();
 }
 internal static void Validate(object host){
  var scope=new PptMetadataScope();var previous=current;current=scope;
  try{
   try{
    IntPtr unknown=scope.Own(Marshal.GetIUnknownForObject(host),"host_unknown"),dispatch;
    Guid iid=new Guid("00020400-0000-0000-C000-000000000046");int h=Marshal.QueryInterface(unknown,ref iid,out dispatch);scope.Own(dispatch,"dispatch");scope.CheckHr(h);
    IntPtr info;h=PptNativeMetadata.Slot<PptNativeMetadata.Info>(dispatch,4)(dispatch,0,0,out info);scope.Own(info,"typeinfo");scope.CheckHr(h);
    var hostInfo=new PptOwnedTypeInfo(scope,info);ITypeLib library;int index;hostInfo.GetContainingTypeLib(out library,out index);
    ITypeInfo events;Guid id=PptConnectionLease.EventIid;library.GetTypeInfoOfGuid(ref id,out events);
    PptEventValidator.Validate(events);
   }catch(Exception x){scope.errors.Observe(x,true);}
   scope.Finish();if(scope.errors.Unknown)throw new Unresolved(scope,scope.errors.Chosen);scope.errors.Throw();
  }finally{current=previous;}
 }
}

static class PptNativeMetadata {
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] internal delegate int O(IntPtr p,out IntPtr r);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] internal delegate int UO(IntPtr p,uint n,out IntPtr r);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] internal delegate int UI(IntPtr p,uint n,out int r);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] internal delegate int GO(IntPtr p,ref Guid n,out IntPtr r);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] internal delegate int OI(IntPtr p,out IntPtr r,out int n);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] internal delegate int Doc(IntPtr p,int id,IntPtr a,IntPtr b,IntPtr c,IntPtr d);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] internal delegate int Info(IntPtr p,uint index,uint lcid,out IntPtr r);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] internal delegate void Free(IntPtr p,IntPtr r);
 internal static T Slot<T>(IntPtr p,int index)where T:class {
  if(typeof(T)!=typeof(Free))PptMetadataScope.Operational();
  Delegate d=Marshal.GetDelegateForFunctionPointer(Marshal.ReadIntPtr(Marshal.ReadIntPtr(p),index*IntPtr.Size),typeof(T));
  if(d.GetType()!=typeof(T))d=Delegate.CreateDelegate(typeof(T),d.Target,d.Method);
  return d as T;
 }
 internal static void Hr(int hr){if(hr<0)throw new COMException("owned_metadata_rpc",hr);}
 internal const int FatalDepth=16;
 internal static bool Fatal(Exception e){for(int n=0;e!=null&&n<FatalDepth;n++,e=e.InnerException)if(e is COMException){uint h=unchecked((uint)e.HResult);if(h==0x800706BA||h==0x800706BE||h==0x800706BF||h==0x80010006||h==0x80010007||h==0x80010012||h==0x80010108||h==0x800401FD)return true;}return false;}
}
sealed class PptOwnedTypeInfo:ITypeInfo {
 readonly PptMetadataScope scope;readonly IntPtr p;
 internal PptOwnedTypeInfo(PptMetadataScope s,IntPtr value){if(value==IntPtr.Zero)throw new COMException("metadata_null_interface",unchecked((int)0x80004003));scope=s;p=value;}
 public void GetTypeAttr(out IntPtr value){int h=PptNativeMetadata.Slot<PptNativeMetadata.O>(p,3)(p,out value);scope.Descriptor(p,19,value);scope.CheckHr(h);}
 public void GetFuncDesc(int index,out IntPtr value){int h=PptNativeMetadata.Slot<PptNativeMetadata.UO>(p,5)(p,(uint)index,out value);scope.Descriptor(p,20,value);scope.CheckHr(h);}
 public void GetRefTypeOfImplType(int index,out int href){scope.CheckHr(PptNativeMetadata.Slot<PptNativeMetadata.UI>(p,8)(p,(uint)index,out href));}
 public void GetImplTypeFlags(int index,out IMPLTYPEFLAGS flags){int n;scope.CheckHr(PptNativeMetadata.Slot<PptNativeMetadata.UI>(p,9)(p,(uint)index,out n));flags=(IMPLTYPEFLAGS)n;}
 public void GetRefTypeInfo(int href,out ITypeInfo info){IntPtr value;int h=PptNativeMetadata.Slot<PptNativeMetadata.UO>(p,14)(p,unchecked((uint)href),out value);scope.Own(value,"referenced_typeinfo");scope.CheckHr(h);info=new PptOwnedTypeInfo(scope,value);}
 public void GetContainingTypeLib(out ITypeLib lib,out int index){IntPtr value;int h=PptNativeMetadata.Slot<PptNativeMetadata.OI>(p,18)(p,out value,out index);scope.Own(value,"containing_typelib");scope.CheckHr(h);lib=new PptOwnedTypeLib(scope,value);}
 public void GetDocumentation(int id,out string name,out string doc,out int context,out string help){scope.Documentation(p,12,id,out name,out doc,out context,out help);}
 public void ReleaseTypeAttr(IntPtr value){scope.ReturnDescriptor(p,19,value);}
 public void ReleaseFuncDesc(IntPtr value){scope.ReturnDescriptor(p,20,value);}
 public void ReleaseVarDesc(IntPtr value){scope.ReturnDescriptor(p,21,value);}
 public void GetTypeComp(out ITypeComp c){throw new NotSupportedException();}
 public void GetVarDesc(int n,out IntPtr p){throw new NotSupportedException();}
 public void GetNames(int id,string[] names,int max,out int count){throw new NotSupportedException();}
 public void GetIDsOfNames(string[] names,int count,int[] ids){throw new NotSupportedException();}
 public void Invoke(object obj,int id,short flags,ref DISPPARAMS args,IntPtr result,IntPtr exception,out int argError){throw new NotSupportedException();}
 public void GetDllEntry(int id,INVOKEKIND kind,IntPtr a,IntPtr b,IntPtr c){throw new NotSupportedException();}
 public void AddressOfMember(int id,INVOKEKIND kind,out IntPtr r){throw new NotSupportedException();}
 public void CreateInstance(object outer,ref Guid iid,out object result){throw new NotSupportedException();}
 public void GetMops(int id,out string value){throw new NotSupportedException();}
}
sealed class PptOwnedTypeLib:ITypeLib {
 readonly PptMetadataScope scope;readonly IntPtr p;internal PptOwnedTypeLib(PptMetadataScope s,IntPtr value){if(value==IntPtr.Zero)throw new COMException("metadata_null_interface",unchecked((int)0x80004003));scope=s;p=value;}
 public void GetTypeInfoOfGuid(ref Guid id,out ITypeInfo info){IntPtr value;int h=PptNativeMetadata.Slot<PptNativeMetadata.GO>(p,6)(p,ref id,out value);scope.Own(value,"event_typeinfo");scope.CheckHr(h);info=new PptOwnedTypeInfo(scope,value);}
 public void GetLibAttr(out IntPtr value){int h=PptNativeMetadata.Slot<PptNativeMetadata.O>(p,7)(p,out value);scope.Descriptor(p,12,value);scope.CheckHr(h);}
 public void ReleaseTLibAttr(IntPtr value){scope.ReturnDescriptor(p,12,value);}
 public void GetDocumentation(int id,out string name,out string doc,out int context,out string help){scope.Documentation(p,9,id,out name,out doc,out context,out help);}
 public int GetTypeInfoCount(){throw new NotSupportedException();}
 public void GetTypeInfo(int index,out ITypeInfo info){throw new NotSupportedException();}
 public void GetTypeInfoType(int index,out TYPEKIND kind){throw new NotSupportedException();}
 public void GetTypeComp(out ITypeComp comp){throw new NotSupportedException();}
 public bool IsName(string name,int hash){throw new NotSupportedException();}
 public void FindName(string name,int hash,ITypeInfo[] info,int[] ids,ref short found){throw new NotSupportedException();}
}

}
