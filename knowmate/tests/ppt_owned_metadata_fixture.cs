using AegisDesk.PptReader;
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
// Offline native forwarding objects only. Each owns one backing-interface reference.
// Counters measure the proxy graph seen by the unchanged installed guard, not Office internals.
class Meta { public static bool ForceDocStrings,DocFatal;
 public static bool BadContract;readonly Dictionary<IntPtr,IntPtr> attrCopies=new Dictionary<IntPtr,IntPtr>();public static bool PartialFailure;static Meta heldAlias;
 public static void HoldMetadataAlias(){foreach(var m in pool.Values)if(m.label=="EApplication"){heldAlias=m;Marshal.AddRef(m.Pointer);Console.WriteLine("META_ALIAS_OWNED id="+m.id);return;}throw new Exception("alias_target_missing");}
 public static void CheckAndDropMetadataAlias(){Need(heldAlias!=null,"no_alias");var m=heldAlias;IntPtr desc;
  var get=(O)Marshal.GetDelegateForFunctionPointer(Marshal.ReadIntPtr(Marshal.ReadIntPtr(m.Pointer),3*IntPtr.Size),typeof(O));
  var free=(Free)Marshal.GetDelegateForFunctionPointer(Marshal.ReadIntPtr(Marshal.ReadIntPtr(m.Pointer),19*IntPtr.Size),typeof(Free));
  Marshal.ThrowExceptionForHR(get(m.Pointer,out desc));free(m.Pointer,desc);Marshal.Release(m.Pointer);heldAlias=null;Console.WriteLine("META_EXTERNAL_ALIAS_PASS after_scope_disposal");}

