using System;
using System.Collections.Generic;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Threading;
using AegisDesk.PptReader;
public class Counts { public int Count {get;set;} }
[ComVisible(true),Guid("72501499-A385-4241-98C8-587E9A95A2D0"),InterfaceType(ComInterfaceType.InterfaceIsIDispatch)]
public interface IFixtureHost {[DispId(1)]void Quit();}
[ComVisible(true),ClassInterface(ClassInterfaceType.None),ComDefaultInterface(typeof(IFixtureHost))]
public class Host:StandardOleMarshalObject,IFixtureHost {
 public List<string> Calls=new List<string>(); public Action OnQuit;
 public int Window;public bool FailWindow;public int HWND {get {if(FailWindow)throw new InvalidOperationException("hwnd_unavailable");return Window;}}
 public int DisplayAlerts { get {Calls.Add("alerts_get");return 2;} set {Calls.Add("alerts_set");} }
 public Counts Docs=new Counts();
 public Counts Presentations {get {Calls.Add("presentations");return Docs;}}
 public Counts ProtectedViewWindows {get {Calls.Add("protectedviews");return new Counts();}}
 public Counts SlideShowWindows {get {Calls.Add("slideshows");return new Counts();}}
 public void Quit(){Calls.Add("quit");if(OnQuit!=null)OnQuit();}
}
public class Addin { public object Object {get;set;} }
public class Point:IConnectionPoint {
 public List<string> Calls; public Action OnUnadvise;
 public void Advise(object value,out int cookie){Calls.Add("advise");cookie=1;}
 public void Unadvise(int cookie){Calls.Add("unadvise");if(OnUnadvise!=null)OnUnadvise();}
 public void GetConnectionInterface(out Guid id){id=Guid.Empty;}
 public void GetConnectionPointContainer(out IConnectionPointContainer p){p=null;}
 public void EnumConnections(out IEnumConnections p){p=null;}
}
namespace AegisDesk.PptReader {
 internal static class ShutdownBuildPolicy { public const bool Enabled=true; }
 internal static class PptEventContract { internal static IConnectionPoint Resolve(object host){throw new Exception("unexpected_resolver");} }
 internal static class PptSinkLoader { internal static object Create(PptShutdownState state){throw new Exception("unexpected_loader");} }
}
namespace AegisDesk.PptShared {internal static class PptExtractionCore {
 public static Action OnExtract; public static string ExtractSlide(object a,string p,int s){if(OnExtract!=null)OnExtract();return "";}
 public static void ReadShape(object a,List<Dictionary<string,object>> b,Dictionary<string,int> c){}
 public static void ReadShapeTracked(object a,List<Dictionary<string,object>> b,Dictionary<string,int> c,List<Dictionary<string,object>> d,string e){}
}}
