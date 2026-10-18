using System;
using System.IO;
using System.Reflection;
using System.Security.Cryptography;
using System.Threading;
class BoundaryHarness {
 static BindingFlags F=BindingFlags.Static|BindingFlags.NonPublic;
 static string Hash(string p){using(var s=SHA256.Create())return BitConverter.ToString(s.ComputeHash(File.ReadAllBytes(p))).Replace("-","").ToLowerInvariant();}
 static Exception Root(Exception e){while(e.InnerException!=null)e=e.InnerException;return e;}
 static void Need(bool b,string id){if(!b)throw new Exception(id);Console.WriteLine("PASS "+id);}
 static int Main(string[] args){int result=1;Thread t=new Thread(()=>{try{Run(args);result=0;}catch(Exception e){Console.WriteLine("FAIL "+Root(e).GetType().Name+" "+Root(e).Message);}});t.SetApartmentState(args[0]=="STA"?ApartmentState.STA:ApartmentState.MTA);t.Start();t.Join();return result;}
 static void Run(string[] args){
  string path=Path.GetFullPath(args[1]),other=Path.GetFullPath(args[2]);
  Assembly a=Assembly.LoadFrom(path);Type identity=a.GetType("AegisDesk.PptReader.PptPackageIdentity",true);
  MethodInfo disk=identity.GetMethod("DiskMvid",F),require=identity.GetMethod("RequireLoaded",F);
  Need(Path.GetDirectoryName(path)!=Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location),"external_exe_layout");
  Guid mvid=(Guid)disk.Invoke(null,new object[]{path,Hash(path)});
  Need(mvid==a.ManifestModule.ModuleVersionId,"mvid_success_"+args[0]);
  try{disk.Invoke(null,new object[]{path,new string('0',64)});throw new Exception("wrong_hash_accepted");}catch(TargetInvocationException e){Need(Root(e).Message=="ppt_event_payload_changed","wrong_hash_rejected");}
  string invalid=Path.Combine(Path.GetDirectoryName(path),"invalid.bin");File.WriteAllBytes(invalid,new byte[]{0,1,2,3});
  try{disk.Invoke(null,new object[]{invalid,Hash(invalid)});throw new Exception("bad_metadata_accepted");}catch(TargetInvocationException e){Need(Root(e) is BadImageFormatException,"bad_metadata_rejected");}
  Need(AssemblyName.GetAssemblyName(other).FullName==a.FullName,"same_identity_fixture");
  Guid different=(Guid)disk.Invoke(null,new object[]{other,Hash(other)});
  Need(different!=mvid,"disk_mvid_not_unified_with_loaded");
  try{require.Invoke(null,new object[]{a,different});throw new Exception("mismatch_accepted");}catch(TargetInvocationException e){Need(Root(e).Message=="ppt_event_loaded_mvid_mismatch","loaded_mismatch_rejected");}
  require.Invoke(null,new object[]{a,mvid});Console.WriteLine("PASS loaded_match");
  foreach(Assembly loaded in AppDomain.CurrentDomain.GetAssemblies())Need(!String.Equals(loaded.Location,other,StringComparison.OrdinalIgnoreCase),"other_not_execution_loaded");
 }
}