 [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int Q(IntPtr p,ref Guid id,out IntPtr r);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int R(IntPtr p);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int O(IntPtr p,out IntPtr r);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int UO(IntPtr p,uint n,out IntPtr r);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int UI(IntPtr p,uint n,out int r);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int GO(IntPtr p,ref Guid id,out IntPtr r);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int OI(IntPtr p,out IntPtr r,out int n);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int Names(IntPtr p,int id,IntPtr names,uint count,out uint got);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int Ids(IntPtr p,IntPtr names,uint count,IntPtr ids);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int Inv(IntPtr p,IntPtr obj,int id,ushort flags,IntPtr args,IntPtr result,IntPtr ex,IntPtr err);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int Doc(IntPtr p,int id,IntPtr name,IntPtr doc,IntPtr context,IntPtr help);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int Dll(IntPtr p,int id,int kind,IntPtr a,IntPtr b,IntPtr c);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int Member(IntPtr p,int id,int kind,out IntPtr r);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int Create(IntPtr p,IntPtr outer,ref Guid id,out IntPtr r);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int Mops(IntPtr p,int id,out IntPtr r);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int IsName(IntPtr p,IntPtr name,uint hash,out int found);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int FindName(IntPtr p,IntPtr name,uint hash,IntPtr infos,IntPtr ids,IntPtr count);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate void Free(IntPtr p,IntPtr descriptor);
 static Guid Unknown=new Guid("00000000-0000-0000-C000-000000000046");
 static Guid TI=new Guid("00020401-0000-0000-C000-000000000046"),TL=new Guid("00020402-0000-0000-C000-000000000046");
 static Dictionary<IntPtr,Meta> pool=new Dictionary<IntPtr,Meta>();
 public IntPtr Pointer; IntPtr backing; Guid iid; string kind; int id,refs=1,adds,releases; Delegate[] roots;
 string label; int attrBuffers,funcBuffers,varBuffers;
 Dictionary<string,int> origins=new Dictionary<string,int>(); Dictionary<Guid,int> queries=new Dictionary<Guid,int>();
 Dictionary<int,Delegate> forwards=new Dictionary<int,Delegate>();
 T F<T>(int slot) where T:class {Delegate d;if(!forwards.TryGetValue(slot,out d)){d=Marshal.GetDelegateForFunctionPointer(Marshal.ReadIntPtr(Marshal.ReadIntPtr(backing),slot*IntPtr.Size),typeof(T));forwards.Add(slot,d);}return d as T;}
 static void Need(bool b,string why){if(!b)throw new Exception(why);}
 public static IntPtr WrapOwned(IntPtr raw,string kind,string origin){
  Need(raw!=IntPtr.Zero,"null_metadata_return");IntPtr canonical=IntPtr.Zero;
  try {Marshal.ThrowExceptionForHR(Marshal.QueryInterface(raw,ref Unknown,out canonical));Meta m;
   if(!pool.TryGetValue(canonical,out m)){m=new Meta(raw,kind,pool.Count+1);pool.Add(canonical,m);Console.WriteLine("META_NEW id="+m.id+" kind="+kind+" backing_identity="+canonical+" proxy_identity="+m.Pointer+" backing_owner_refs=1");}
   Need(m.kind==kind,"cross_interface_identity_requires_tearoff_not_modeled");int n;m.origins.TryGetValue(origin,out n);m.origins[origin]=n+1;m.Add(m.Pointer);Console.WriteLine("META_RETURN id="+m.id+" origin="+origin+" refs="+m.refs);return m.Pointer;
  }finally{if(canonical!=IntPtr.Zero)Marshal.Release(canonical);Marshal.Release(raw);}
 }
 public static IntPtr FromInfo(ITypeInfo info){return WrapOwned(Marshal.GetComInterfaceForObject(info,typeof(ITypeInfo)),"TI","host.GetTypeInfo");}
 Meta(IntPtr raw,string k,int number){backing=raw;Marshal.AddRef(raw);kind=k;id=number;iid=k=="TI"?TI:TL;
  IntPtr name=Marshal.AllocHGlobal(IntPtr.Size);Marshal.WriteIntPtr(name,IntPtr.Zero);
  try{Marshal.ThrowExceptionForHR(F<Doc>(k=="TI"?12:9)(backing,-1,name,IntPtr.Zero,IntPtr.Zero,IntPtr.Zero));IntPtr b=Marshal.ReadIntPtr(name);label=Marshal.PtrToStringBSTR(b);Marshal.FreeBSTR(b);}finally{Marshal.FreeHGlobal(name);}
  Console.WriteLine("META_LABEL id="+id+" label="+label);
  var slots=new List<Delegate>{new Q(Query),new R(Add),new R(Release)};
  if(k=="TI"){
   slots.Add(new O((IntPtr p,out IntPtr r)=>{int h=F<O>(3)(backing,out r);if(h>=0){attrBuffers++;if(BadContract&&label=="EApplication"){
 var attr=(System.Runtime.InteropServices.ComTypes.TYPEATTR)Marshal.PtrToStructure(r,typeof(System.Runtime.InteropServices.ComTypes.TYPEATTR));attr.guid=Guid.Empty;
 IntPtr copy=Marshal.AllocHGlobal(Marshal.SizeOf(typeof(System.Runtime.InteropServices.ComTypes.TYPEATTR)));Marshal.StructureToPtr(attr,copy,false);attrCopies.Add(copy,r);r=copy;}}return h;}));
   slots.Add(new O((IntPtr p,out IntPtr r)=>F<O>(4)(backing,out r)));
   slots.Add(new UO((IntPtr p,uint n,out IntPtr r)=>{int h=F<UO>(5)(backing,n,out r);if(h>=0)funcBuffers++;return h;}));
   slots.Add(new UO((IntPtr p,uint n,out IntPtr r)=>{int h=F<UO>(6)(backing,n,out r);if(h>=0)varBuffers++;return h;}));
   slots.Add(new Names((IntPtr p,int n,IntPtr a,uint b,out uint c)=>F<Names>(7)(backing,n,a,b,out c)));
   slots.Add(new UI((IntPtr p,uint n,out int r)=>F<UI>(8)(backing,n,out r)));
   slots.Add(new UI((IntPtr p,uint n,out int r)=>F<UI>(9)(backing,n,out r)));
   slots.Add(new Ids((p,a,b,c)=>F<Ids>(10)(backing,a,b,c)));
   slots.Add(new Inv((p,a,b,c,d,e,f,g)=>F<Inv>(11)(backing,a,b,c,d,e,f,g)));
   slots.Add(new Doc((p,a,b,c,d,e)=>{int h=F<Doc>(12)(backing,a,b,c,d,e);if(ForceDocStrings){foreach(IntPtr output in new[]{b,c,e})if(output!=IntPtr.Zero&&Marshal.ReadIntPtr(output)==IntPtr.Zero)Marshal.WriteIntPtr(output,Marshal.StringToBSTR("fixture"));}if(DocFatal){DocFatal=false;return unchecked((int)0x80010108);}return h;}));
   slots.Add(new Dll((p,a,b,c,d,e)=>F<Dll>(13)(backing,a,b,c,d,e)));
   slots.Add(new UO((IntPtr p,uint n,out IntPtr r)=>{int h=F<UO>(14)(backing,n,out r);if(h>=0)r=WrapOwned(r,"TI","TI"+id+".GetRefTypeInfo("+n+")");if(PartialFailure){PartialFailure=false;Console.WriteLine("META_PARTIAL_FATAL nonnull_owned_result");return unchecked((int)0x80010108);}return h;}));
   slots.Add(new Member((IntPtr p,int a,int b,out IntPtr r)=>F<Member>(15)(backing,a,b,out r)));
   slots.Add(new Create((IntPtr p,IntPtr a,ref Guid b,out IntPtr r)=>{r=IntPtr.Zero;Console.WriteLine("BLOCK unexpected_metadata_CreateInstance");return unchecked((int)0x80004001);}));
   slots.Add(new Mops((IntPtr p,int a,out IntPtr r)=>F<Mops>(17)(backing,a,out r)));
   slots.Add(new OI((IntPtr p,out IntPtr r,out int n)=>{int h=F<OI>(18)(backing,out r,out n);if(h>=0)r=WrapOwned(r,"TL","TI"+id+".GetContainingTypeLib");return h;}));
   slots.Add(new Free((p,a)=>{IntPtr original;if(attrCopies.TryGetValue(a,out original)){attrCopies.Remove(a);Marshal.FreeHGlobal(a);a=original;}F<Free>(19)(backing,a);attrBuffers--;}));
   slots.Add(new Free((p,a)=>{F<Free>(20)(backing,a);funcBuffers--;}));
   slots.Add(new Free((p,a)=>{F<Free>(21)(backing,a);varBuffers--;}));
  }else{
   slots.Add(new R(p=>F<R>(3)(backing)));
   slots.Add(new UO((IntPtr p,uint n,out IntPtr r)=>{int h=F<UO>(4)(backing,n,out r);if(h>=0)r=WrapOwned(r,"TI","TL"+id+".GetTypeInfo");return h;}));
   slots.Add(new UI((IntPtr p,uint n,out int r)=>F<UI>(5)(backing,n,out r)));
   slots.Add(new GO((IntPtr p,ref Guid n,out IntPtr r)=>{int h=F<GO>(6)(backing,ref n,out r);if(h>=0)r=WrapOwned(r,"TI","TL"+id+".GetTypeInfoOfGuid");return h;}));
   slots.Add(new O((IntPtr p,out IntPtr r)=>{int h=F<O>(7)(backing,out r);if(h>=0)attrBuffers++;return h;}));
   slots.Add(new O((IntPtr p,out IntPtr r)=>F<O>(8)(backing,out r)));
   slots.Add(new Doc((p,a,b,c,d,e)=>F<Doc>(9)(backing,a,b,c,d,e)));
   slots.Add(new IsName((IntPtr p,IntPtr a,uint b,out int c)=>F<IsName>(10)(backing,a,b,out c)));
   slots.Add(new FindName((p,a,b,c,d,e)=>{Console.WriteLine("BLOCK unexpected_metadata_FindName");return unchecked((int)0x80004001);}));
   slots.Add(new Free((p,a)=>{F<Free>(12)(backing,a);attrBuffers--;}));
  }
  roots=slots.ToArray();IntPtr v=Marshal.AllocHGlobal(roots.Length*IntPtr.Size);for(int i=0;i<roots.Length;i++)Marshal.WriteIntPtr(v,i*IntPtr.Size,Marshal.GetFunctionPointerForDelegate(roots[i]));Pointer=Marshal.AllocHGlobal(IntPtr.Size);Marshal.WriteIntPtr(Pointer,v);
 }
 int Query(IntPtr p,ref Guid q,out IntPtr r){int n;queries.TryGetValue(q,out n);queries[q]=n+1;if(q==Unknown||q==iid){r=Pointer;Add(p);return 0;}r=IntPtr.Zero;return unchecked((int)0x80004002);}
 int Add(IntPtr p){adds++;return ++refs;} int Release(IntPtr p){releases++;return --refs;}
 public static void Report(string phase){Console.WriteLine("GC_COUNTS phase="+phase+" gen0="+GC.CollectionCount(0)+" gen1="+GC.CollectionCount(1)+" gen2="+GC.CollectionCount(2));foreach(var m in pool.Values){Console.WriteLine("META_REF phase="+phase+" id="+m.id+" kind="+m.kind+" refs="+m.refs+" delta="+(m.refs-1)+" adds="+m.adds+" releases="+m.releases+" attr_buffers="+m.attrBuffers+" func_buffers="+m.funcBuffers+" var_buffers="+m.varBuffers);if(0==0)Need(m.attrBuffers==0&&m.funcBuffers==0&&m.varBuffers==0,"descriptor_imbalance_"+m.id);}}
 public static void VerifyAfterGc(){foreach(var m in pool.Values)Need(m.refs==1&&m.adds==m.releases,"metadata_not_balanced_id_"+m.id);}
 public static void Contract(){foreach(var m in pool.Values){int before=m.refs;IntPtr a,b;Guid q=m.iid;Need(Marshal.QueryInterface(m.Pointer,ref q,out a)==0&&a==m.Pointer,"meta_qi");Need(Marshal.QueryInterface(a,ref Unknown,out b)==0&&b==m.Pointer,"meta_canonical");Marshal.Release(a);
   int get=m.kind=="TI"?3:7,free=m.kind=="TI"?19:12;IntPtr descriptor;
   Marshal.ThrowExceptionForHR(((O)Marshal.GetDelegateForFunctionPointer(Marshal.ReadIntPtr(Marshal.ReadIntPtr(b),get*IntPtr.Size),typeof(O)))(b,out descriptor));
   ((Free)Marshal.GetDelegateForFunctionPointer(Marshal.ReadIntPtr(Marshal.ReadIntPtr(b),free*IntPtr.Size),typeof(Free)))(b,descriptor);
   Marshal.Release(b);Need(m.refs==before,"meta_contract_balance");Console.WriteLine("META_ALIAS_PASS id="+m.id+" surviving_alias_usable_after_other_release");}}
 public static void Summary(){foreach(var m in pool.Values){foreach(var p in m.origins)Console.WriteLine("META_ORIGIN id="+m.id+" origin="+p.Key+" count="+p.Value);foreach(var p in m.queries)Console.WriteLine("META_QI id="+m.id+" iid="+p.Key+" count="+p.Value);}}
}
