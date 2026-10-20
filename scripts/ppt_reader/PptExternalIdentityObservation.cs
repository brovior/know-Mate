#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using TYPEATTR=System.Runtime.InteropServices.ComTypes.TYPEATTR;
namespace AegisDesk.PptReader {
// Review-stage read-only probe. No bootstrap/guard integration or permission decision.
// Every result is primitive; native addresses live only in this synchronous owner scope.
internal sealed class IdentityObservation {
 internal sealed class Step {public string row,operation,outcome;public int hr,inner,wCode,argError;public bool sourcePresent,descriptionPresent;}
 internal sealed class Ownership {public int id;public string row,kind;public bool acquired=true,attempted,released,blocked;}
 internal sealed class Row {
  public string name,managedType,kind,typeGuid,typeName,status="not_run",reason;
  public bool? isComObject;public int group=-1,selfControl=-1,dispatchSupport=-1,applicationSupport=-1,major,minor;
 }
 internal sealed class Report {
  public int schema=2,nativeThread,apartment,qualifier,currentApartment,currentQualifier,generation,currentGeneration,acquired,releaseAttempts,released,dropped;
  public bool stopped,reentrant,cleanupFailed,usable;public string stopReason,outcome="unknown";
  public Step primary;public List<Step> cleanupErrors=new List<Step>();
  public List<Row> rows=new List<Row>();public List<Step> steps=new List<Step>();
  public List<Ownership> ownership=new List<Ownership>();
 }
 [ThreadStatic]static IdentityObservation active;
#if AEGIS_IDENTITY_TEST
 internal static Action<string,string> Boundary;
 internal static Action FailureNoted;
 internal static Action<string,Action> CleanupCall;
 internal static Action<Action> BlockedCleanup;
 internal static Func<int> ThreadOverride;
 internal static Func<int> ApartmentOverride;
 internal static int StepLimitOverride;
 internal static bool ForceSelfMismatch;
#endif
 static readonly Guid Unknown=new Guid("00000000-0000-0000-C000-000000000046"),Dispatch=new Guid("00020400-0000-0000-C000-000000000046"),Application=new Guid("91493442-5A91-11CF-8700-00AA0060263B");
 [DllImport("kernel32.dll")]static extern int GetCurrentThreadId();
 [DllImport("ole32.dll")]static extern int CoGetApartmentType(out int apartment,out int qualifier);
 [DllImport("oleaut32.dll")]static extern int VariantClear(IntPtr value);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)]delegate int Info(IntPtr self,uint index,uint lcid,out IntPtr value);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)]delegate int Attr(IntPtr self,out IntPtr value);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)]delegate void FreeAttr(IntPtr self,IntPtr value);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)]delegate int Doc(IntPtr self,int id,IntPtr name,IntPtr doc,IntPtr context,IntPtr help);
 [UnmanagedFunctionPointer(CallingConvention.StdCall)]delegate int GetNames(IntPtr self,ref Guid iid,IntPtr names,uint count,uint lcid,out int id);
 [StructLayout(LayoutKind.Sequential)]struct Parameters {public IntPtr args,named;public uint count,namedCount;}
 [StructLayout(LayoutKind.Sequential)]struct Error {public ushort code,reserved;public IntPtr source,description,helpFile;public uint helpContext;public IntPtr reservedPointer,deferredFill;public int scode;}
 [UnmanagedFunctionPointer(CallingConvention.StdCall)]delegate int Invoke(IntPtr self,int id,ref Guid iid,uint lcid,ushort flags,ref Parameters args,IntPtr result,ref Error error,out uint argError);
 sealed class Halt:Exception {}
 readonly Report report=new Report();readonly List<Action> cleanup=new List<Action>();readonly List<IntPtr> groups=new List<IntPtr>();
 Func<int> generation;int expectedThread;string currentRow="probe",currentOperation="entry";
 T Slot<T>(IntPtr p,int index) where T:class {var d=Marshal.GetDelegateForFunctionPointer(Marshal.ReadIntPtr(Marshal.ReadIntPtr(p),index*IntPtr.Size),typeof(T));if(d.GetType()!=typeof(T))d=Delegate.CreateDelegate(typeof(T),d.Target,d.Method);return d as T;}
 void Note(string row,string op,string outcome,int hr=0,int inner=0){int limit=256;
#if AEGIS_IDENTITY_TEST
  if(StepLimitOverride>0)limit=StepLimitOverride;
#endif
  if(report.steps.Count>=limit){report.dropped++;Fail(row,op,"trace_overflow");return;}report.steps.Add(new Step{row=row,operation=op,outcome=outcome,hr=hr,inner=inner});
  var trace=ExternalTrace.Current;if(trace!=null)trace.Record(ExternalTrace.Stage.IdentityObservation,outcome=="begin"?ExternalTrace.Result.Entered:outcome=="ok"?ExternalTrace.Result.Success:ExternalTrace.Result.Unknown,RowCode(row),OperationCode(op),hr);
 }
 static int RowCode(string row){return Array.IndexOf(new[]{"host","candidate","host.Application","candidate.Application","probe"},row);}
 static int OperationCode(string operation){unchecked{uint hash=2166136261;foreach(char value in operation)hash=(hash^value)*16777619;return (int)hash;}}
 static int CurrentThread(){
#if AEGIS_IDENTITY_TEST
  if(ThreadOverride!=null)return ThreadOverride();
#endif
  return GetCurrentThreadId();
 }
 static int Apartment(out int apartment,out int qualifier){int hr=CoGetApartmentType(out apartment,out qualifier);
#if AEGIS_IDENTITY_TEST
  if(ApartmentOverride!=null)apartment=ApartmentOverride();
#endif
  return hr;
 }
 void FailedBoundary(){
#if AEGIS_IDENTITY_TEST
  if(FailureNoted!=null)FailureNoted();
#endif
 }
 void Fail(string row,string op,string outcome,int hr=0,int inner=0){if(report.primary==null)report.primary=new Step{row=row,operation=op,outcome=outcome,hr=hr,inner=inner};report.stopped=true;report.stopReason=report.primary.outcome;foreach(Row r in report.rows)if(r.name==row){r.status=outcome;r.reason=op;}FailedBoundary();}
 void Check(){if(report.stopped||report.cleanupFailed)throw new Halt();report.currentGeneration=generation();if(CurrentThread()!=expectedThread||report.currentGeneration!=report.generation){Fail(currentRow,currentOperation,"owner_or_generation_changed");throw new Halt();}int hr=Apartment(out report.currentApartment,out report.currentQualifier);if(hr!=0||(report.currentApartment!=0&&report.currentApartment!=3)){Fail(currentRow,currentOperation,"apartment_changed",hr);throw new Halt();}}
 void BoundaryCheck(string row,string op){
#if AEGIS_IDENTITY_TEST
  if(Boundary!=null)Boundary(row,op);
#endif
  Check();
 }
 Ownership Resource(string row,string kind){var resource=new Ownership{id=report.ownership.Count+1,row=row,kind=kind};report.ownership.Add(resource);return resource;}
 bool DoCleanup(string row,string op,Action action,Ownership resource=null){resource=resource??Resource(row,op);if(resource.attempted){report.cleanupFailed=true;report.cleanupErrors.Add(new Step{row=row,operation=op,outcome="duplicate_attempt"});return false;}resource.attempted=true;Note(row,op,"begin");try{
#if AEGIS_IDENTITY_TEST
  if(CleanupCall!=null)CleanupCall(op,action);else action();
#else
  action();
#endif
  resource.released=true;Note(row,op,"ok");return true;
 }catch(Exception e){report.cleanupFailed=true;report.cleanupErrors.Add(new Step{row=row,operation=op,outcome="failed",hr=e.HResult});Note(row,op,"failed",e.HResult);FailedBoundary();return false;}}
 static bool Missing(int hr){return hr==unchecked((int)0x80004002)||hr==unchecked((int)0x80020003)||hr==unchecked((int)0x80020006);}
 static bool Fatal(int hr){switch(unchecked((uint)hr)){case 0x800706BA:case 0x800706BE:case 0x800706BF:case 0x80010006:case 0x80010007:case 0x80010012:case 0x80010108:case 0x800401FD:return true;}return false;}
 bool End(string row,string op,int hr,bool allowMissing=false,int inner=0){
  int effective=hr==unchecked((int)0x80020009)&&inner<0?inner:hr;
  if(hr<0){bool unavailable=allowMissing&&Missing(effective);string outcome=unavailable?"unavailable":Fatal(effective)?"fatal":"unknown";Note(row,op,outcome,hr,inner);foreach(Row item in report.rows)if(item.name==row){item.status=outcome;item.reason=op;}if(unavailable){BoundaryCheck(row,op+"_return");return false;}Fail(row,op,outcome,hr,inner);throw new Halt();}
  Note(row,op,"ok",hr,inner);BoundaryCheck(row,op+"_return");return true;
 }
 void Begin(string row,string op){currentRow=row;currentOperation=op;Check();Note(row,op,"begin");BoundaryCheck(row,op);}
 IntPtr Own(IntPtr p,string row,string label,Func<bool> canRelease=null){if(p==IntPtr.Zero)return p;report.acquired++;var resource=Resource(row,"release_"+label);cleanup.Add(()=>{if(canRelease!=null&&!canRelease()){resource.blocked=true;report.cleanupFailed=true;report.cleanupErrors.Add(new Step{row=row,operation="release_"+label,outcome="blocked_descriptor"});Note(row,"release_"+label,"blocked_descriptor");
#if AEGIS_IDENTITY_TEST
  if(BlockedCleanup!=null)BlockedCleanup(()=>Marshal.Release(p));
#endif
  return;}report.releaseAttempts++;if(DoCleanup(row,"release_"+label,()=>Marshal.Release(p),resource))report.released++;});return p;}
 void SelfControl(Row row,IntPtr actual,IntPtr expected){bool same=actual==expected;
#if AEGIS_IDENTITY_TEST
  if(ForceSelfMismatch)same=false;
#endif
  row.selfControl=same?1:0;if(!same){row.status="invalid";row.reason="self_control";Fail(row.name,"self_control","invalid");throw new Halt();}}
 IntPtr Query(IntPtr p,Guid iid,string row,string label,bool optional){Begin(row,label);if(p==IntPtr.Zero){End(row,label,unchecked((int)0x80004005));return IntPtr.Zero;}IntPtr q;int hr=Marshal.QueryInterface(p,ref iid,out q);Own(q,row,label);if(!End(row,label,hr,optional))return IntPtr.Zero;if(q==IntPtr.Zero)End(row,label+"_null_result",unchecked((int)0x80004005));return q;}
 int Group(IntPtr p){int n=groups.IndexOf(p);if(n<0){n=groups.Count;groups.Add(p);}return n;}
 void Metadata(IntPtr dispatch,Row row){
  bool descriptorReleased=true;
  Begin(row.name,"GetTypeInfo");IntPtr info;int hr=Slot<Info>(dispatch,4)(dispatch,0,0,out info);Own(info,row.name,"typeinfo",()=>descriptorReleased);if(!End(row.name,"GetTypeInfo",hr,true))return;
  if(info==IntPtr.Zero){End(row.name,"null_typeinfo",unchecked((int)0x80004005));return;}
  IntPtr attr=IntPtr.Zero;Begin(row.name,"GetTypeAttr");hr=Slot<Attr>(info,3)(info,out attr);
  if(attr!=IntPtr.Zero){descriptorReleased=false;IntPtr owned=attr;cleanup.Add(()=>{descriptorReleased=DoCleanup(row.name,"ReleaseTypeAttr",()=>Slot<FreeAttr>(info,19)(info,owned));});}
  End(row.name,"GetTypeAttr",hr);if(attr==IntPtr.Zero){End(row.name,"null_typeattr",unchecked((int)0x80004005));return;}
  var a=(TYPEATTR)Marshal.PtrToStructure(attr,typeof(TYPEATTR));row.typeGuid=a.guid.ToString();row.major=a.wMajorVerNum;row.minor=a.wMinorVerNum;
  IntPtr name=IntPtr.Zero,output=Marshal.AllocHGlobal(IntPtr.Size);Marshal.WriteIntPtr(output,IntPtr.Zero);
  try{Begin(row.name,"GetDocumentation_name");hr=Slot<Doc>(info,12)(info,-1,output,IntPtr.Zero,IntPtr.Zero,IntPtr.Zero);}
  finally{try{name=Marshal.ReadIntPtr(output);if(name!=IntPtr.Zero){IntPtr owned=name;cleanup.Add(()=>DoCleanup(row.name,"FreeBSTR_typeinfo_name",()=>Marshal.FreeBSTR(owned)));}}finally{DoCleanup(row.name,"FreeHGlobal_typeinfo_name",()=>Marshal.FreeHGlobal(output));}}
  if(End(row.name,"GetDocumentation_name",hr,true)&&name!=IntPtr.Zero){string n=Marshal.PtrToStringBSTR(name);row.typeName=n.Length<=128?n:n.Substring(0,128);}
 }
 IntPtr Describe(object value,Row row,bool metadata=true){
  row.kind="managed_argument";row.managedType=value==null?"null":value.GetType().FullName;
  if(value==null){row.status="unavailable";return IntPtr.Zero;}
  row.isComObject=Marshal.IsComObject(value);
  Begin(row.name,"GetIUnknownForObject");IntPtr u=Own(Marshal.GetIUnknownForObject(value),row.name,"managed_unknown");End(row.name,"GetIUnknownForObject",0);row.group=Group(u);
  Begin(row.name,"GetIDispatchForObject");IntPtr d=Own(Marshal.GetIDispatchForObject(value),row.name,"managed_dispatch");End(row.name,"GetIDispatchForObject",0);
  IntPtr self=Query(d,Unknown,row.name,"dispatch_to_unknown",false);SelfControl(row,self,u);
  DescribeNative(u,d,row,metadata);return d;
 }
 void DescribeNative(IntPtr u,IntPtr dispatch,Row row,bool metadata=true){
  row.group=Group(u);IntPtr d=Query(u,Dispatch,row.name,"QI_IDispatch",true);row.dispatchSupport=d==IntPtr.Zero?0:1;
  if(d!=IntPtr.Zero)SelfControl(row,Query(d,Unknown,row.name,"QI_dispatch_self_control",false),u);
  IntPtr app=Query(u,Application,row.name,"QI_Application",true);row.applicationSupport=app==IntPtr.Zero?0:1;
  if(app!=IntPtr.Zero)SelfControl(row,Query(app,Unknown,row.name,"QI_application_self_control",false),u);
  if(metadata&&dispatch!=IntPtr.Zero)Metadata(dispatch,row);Check();row.status="observed";
 }
 IntPtr Property(IntPtr dispatch,Row row){
  row.kind="native_property";if(dispatch==IntPtr.Zero){row.status="not_run";return IntPtr.Zero;}
  IntPtr name=IntPtr.Zero,names=IntPtr.Zero,value=IntPtr.Zero;bool initialized=false;var error=new Error();
  try{
   name=Marshal.StringToCoTaskMemUni("Application");names=Marshal.AllocHGlobal(IntPtr.Size);value=Marshal.AllocHGlobal(IntPtr.Size==8?24:16);
   for(int i=0;i<(IntPtr.Size==8?24:16);i++)Marshal.WriteByte(value,i,0);Marshal.WriteIntPtr(names,name);
   initialized=true;
   Guid empty=Guid.Empty;int id;Begin(row.name,"GetIDsOfNames_Application");int hr=Slot<GetNames>(dispatch,5)(dispatch,ref empty,names,1,0,out id);
   if(!End(row.name,"GetIDsOfNames_Application",hr,true)){row.status="unavailable";return IntPtr.Zero;}
   var args=new Parameters();uint argError;Begin(row.name,"Invoke_Application");hr=Slot<Invoke>(dispatch,6)(dispatch,id,ref empty,0,2,ref args,value,ref error,out argError);
   if(report.steps.Count<256)report.steps.Add(new Step{row=row.name,operation="Invoke_Application_diagnostics",outcome="numeric",hr=hr,inner=error.scode,wCode=error.code,argError=unchecked((int)argError),sourcePresent=error.source!=IntPtr.Zero,descriptionPresent=error.description!=IntPtr.Zero});else report.dropped++;
   var telemetry=ExternalTrace.Current;if(telemetry!=null){telemetry.Record(ExternalTrace.Stage.ErrorDetail,hr<0?ExternalTrace.Result.Failed:ExternalTrace.Result.Success,hr,error.scode,error.code);telemetry.Record(ExternalTrace.Stage.ErrorDetail,ExternalTrace.Result.Success,unchecked((int)argError),error.source!=IntPtr.Zero?1:0,error.description!=IntPtr.Zero?1:0);}
   if(error.deferredFill!=IntPtr.Zero){Fail(row.name,"deferred_error_not_invoked","unknown",hr,error.scode);throw new Halt();}
   if(!End(row.name,"Invoke_Application",hr,true,error.scode)){row.status="unavailable";return IntPtr.Zero;}
   int vt=(ushort)Marshal.ReadInt16(value);Note(row.name,"returned_variant","ok",vt);
   if(vt!=9&&vt!=13){End(row.name,"unexpected_variant",unchecked((int)0x80004005));return IntPtr.Zero;}
   IntPtr borrowed=Marshal.ReadIntPtr(value,8);if(borrowed==IntPtr.Zero){End(row.name,"null_variant_interface",unchecked((int)0x80004005));return IntPtr.Zero;}
   IntPtr u=Query(borrowed,Unknown,row.name,"property_to_unknown",false),d=Query(u,Dispatch,row.name,"property_to_dispatch",true);
   if(d!=IntPtr.Zero)SelfControl(row,Query(d,Unknown,row.name,"property_self_control",false),u);DescribeNative(u,d,row);return d;
  }finally{
   if(initialized)DoCleanup(row.name,"VariantClear",()=>Marshal.ThrowExceptionForHR(VariantClear(value)));
   if(value!=IntPtr.Zero)DoCleanup(row.name,"FreeHGlobal_variant",()=>Marshal.FreeHGlobal(value));
   if(names!=IntPtr.Zero)DoCleanup(row.name,"FreeHGlobal_names",()=>Marshal.FreeHGlobal(names));
   if(name!=IntPtr.Zero)DoCleanup(row.name,"FreeCoTaskMem_name",()=>Marshal.FreeCoTaskMem(name));
   foreach(IntPtr p in new[]{error.source,error.description,error.helpFile})if(p!=IntPtr.Zero){IntPtr owned=p;DoCleanup(row.name,"FreeBSTR_exception",()=>Marshal.FreeBSTR(owned));}
  }
 }
 void Finish(){
  bool complete=report.rows.Count==4;
  foreach(Row row in report.rows){if(row.status=="not_run")row.reason=report.stopReason??(report.cleanupFailed?"cleanup_failed":"dependency_unavailable");if(row.status=="unavailable"&&row.reason==null)row.reason="interface_unavailable";complete&=row.status=="observed"||row.status=="unavailable";}
  report.usable=complete&&!report.stopped&&!report.cleanupFailed&&report.dropped==0&&report.acquired==report.released&&report.rows[0].status=="observed"&&report.rows[1].status=="observed";
  report.outcome=report.usable?"complete":"unknown";
  if(!report.usable){report.stopped=true;if(report.stopReason==null)report.stopReason=report.cleanupFailed?"cleanup_failed":"incomplete";}
 }
 internal static Report Capture(object host,object candidate,int expectedOwner,int capturedGeneration,Func<int> currentGeneration){
  var probe=new IdentityObservation();var r=probe.report;r.nativeThread=CurrentThread();r.generation=capturedGeneration;
  foreach(string name in new[]{"host","candidate","host.Application","candidate.Application"})r.rows.Add(new Row{name=name});
  if(active!=null){r.reentrant=true;probe.Fail("probe","entry","reentrant");active.report.reentrant=true;active.Fail(active.currentRow,active.currentOperation,"reentrant");probe.Finish();return r;}
  probe.expectedThread=expectedOwner;probe.generation=currentGeneration;active=probe;
  try{int hr=Apartment(out r.apartment,out r.qualifier);if(hr!=0||r.nativeThread!=expectedOwner||(r.apartment!=3&&r.apartment!=0)){probe.Fail("probe","entry","owner_apartment",hr);throw new Halt();}
   probe.Check();if(host==null||candidate==null){probe.Fail(host==null?"host":"candidate","null_argument","invalid");throw new Halt();}IntPtr h=probe.Describe(host,r.rows[0],false),c=probe.Describe(candidate,r.rows[1],false);probe.Metadata(h,r.rows[0]);probe.Check();probe.Metadata(c,r.rows[1]);probe.Check();probe.Property(h,r.rows[2]);probe.Check();probe.Property(c,r.rows[3]);
  }catch(Halt){}catch(Exception e){probe.Fail(probe.currentRow,probe.currentOperation,Fatal(e.HResult)?"fatal":"unknown",e.HResult);foreach(Row row in r.rows)if(row.name==probe.currentRow)row.status=r.stopReason;probe.Note(probe.currentRow,probe.currentOperation,r.stopReason,e.HResult);}
  finally{for(int i=probe.cleanup.Count-1;i>=0;i--)probe.DoCleanupEntry(probe.cleanup[i]);probe.cleanup.Clear();probe.groups.Clear();active=null;probe.Finish();}
  return r;
 }
 void DoCleanupEntry(Action action){try{action();}catch(Exception e){report.cleanupFailed=true;report.cleanupErrors.Add(new Step{row="probe",operation="cleanup_dispatch",outcome="failed",hr=e.HResult});Note("probe","cleanup_dispatch","failed",e.HResult);}}
}
}
#endif
