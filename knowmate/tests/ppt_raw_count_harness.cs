using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Threading;
using System.IO;
using System.Reflection;
using AegisDesk.PptReader;
using TYPEATTR=System.Runtime.InteropServices.ComTypes.TYPEATTR;
class NativeObject {
 [UnmanagedFunctionPointer(CallingConvention.StdCall)]delegate int Q(IntPtr p,ref Guid id,out IntPtr r);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)]delegate int R(IntPtr p);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)]delegate int C(IntPtr p,out uint count);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)]delegate int T(IntPtr p,uint index,uint lcid,out IntPtr info);
 internal readonly IntPtr Pointer; internal int Refs=1,Adds,Releases,Quits,CollectionCalls,CountCalls,NamesCalls;
 internal int CountAcquireHostRefs; internal string Mode="ok";internal bool IsChild;internal ITypeInfo Info;Guid identity;
 internal Action<string> OnRpc;internal Action AtQuit;
 internal Dictionary<string,NativeObject> Children=new Dictionary<string,NativeObject>();
 Dictionary<int,string> names=new Dictionary<int,string>();Delegate[] roots;
 internal NativeObject(ITypeInfo info,bool child) {
  Info=info;IsChild=child;IntPtr a;info.GetTypeAttr(out a);identity=((TYPEATTR)Marshal.PtrToStructure(a,typeof(TYPEATTR))).guid;info.ReleaseTypeAttr(a);
  foreach(string n in child?new[]{"Count"}:new[]{"Presentations","ProtectedViewWindows","SlideShowWindows","DisplayAlerts","Quit"}){int[] id=new int[1];info.GetIDsOfNames(new[]{n},1,id);names[id[0]]=n;}
  roots=new Delegate[]{new Q(Query),new R(Add),new R(Release),new C(InfoCount),new T(TypeInfo),new RawCount.GetNames(GetNames),new RawCount.Invoke(Invoke)};
  IntPtr table=Marshal.AllocHGlobal(IntPtr.Size*7);for(int i=0;i<7;i++)Marshal.WriteIntPtr(table,i*IntPtr.Size,Marshal.GetFunctionPointerForDelegate(roots[i]));Pointer=Marshal.AllocHGlobal(IntPtr.Size);Marshal.WriteIntPtr(Pointer,table);
 }
 int Query(IntPtr p,ref Guid id,out IntPtr result){if(id==identity||id==RawCount.DispatchIid||id==new Guid("00000000-0000-0000-C000-000000000046")){result=Pointer;Add(p);return 0;}result=IntPtr.Zero;return unchecked((int)0x80004002);}
 internal int Add(IntPtr p){Interlocked.Increment(ref Adds);return Interlocked.Increment(ref Refs);}
 int Release(IntPtr p){Interlocked.Increment(ref Releases);return Interlocked.Decrement(ref Refs);}
 int InfoCount(IntPtr p,out uint n){n=1;return 0;}
 int TypeInfo(IntPtr p,uint index,uint lcid,out IntPtr result){result=Marshal.GetComInterfaceForObject(Info,typeof(ITypeInfo));return 0;}
 int GetNames(IntPtr p,ref Guid iid,IntPtr ns,uint count,uint lcid,out int id){
  NamesCalls++;id=-1;if(iid!=Guid.Empty||count!=1||lcid!=0)return unchecked((int)0x80070057);
  if(!IsChild&&Marshal.PtrToStringUni(Marshal.ReadIntPtr(ns))=="Presentations")CountAcquireHostRefs=Refs;
  if(Mode=="names_fatal")return unchecked((int)0x80010108);
  string n=Marshal.PtrToStringUni(Marshal.ReadIntPtr(ns));foreach(var pair in names)if(pair.Value==n){id=pair.Key;if(OnRpc!=null)OnRpc("names");return 0;}return unchecked((int)0x80020006);
 }
 int Invoke(IntPtr p,int id,ref Guid iid,uint lcid,ushort flags,ref RawCount.Parameters args,IntPtr result,ref RawCount.ErrorInfo error,out uint argError){
  argError=0;string n;if(!names.TryGetValue(id,out n))return unchecked((int)0x80020003);
  if(n=="Quit"){Quits++;if(AtQuit!=null)AtQuit();return 0;}
  if(n=="DisplayAlerts"){if((flags&4)==0)Marshal.GetNativeVariantForObject(2,result);return 0;}
  if(iid!=Guid.Empty||(flags!=2&&!(IsChild&&flags==3))||args.count!=0||args.namedCount!=0)return unchecked((int)0x80070057);
  if(IsChild){CountCalls++;if(OnRpc!=null)OnRpc("count");
   if(Mode=="count_fatal"||Mode=="fatal_cleanup")return unchecked((int)0x80010108);
   if(Mode=="exception_fatal"||Mode=="exception_nonfatal"||Mode=="bstr_success"){
    error.source=Marshal.StringToBSTR("mock");error.description=Marshal.StringToBSTR("not logged");error.helpFile=Marshal.StringToBSTR("mock");
    if(Mode!="bstr_success"){error.scode=Mode=="exception_fatal"?unchecked((int)0x80010108):unchecked((int)0x80004005);error.deferredFill=new IntPtr(1);return unchecked((int)0x80020009);}
   }
   if(Mode=="count_bstr"){Marshal.GetNativeVariantForObject("bad",result);return 0;}
   Marshal.WriteInt16(result,Mode=="count_i2"?(short)2:(short)3);Marshal.WriteInt32(result,8,Mode=="negative"?-1:Mode=="nonzero"?1:0);return 0;
  }
  CollectionCalls++;if(OnRpc!=null)OnRpc("collection");
  if(Mode=="collection_fatal")return unchecked((int)0x80010108);
  var child=Children[n];
  if(Mode=="null_dispatch"){Marshal.WriteInt16(result,9);return 0;}
  if(Mode=="byref"){Marshal.WriteInt16(result,0x4009);var slot=Marshal.AllocHGlobal(IntPtr.Size);Marshal.WriteIntPtr(slot,child.Pointer);Marshal.WriteIntPtr(result,8,slot);return 0;} // fixture owns slot until process exit
  child.Add(child.Pointer);Marshal.WriteInt16(result,Mode=="unknown"?(short)13:(short)9);Marshal.WriteIntPtr(result,8,child.Pointer);return 0;
 }
}
class Point:IConnectionPoint {
 internal int Unadvised;internal object Sink;public void Advise(object sink,out int cookie){Sink=sink;cookie=1;}public void Unadvise(int cookie){Unadvised++;}
 public void GetConnectionInterface(out Guid id){id=Guid.Empty;}public void GetConnectionPointContainer(out IConnectionPointContainer p){p=null;}public void EnumConnections(out IEnumConnections p){p=null;}
}
class Harness {
 static Assembly events;
 static string outputRoot;
 static int traceSequence;
 static object Sink(PptShutdownState state){return Activator.CreateInstance(events.GetType("AegisDesk.PptEvents.PptEventSink"),new object[]{state});}
 static void Cancel(Point point){object[] args={null,false};point.Sink.GetType().GetMethod("PresentationBeforeClose").Invoke(point.Sink,args);Check((bool)args[1],"actual_event_cancel");}
 static PptLifecycleTrace Trace(string label){
  string directory=Path.Combine(outputRoot,(++traceSequence)+"-"+label);Directory.CreateDirectory(directory);
  Environment.SetEnvironmentVariable("AEGIS_PPT_LIFECYCLE_TRACE","1");Environment.SetEnvironmentVariable("AEGIS_PPT_LIFECYCLE_DIR",directory);
  var trace=new PptLifecycleTrace();trace.BindOwner(Thread.CurrentThread.ManagedThreadId);return trace;
 }
 static string Request(PptShutdownGuard guard,PptLifecycleTrace trace){int token;string status=guard.TryQuitIfIdleTracked(false,out token);trace.FlushNormalReturn(token);return status;}

