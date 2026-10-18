using System;
using System.Runtime.InteropServices;

namespace AegisDesk.PptReader {
internal sealed class CountInterrupted : Exception {
 internal readonly string Outcome;
 internal CountInterrupted(string outcome) { Outcome=outcome; }
}
internal sealed class CountCleanupFailed : Exception {
 internal CountCleanupFailed(Exception cause):base("raw_count_cleanup_failed",cause){}
}
// Read only the three shutdown collection counts without creating collection RCWs.
internal static class RawCount {
 internal static readonly Guid DispatchIid=new Guid("00020400-0000-0000-C000-000000000046");
 [UnmanagedFunctionPointer(CallingConvention.StdCall)]
 internal delegate int GetNames(IntPtr self,ref Guid iid,IntPtr names,uint count,uint lcid,out int id);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)]
 internal delegate int Invoke(IntPtr self,int id,ref Guid iid,uint lcid,ushort flags,ref Parameters args,IntPtr result,ref ErrorInfo error,out uint argError);
 [StructLayout(LayoutKind.Sequential)] internal struct Parameters {internal IntPtr args,named;internal uint count,namedCount;}
 [StructLayout(LayoutKind.Sequential)] internal struct ErrorInfo {
  internal ushort code,reserved;internal IntPtr source,description,helpFile;
  internal uint helpContext;internal IntPtr reservedPointer,deferredFill;internal int scode;
 }
 [DllImport("oleaut32.dll",EntryPoint="VariantClear",PreserveSig=true)]
 internal static extern int NativeClear(IntPtr value);
 internal sealed class Errors {
  internal Exception Primary,FirstCleanup,FirstCleanupFatal;
  internal static COMException Fatal(Exception error) {
   for(int i=0;error!=null&&i<16;i++,error=error.InnerException) {
    var com=error as COMException;if(com==null)continue;
    switch(unchecked((uint)com.HResult)) {
     case 0x800706BA:case 0x800706BE:case 0x800706BF:case 0x80010006:
     case 0x80010007:case 0x80010012:case 0x80010108:case 0x800401FD:return com;
    }
   }return null;
  }
  internal void Clean(string label,Action action) {
   try {action();}catch(Exception error) {
#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
    if(ExternalTrace.Current!=null)ExternalTrace.Current.Error(error,true);
#endif
    if(FirstCleanup==null)FirstCleanup=error;
    if(FirstCleanupFatal==null&&Fatal(error)!=null)FirstCleanupFatal=error;
   }
  }
  internal void ThrowIfAny() {
   Exception chosen=(Exception)Fatal(Primary)??FirstCleanupFatal??Primary??FirstCleanup;
   if(FirstCleanup!=null)throw new CountCleanupFailed(chosen);
   if(Primary!=null)throw Primary;
  }
 }
 internal static int VariantBytes {get{return IntPtr.Size==8?24:16;}}
 static Delegate Slot(IntPtr pointer,int index,Type type) {
  return Marshal.GetDelegateForFunctionPointer(Marshal.ReadIntPtr(Marshal.ReadIntPtr(pointer),index*IntPtr.Size),type);
 }
 static void HResult(int hr) { if(hr<0)throw new COMException("raw_count_rpc",hr); }
 static IntPtr AllocateVariant() {var p=Marshal.AllocHGlobal(VariantBytes);for(int i=0;i<VariantBytes;i++)Marshal.WriteByte(p,i,0);return p;}
 static void ClearVariant(ref IntPtr p,string label) {
  if(p==IntPtr.Zero)return;var owned=p;p=IntPtr.Zero;
  var errors=new Errors();
  errors.Clean(label+".variant",()=>HResult(NativeClear(owned)));
  errors.Clean(label+".buffer",()=>Marshal.FreeHGlobal(owned));
  errors.ThrowIfAny();
 }
 static void FreeError(ref ErrorInfo error,Errors errors,string label) {
  IntPtr source=error.source,description=error.description,helpFile=error.helpFile;
  error=new ErrorInfo();
  if(source!=IntPtr.Zero)errors.Clean(label+".source",()=>Marshal.FreeBSTR(source));
  if(description!=IntPtr.Zero)errors.Clean(label+".description",()=>Marshal.FreeBSTR(description));
  if(helpFile!=IntPtr.Zero)errors.Clean(label+".helpFile",()=>Marshal.FreeBSTR(helpFile));
 }
 static void Property(IntPtr dispatch,string name,IntPtr result,Action check) {
  IntPtr text=IntPtr.Zero,names=IntPtr.Zero;var error=new ErrorInfo();var errors=new Errors();
  try {
   text=Marshal.StringToCoTaskMemUni(name);names=Marshal.AllocHGlobal(IntPtr.Size);Marshal.WriteIntPtr(names,text);
   Guid empty=Guid.Empty;int id;
   check();int hr=((GetNames)Slot(dispatch,5,typeof(GetNames)))(dispatch,ref empty,names,1,0,out id);
   HResult(hr);check();
   var args=new Parameters();uint argError;
   check();hr=((Invoke)Slot(dispatch,6,typeof(Invoke)))(dispatch,id,ref empty,0,2,ref args,result,ref error,out argError);
   // Deferred exception filling would be an extra RPC: never invoke it.
   if(hr==unchecked((int)0x80020009) && error.scode<0)HResult(error.scode);
   HResult(hr);check();
   if(error.deferredFill!=IntPtr.Zero)throw new InvalidOperationException("deferred_error_unsupported");
  } catch(Exception caught) {errors.Primary=caught;
#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
   if(ExternalTrace.Current!=null)ExternalTrace.Current.Error(caught,false);
#endif
  }
  finally {
   FreeError(ref error,errors,name);
   if(names!=IntPtr.Zero)errors.Clean(name+".names",()=>Marshal.FreeHGlobal(names));
   if(text!=IntPtr.Zero)errors.Clean(name+".text",()=>Marshal.FreeCoTaskMem(text));
  }
  errors.ThrowIfAny();
 }
 internal static int Read(object host,string collection,Action check) {
  if(host==null||!Marshal.IsComObject(host))throw new ArgumentException("native_host_required");
  if(collection!="Presentations"&&collection!="ProtectedViewWindows"&&collection!="SlideShowWindows")throw new ArgumentException("collection");
  IntPtr unknown=IntPtr.Zero,dispatch=IntPtr.Zero,child=IntPtr.Zero,value=IntPtr.Zero;
  var errors=new Errors();int count=0;
  try {
   check();unknown=Marshal.GetIUnknownForObject(host);check();
   Guid iid=DispatchIid;check();HResult(Marshal.QueryInterface(unknown,ref iid,out dispatch));check();
   child=AllocateVariant();Property(dispatch,collection,child,check);
   if(Marshal.ReadInt16(child)!=9 || Marshal.ReadIntPtr(child,8)==IntPtr.Zero)throw new InvalidOperationException("collection_variant_unsupported");
   value=AllocateVariant();Property(Marshal.ReadIntPtr(child,8),"Count",value,check);
   if(Marshal.ReadInt16(value)!=3)throw new InvalidOperationException("count_variant_unsupported");
   count=Marshal.ReadInt32(value,8);if(count<0)throw new InvalidOperationException("negative_count");
  } catch(Exception e) {errors.Primary=e;
#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
   if(ExternalTrace.Current!=null)ExternalTrace.Current.Error(e,false);
#endif
  }
  finally {
   // One balanced release per owned acquisition; no ReleaseComObject or retries.
   errors.Clean("count.scope",()=>ClearVariant(ref value,"count"));
   errors.Clean("collection.scope",()=>ClearVariant(ref child,"collection"));
   if(dispatch!=IntPtr.Zero)errors.Clean("host.dispatch",()=>Marshal.Release(dispatch));
   if(unknown!=IntPtr.Zero)errors.Clean("host.unknown",()=>Marshal.Release(unknown));
   GC.KeepAlive(host);
  }
  errors.ThrowIfAny();
  check();return count;
 }
}
}
