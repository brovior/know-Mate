using AegisDesk.PptReader;
using System;
using System.Collections.Generic;
using System.Reflection;
using System.Reflection.Emit;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Threading;
using TYPEATTR=System.Runtime.InteropServices.ComTypes.TYPEATTR;
using FUNCDESC=System.Runtime.InteropServices.ComTypes.FUNCDESC;
// Native-layout IDispatch spy, no Office activation. Allocations/delegates live until this isolated process exits.
class Spy {
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int Q(IntPtr p,ref Guid id,out IntPtr r);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int R(IntPtr p);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int C(IntPtr p,out uint n);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int T(IntPtr p,uint n,uint l,out IntPtr r);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int N(IntPtr p,IntPtr iid,IntPtr names,uint n,uint l,IntPtr ids);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int I(IntPtr p,int id,IntPtr iid,uint l,ushort flags,IntPtr pars,IntPtr result,IntPtr ex,IntPtr err);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int FindPoint(IntPtr p,ref Guid iid,out IntPtr r);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int EnumPoint(IntPtr p,out IntPtr r);
 public NativePoint CP; public IntPtr container; Delegate[] containerRoots;
 public void VerifyContract(){
  int baseline=refs;Guid cpc=new Guid("B196B284-BAB4-101A-B69C-00AA00341D07"),unk=new Guid("00000000-0000-0000-C000-000000000046"),evt=new Guid("914934C2-5A91-11CF-8700-00AA0060263B");IntPtr c,u,point;
  Probe.Check(Marshal.QueryInterface(Pointer,ref cpc,out c)==0&&c==container&&refs==baseline+1,"cpc_qi_addref");
  Probe.Check(Marshal.QueryInterface(c,ref unk,out u)==0&&u==Pointer&&refs==baseline+2,"cpc_canonical_unknown");Marshal.Release(u);Marshal.Release(c);Probe.Check(refs==baseline,"cpc_qi_balance");
  int before=CP.Refs;Probe.Check(Find(container,ref evt,out point)==0&&point==CP.Pointer&&CP.Refs==before+1,"find_owned_reference");Marshal.Release(point);Probe.Check(CP.Refs==before,"find_release_balance");CP.VerifyContract();Console.WriteLine("CONTRACT PASS CPC_identity CP_identity QI_AddRef Find_AddRef balanced_release");
 }
 public ITypeInfo Info; public Guid HostIid; public IntPtr Pointer; Delegate[] roots; public int refs=1; public int Adds,Releases; public bool Collection; public Dictionary<string,Spy> Children=new Dictionary<string,Spy>();
 public Dictionary<int,string> Names=new Dictionary<int,string>(); public int Quits,AfterQuit,CountValue; public Action OnQuit; public Action<string> OnInvoke;public int FindError,CpcError;public bool FindPartial;
 public Spy(ITypeInfo info,bool collection=false) { Collection=collection;
  Info=info;IntPtr a;info.GetTypeAttr(out a);HostIid=((TYPEATTR)Marshal.PtrToStructure(a,typeof(TYPEATTR))).guid;info.ReleaseTypeAttr(a);
  foreach(string name in (collection ? new[]{"Count"} : new[]{"Quit","CommandBars","DisplayAlerts","Presentations","ProtectedViewWindows","SlideShowWindows"})) {int[] ids=new int[1];info.GetIDsOfNames(new[]{name},1,ids);Names.Add(ids[0],name);}
  roots=new Delegate[]{new Q(Query),new R(Add),new R(Release),new C(Count),new T(TypeInfo),Probe.Adapter(this,"GetNames","GetNames"),Probe.Adapter(this,"Invoke","Invoke")};
  var table=Marshal.AllocHGlobal(IntPtr.Size*7);for(int j=0;j<7;j++)Marshal.WriteIntPtr(table,j*IntPtr.Size,Marshal.GetFunctionPointerForDelegate(roots[j]));Pointer=Marshal.AllocHGlobal(IntPtr.Size);Marshal.WriteIntPtr(Pointer,table);
  containerRoots=new Delegate[]{new Q(Query),new R(Add),new R(Release),new EnumPoint(EnumPoints),new PptConnectionLease.FindProc(Find)};
  var ct=Marshal.AllocHGlobal(IntPtr.Size*5);for(int j=0;j<5;j++)Marshal.WriteIntPtr(ct,j*IntPtr.Size,Marshal.GetFunctionPointerForDelegate(containerRoots[j]));container=Marshal.AllocHGlobal(IntPtr.Size);Marshal.WriteIntPtr(container,ct);

 }
 int Add(IntPtr p){Interlocked.Increment(ref Adds);return Interlocked.Increment(ref refs);} int Release(IntPtr p){Interlocked.Increment(ref Releases);return Interlocked.Decrement(ref refs);} public void Report(string phase){Console.WriteLine("REF "+phase+" ptr="+Pointer+" refs="+refs+" adds="+Adds+" releases="+Releases);} int Query(IntPtr p,ref Guid id,out IntPtr r){Console.WriteLine("HOST_QI iid="+id);if(id==new Guid("B196B284-BAB4-101A-B69C-00AA00341D07")){if(CpcError!=0){r=IntPtr.Zero;return CpcError;}r=container;Add(p);return 0;}if(id==HostIid||id==new Guid("00000000-0000-0000-C000-000000000046")||id==new Guid("00020400-0000-0000-C000-000000000046")){r=Pointer;Add(p);return 0;}r=IntPtr.Zero;return unchecked((int)0x80004002);}
 int EnumPoints(IntPtr p,out IntPtr r){r=IntPtr.Zero;return unchecked((int)0x80004001);}
 int Find(IntPtr p,ref Guid iid,out IntPtr r){if(FindError!=0){r=IntPtr.Zero;if(FindPartial){r=CP.Pointer;CP.Add(r);}return FindError;}if(CP==null||iid!=new Guid("914934C2-5A91-11CF-8700-00AA0060263B")){r=IntPtr.Zero;return unchecked((int)0x80040200);}CP.Add(CP.Pointer);r=CP.Pointer;Console.WriteLine("REAL_FIND_CONNECTION_POINT");return 0;}
 int Count(IntPtr p,out uint n){n=1;return 0;}
 int TypeInfo(IntPtr p,uint n,uint l,out IntPtr r){r=Meta.FromInfo(Info);return 0;}
 int GetNames(IntPtr p,IntPtr iid,IntPtr names,uint n,uint l,IntPtr ids){try{var s=Marshal.PtrToStringUni(Marshal.ReadIntPtr(names));int[] found=new int[1];Info.GetIDsOfNames(new[]{s},1,found);Marshal.WriteInt32(ids,found[0]);Console.WriteLine("GETID name="+s+" id="+found[0]);return 0;}catch{return unchecked((int)0x80020006);}}
 int Invoke(IntPtr p,int id,IntPtr iid,uint l,ushort flags,IntPtr pars,IntPtr result,IntPtr ex,IntPtr err){try {
  string name;if(!Names.TryGetValue(id,out name))return unchecked((int)0x80020003);if(OnInvoke!=null)OnInvoke(name);
  if(Collection){Marshal.GetNativeVariantForObject(CountValue,result);return 0;} if(Quits>0)AfterQuit++;
  int argc=Marshal.ReadInt32(pars,IntPtr.Size*2),named=Marshal.ReadInt32(pars,IntPtr.Size*2+4);
  if(name=="Quit") {Console.WriteLine("QUIT_INVOKE id="+id+" flags="+flags+" argc="+argc+" named="+named);Probe.Check((flags&1)!=0&&(flags&~3)==0&&argc==0&&named==0,"quit_dispatch_contract");Quits++;if(OnQuit!=null)OnQuit();return 0;}
  if(name=="CommandBars"){Marshal.GetNativeVariantForObject(new OwnedExitCommands(()=>{Quits++;if(OnQuit!=null)OnQuit();}),result);return 0;}
  if(name=="DisplayAlerts"){if((flags&4)==0)Marshal.GetNativeVariantForObject(2,result);return 0;}
  var child=Children[name]; child.Add(child.Pointer); Marshal.WriteInt16(result,9); Marshal.WriteIntPtr(result,8,child.Pointer); return 0;
 }catch(Exception e){Console.WriteLine("SPY_ERROR "+e.GetType().Name+" "+e.Message);return e is COMException?e.HResult:unchecked((int)0x80004005);}}
}
class NativePoint {
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int Q(IntPtr p,ref Guid id,out IntPtr r);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int R(IntPtr p);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int G(IntPtr p,out Guid id);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int O(IntPtr p,out IntPtr r);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int A(IntPtr p,IntPtr sink,out int cookie);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int U(IntPtr p,int cookie);
 public IntPtr Pointer,Owner;IntPtr held;Delegate[] roots;public int Refs=1,Adds,Releases,Unadvised;public int AdviseError,UnadviseError,AdviseCalls;public bool ZeroCookie;public bool Partial;public Action OnAdvise;public void FixtureDropConnection(){if(held!=IntPtr.Zero){Marshal.Release(held);held=IntPtr.Zero;}}
 public void VerifyContract(){int baseline=Refs;Guid unk=new Guid("00000000-0000-0000-C000-000000000046"),cp=new Guid("B196B286-BAB4-101A-B69C-00AA00341D07"),bad=Guid.NewGuid();IntPtr a,b,c;Probe.Check(Marshal.QueryInterface(Pointer,ref cp,out a)==0&&a==Pointer&&Refs==baseline+1,"cp_qi_addref");Probe.Check(Marshal.QueryInterface(a,ref unk,out b)==0&&b==Pointer&&Refs==baseline+2,"cp_canonical_unknown");Probe.Check(Marshal.QueryInterface(a,ref bad,out c)==unchecked((int)0x80004002)&&c==IntPtr.Zero&&Refs==baseline+2,"cp_nointerface_no_addref");Marshal.Release(b);Marshal.Release(a);Probe.Check(Refs==baseline,"cp_qi_balanced");}
 public NativePoint(){roots=new Delegate[]{new Q(Query),new R(Add),new R(Release),new G(Interface),new O(Container),new PptConnectionLease.AdviseProc(Advise),new PptConnectionLease.UnadviseProc(Unadvise),new O(EnumConnections)};var v=Marshal.AllocHGlobal(IntPtr.Size*8);for(int i=0;i<8;i++)Marshal.WriteIntPtr(v,i*IntPtr.Size,Marshal.GetFunctionPointerForDelegate(roots[i]));Pointer=Marshal.AllocHGlobal(IntPtr.Size);Marshal.WriteIntPtr(Pointer,v);}
 int Query(IntPtr p,ref Guid id,out IntPtr r){if(id==new Guid("00000000-0000-0000-C000-000000000046")||id==new Guid("B196B286-BAB4-101A-B69C-00AA00341D07")){r=Pointer;Add(p);return 0;}r=IntPtr.Zero;return unchecked((int)0x80004002);}
 public int Add(IntPtr p){Interlocked.Increment(ref Adds);return Interlocked.Increment(ref Refs);}int Release(IntPtr p){Interlocked.Increment(ref Releases);return Interlocked.Decrement(ref Refs);}
 int Interface(IntPtr p,out Guid id){id=new Guid("914934C2-5A91-11CF-8700-00AA0060263B");return 0;}
 int Container(IntPtr p,out IntPtr r){r=Owner;if(r==IntPtr.Zero)return unchecked((int)0x80004001);Marshal.AddRef(r);return 0;}
 int EnumConnections(IntPtr p,out IntPtr r){r=IntPtr.Zero;return unchecked((int)0x80004001);}
 int Advise(IntPtr p,IntPtr sink,out int cookie){AdviseCalls++;cookie=0;if(OnAdvise!=null)OnAdvise();if(AdviseError==0||Partial){held=sink;Marshal.AddRef(held);cookie=1;}Probe.Snapshot("AFTER_ADVISE");if(ZeroCookie)cookie=0;return AdviseError;}
 int Unadvise(IntPtr p,int cookie){Unadvised++;if(UnadviseError!=0)return UnadviseError;Marshal.Release(held);held=IntPtr.Zero;Probe.Snapshot("AFTER_UNADVISE");return 0;}
 public void Report(string phase){Console.WriteLine("CP_REF "+phase+" refs="+Refs+" adds="+Adds+" releases="+Releases+" unadvise="+Unadvised+" sink_held="+(held!=IntPtr.Zero));}
}
partial class Probe {
 static Spy current;
 public static void Snapshot(string phase){current.Report("HOST_"+phase);current.CP.Report(phase);Meta.Report(phase);}
 static Assembly Core; static List<Spy> roots=new List<Spy>();
 public static Delegate Adapter(Spy target,string method,string delegateName){
  Type kind=Core.GetType("AegisDesk.PptReader.RawCount+"+delegateName,true);var parameters=kind.GetMethod("Invoke").GetParameters();var types=new Type[parameters.Length+1];types[0]=typeof(Spy);for(int i=0;i<parameters.Length;i++)types[i+1]=parameters[i].ParameterType;
  var wrapper=new DynamicMethod("native_abi_adapter_"+method,typeof(int),types,typeof(Probe),true);var il=wrapper.GetILGenerator();il.Emit(OpCodes.Ldarg_0);
  for(int i=0;i<parameters.Length;i++){il.Emit(OpCodes.Ldarg,i+1);if(parameters[i].ParameterType.IsByRef)il.Emit(OpCodes.Conv_I);}
  il.Emit(OpCodes.Call,typeof(Spy).GetMethod(method,BindingFlags.Instance|BindingFlags.NonPublic));il.Emit(OpCodes.Ret);return wrapper.CreateDelegate(kind,target);
 }
 [DllImport("oleaut32.dll",PreserveSig=false)] static extern void LoadRegTypeLib(ref Guid id,ushort major,ushort minor,int lcid,out ITypeLib lib);
 public static void Check(bool yes,string code){if(!yes)throw new Exception(code);}
 static ITypeInfo Find(ITypeLib lib,string expected){for(int i=0;i<lib.GetTypeInfoCount();i++){string n,d,h;int c;lib.GetDocumentation(i,out n,out d,out c,out h);if(n==expected){ITypeInfo t;lib.GetTypeInfo(i,out t);return t;}}throw new Exception(expected);}
}

[ComVisible(true),ClassInterface(ClassInterfaceType.AutoDispatch)]
public sealed class OwnedExitCommands {readonly Action exit;public OwnedExitCommands(Action action){exit=action;}public void ExecuteMso(string command){if(command!="FileExit")throw new Exception("wrong_ui_command");exit();}}