 static List<NativeObject> roots=new List<NativeObject>();static int checks;
 internal static void Check(bool ok,string reason){checks++;if(!ok)throw new Exception(reason);}
 [DllImport("oleaut32.dll",PreserveSig=false)]static extern void LoadRegTypeLib(ref Guid id,ushort major,ushort minor,int lcid,out ITypeLib lib);
 static ITypeInfo Find(ITypeLib lib,string target){for(int i=0;i<lib.GetTypeInfoCount();i++){string n,d,h;int c;lib.GetDocumentation(i,out n,out d,out c,out h);if(n==target){ITypeInfo t;lib.GetTypeInfo(i,out t);return t;}}throw new Exception(target);}
 static void Run(ITypeLib lib,string mode) {
  var host=new NativeObject(Find(lib,"_Application"),false);roots.Add(host);NativeObject first=null;
  bool shared=mode=="shared"||mode=="alias";
  foreach(string n in new[]{"Presentations","ProtectedViewWindows","SlideShowWindows"}){var child=shared&&first!=null?first:new NativeObject(Find(lib,n),true);if(first==null)first=child;roots.Add(child);host.Children[n]=child;child.Mode=mode;}
  host.Mode=mode;object app=Marshal.GetObjectForIUnknown(host.Pointer);object alias=mode=="alias"?Marshal.GetObjectForIUnknown(first.Pointer):null;
  if(alias!=null)Check((int)((dynamic)alias).Count==0,"alias_before");
  var baseline=new Dictionary<NativeObject,int>();foreach(var c in host.Children.Values)baseline[c]=c.Refs;
  PptShutdownState state=null;var point=new Point();
  var trace=Trace(mode);var guard=new PptShutdownGuard(app,Thread.CurrentThread.ManagedThreadId,ignored=>point,s=>{state=s;return Sink(s);},trace);
  Action<string> reenter=phase=>{if(mode=="cancel_"+phase)Cancel(point);if(mode=="broken_"+phase)state.MarkBroken();if(mode=="disconnect_"+phase)guard.Disconnect();};
  host.OnRpc=reenter;foreach(var c in host.Children.Values)c.OnRpc=reenter;
  host.AtQuit=()=>{foreach(var c in baseline){Check(c.Key.Refs==c.Value,"owned_collection_refs_not_zero_at_quit");}Console.WriteLine("QUIT owned_collection_refs=0 mode="+mode);};
  if(mode=="cleanup_failure"||mode=="fatal_cleanup")RawCount.Clear=p=>{int hr=RawCount.NativeClear(p);return hr<0?hr:unchecked((int)0x80004005);};
  string status;try{status=Request(guard,trace);}catch(COMException error){status=mode.Contains("fatal")?"transport":"unknown";Check(error.HResult==unchecked((int)(mode.Contains("fatal")?0x80010108:0x80004005)),"original_fatal_hresult_preserved");}finally{RawCount.Clear=RawCount.NativeClear;}
  bool success=mode=="ok"||shared;Check(host.Quits==(success?1:0),"quit_count_"+mode);
  Check(status==(success?"quit_requested":mode.StartsWith("cancel_")||mode=="nonzero"?"protected":mode.StartsWith("broken_")||mode=="cleanup_failure"?"unknown":mode.StartsWith("disconnect_")?"not_supported":mode.Contains("fatal")?"transport":"failed"),"status_"+mode+"="+status);
  foreach(var c in baseline)Check(c.Key.Refs==c.Value,"balanced_without_gc_"+mode);
  if(mode=="names_fatal"||mode.EndsWith("_names"))Check(host.CollectionCalls==0,"rpc_after_names_failure");
  if(mode.Contains("fatal")||mode.StartsWith("cancel_")||mode.StartsWith("broken_")||mode.StartsWith("disconnect_"))Check(host.CollectionCalls<=1,"extra_collection_rpc");
  if(mode=="cleanup_failure"){int before=host.CollectionCalls;Check(Request(guard,trace)=="not_supported"&&host.CollectionCalls==before&&host.Quits==0,"cleanup_failure_sticky_no_rpc");}
  if(alias!=null)Check((int)((dynamic)alias).Count==0,"alias_after");
  Console.WriteLine("PASS "+mode+" status="+status+" quit="+host.Quits);GC.KeepAlive(alias);GC.KeepAlive(app);
 }
 const int FatalA=unchecked((int)0x80010108),FatalB=unchecked((int)0x800706BA),Nonfatal=unchecked((int)0x80004005);
 static void Failure(ITypeLib lib,string label,string mode,Dictionary<string,int> faults,int expectedFatal,bool cancel,bool beforeClear) {
  var host=new NativeObject(Find(lib,"_Application"),false);roots.Add(host);
  foreach(string n in new[]{"Presentations","ProtectedViewWindows","SlideShowWindows"}){var c=new NativeObject(Find(lib,n),true);c.Mode=mode;roots.Add(c);host.Children[n]=c;}
  if(mode=="names_fatal")host.Mode=mode;
  object app=Marshal.GetObjectForIUnknown(host.Pointer);int hostBaseline=host.Refs;
  PptShutdownState state=null;var point=new Point();var trace=Trace(label);var guard=new PptShutdownGuard(app,Thread.CurrentThread.ManagedThreadId,ignored=>point,s=>{state=s;return Sink(s);},trace);
  if(cancel)foreach(var c in host.Children.Values)c.OnRpc=phase=>{if(phase=="count")Cancel(point);};
  var attempted=new Dictionary<string,int>();var recovery=new List<IntPtr>();
  RawCount.Cleanup=(resource,action)=>{if(!attempted.ContainsKey(resource))attempted[resource]=0;attempted[resource]++;action();int hr;if(faults.TryGetValue(resource,out hr))throw new COMException("injected_cleanup",hr);};
  if(beforeClear)RawCount.Clear=value=>{
   if(Marshal.ReadInt16(value)==9){
    // Transfer the still-owned contents into a fixture recovery buffer before
    // helper frees the original storage. Do not count recovery as helper success.
    byte[] bytes=new byte[RawCount.VariantBytes];Marshal.Copy(value,bytes,0,bytes.Length);IntPtr saved=Marshal.AllocHGlobal(bytes.Length);Marshal.Copy(bytes,0,saved,bytes.Length);recovery.Add(saved);
    return expectedFatal==0?Nonfatal:expectedFatal;
   }return RawCount.NativeClear(value);
  };
  string status=null;int observedFatal=0;
  try{status=Request(guard,trace);}catch(COMException error){status=expectedFatal==0?"unknown":"transport";observedFatal=error.HResult;}
  finally{RawCount.Cleanup=(resource,action)=>action();RawCount.Clear=RawCount.NativeClear;}
  Check(host.Quits==0,"failure_quit_"+label);
  Check(observedFatal==(expectedFatal==0?Nonfatal:expectedFatal),"fatal_precedence_"+label);
  Check(status==(expectedFatal==0?"unknown":"transport"),"failure_status_"+label);
  Check(point.Unadvised==0,"no_unadvise_after_fatal_or_unknown_"+label);
  Check(host.CollectionCalls<2,"no_further_collection_"+label);
  foreach(var entry in attempted)Check(entry.Value==1,"cleanup_once_"+label+"_"+entry.Key);
  foreach(string name in new[]{"Presentations.names","Presentations.text","collection.variant","collection.buffer","host.dispatch","host.unknown"})Check(attempted.ContainsKey(name),"remaining_cleanup_"+label+"_"+name);
  if(mode!="names_fatal")foreach(string name in new[]{"Count.source","Count.description","Count.helpFile","Count.names","Count.text","count.variant","count.buffer"})Check(attempted.ContainsKey(name),"remaining_bstr_cleanup_"+label+"_"+name);
  int calls=host.CollectionCalls;Check(Request(guard,trace)=="not_supported"&&host.CollectionCalls==calls&&host.Quits==0,"sticky_broken_"+label);
  Check(host.CountAcquireHostRefs!=0&&host.Refs==host.CountAcquireHostRefs-2,"host_owned_balance_"+label);
  if(beforeClear){Check(recovery.Count==1,"fixture_unreleased_variant");Check(host.Children["Presentations"].Refs==2,"helper_clear_did_not_succeed");}
  // Recovery is fixture-only, after all fail-closed assertions.
  foreach(IntPtr saved in recovery){Check(RawCount.NativeClear(saved)==0,"fixture_recovery");Marshal.FreeHGlobal(saved);}
  foreach(var c in host.Children.Values)Check(c.Refs==1,"post_fixture_recovery_balance_"+label);
  Console.WriteLine("PASS failure="+label+" fatal="+observedFatal.ToString("X8")+" unadvise="+point.Unadvised+" cleanups_once="+attempted.Count+" fixture_recovery="+recovery.Count);GC.KeepAlive(app);
 }
 static Dictionary<string,int> Faults(params object[] values){var d=new Dictionary<string,int>();for(int i=0;i<values.Length;i+=2)d[(string)values[i]]=(int)values[i+1];return d;}
 static void FailureMatrix(ITypeLib lib){
  Failure(lib,"invoke_fatal_bstr_nonfatal","exception_fatal",Faults("Count.source",Nonfatal),FatalA,false,false);
  Failure(lib,"invoke_fatal_bstr_otherfatal","exception_fatal",Faults("Count.source",FatalB,"Count.description",Nonfatal),FatalA,false,false);
  Failure(lib,"invoke_nonfatal_bstr_fatal","exception_nonfatal",Faults("Count.source",FatalB),FatalB,false,false);
  Failure(lib,"first_cleanup_fatal_later_nonfatal","exception_nonfatal",Faults("Count.source",FatalB,"Count.description",Nonfatal,"Count.text",Nonfatal,"host.unknown",Nonfatal),FatalB,false,false);
  Failure(lib,"first_cleanup_nonfatal_later_fatal","exception_nonfatal",Faults("Count.source",Nonfatal,"Count.description",FatalB,"Count.text",Nonfatal),FatalB,false,false);
  Failure(lib,"first_of_two_cleanup_fatals","exception_nonfatal",Faults("Count.source",FatalB,"Count.description",FatalA),FatalB,false,false);
  Failure(lib,"interrupted_primary_fatal_cleanup","bstr_success",Faults("Count.source",FatalB),FatalB,true,false);
  Failure(lib,"getids_fatal_cleanup_nonfatal","names_fatal",Faults("Presentations.names",Nonfatal,"Presentations.text",Nonfatal),FatalA,false,false);
  Failure(lib,"nonfatal_cleanup_then_read_fatal","exception_nonfatal",Faults("Count.source",Nonfatal,"collection.variant",FatalB,"host.unknown",Nonfatal),FatalB,false,false);
  Failure(lib,"read_first_fatal_later_nonfatal","exception_nonfatal",Faults("count.variant",FatalB,"collection.variant",Nonfatal,"host.unknown",Nonfatal),FatalB,false,false);
  Failure(lib,"read_first_nonfatal_later_fatal","exception_nonfatal",Faults("count.variant",Nonfatal,"collection.variant",FatalB),FatalB,false,false);
  Failure(lib,"all_cleanup_nonfatal","exception_nonfatal",Faults("Count.source",Nonfatal,"Count.description",Nonfatal,"Count.helpFile",Nonfatal,"Count.names",Nonfatal,"Count.text",Nonfatal,"count.variant",Nonfatal,"count.buffer",Nonfatal,"collection.variant",Nonfatal,"collection.buffer",Nonfatal,"host.dispatch",Nonfatal,"host.unknown",Nonfatal),0,false,false);
  Failure(lib,"clear_before_nonfatal","bstr_success",Faults(),0,false,true);
  Failure(lib,"clear_before_fatal","bstr_success",Faults(),FatalB,false,true);
 }
 static void ValidateCount(ITypeInfo info,string name) {
  IntPtr ap;info.GetTypeAttr(out ap);var a=(TYPEATTR)Marshal.PtrToStructure(ap,typeof(TYPEATTR));bool found=false;
  for(int i=0;i<a.cFuncs;i++){IntPtr fp;info.GetFuncDesc(i,out fp);var f=(System.Runtime.InteropServices.ComTypes.FUNCDESC)Marshal.PtrToStructure(fp,typeof(System.Runtime.InteropServices.ComTypes.FUNCDESC));string n,d,h;int ctx;info.GetDocumentation(f.memid,out n,out d,out ctx,out h);
   if(n=="Count"){Check(f.invkind==System.Runtime.InteropServices.ComTypes.INVOKEKIND.INVOKE_PROPERTYGET&&f.cParams==0&&f.elemdescFunc.tdesc.vt==3,"Count_contract_"+name);found=true;Console.WriteLine("TYPELIB "+name+".Count PROPERTYGET args=0 VT_I4 dispid="+f.memid);}
   info.ReleaseFuncDesc(fp);
  }info.ReleaseTypeAttr(ap);Check(found,"Count_missing");
 }
 [STAThread]static int Main(string[] args){try{
  events=Assembly.LoadFrom(args[0]);outputRoot=args[1];Directory.CreateDirectory(outputRoot);
  Check(Marshal.SizeOf(typeof(RawCount.Parameters))==(IntPtr.Size==8?24:16),"DISPPARAMS_size");
  Check(Marshal.SizeOf(typeof(RawCount.ErrorInfo))==(IntPtr.Size==8?64:32),"EXCEPINFO_size");
  Check(Marshal.OffsetOf(typeof(RawCount.ErrorInfo),"scode").ToInt32()==(IntPtr.Size==8?56:28),"EXCEPINFO_scode");
  Console.WriteLine("ABI ptr="+IntPtr.Size+" variant="+RawCount.VariantBytes);
  Guid id=new Guid("91493440-5A91-11CF-8700-00AA0060263B");ITypeLib lib;LoadRegTypeLib(ref id,2,12,0,out lib);
  foreach(string name in new[]{"Presentations","ProtectedViewWindows","SlideShowWindows"})ValidateCount(Find(lib,name),name);
  foreach(string mode in new[]{"ok","shared","alias","unknown","byref","null_dispatch","count_bstr","count_i2","negative","nonzero","names_fatal","collection_fatal","count_fatal","exception_fatal","fatal_cleanup","cleanup_failure","cancel_names","cancel_collection","cancel_count","broken_names","broken_collection","broken_count","disconnect_names","disconnect_collection","disconnect_count"})Run(lib,mode);
  FailureMatrix(lib);
  Check(Directory.GetFiles(outputRoot,"host-*.log",SearchOption.AllDirectories).Length>=3,"real_diagnostic_files_missing");
  Console.WriteLine("PASS INTEGRATED checks="+checks+" no_gc_no_forced_rcw_release");GC.KeepAlive(roots);return 0;
 }catch(Exception e){Console.WriteLine(e);return 1;}}
}
