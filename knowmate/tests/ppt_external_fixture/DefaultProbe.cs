using System;
using System.Reflection;
using System.Threading;
using AegisDesk.PptReader;
static class DefaultProbe {
 [STAThread]static int Main(string[] args){
  var assembly=typeof(Connect).Assembly;
  if(assembly.GetType("AegisDesk.PptReader.IPptExternalBootstrap")!=null||assembly.GetType("BootstrapCleanupFaults")!=null||typeof(Connect).GetMethod("BindExternalBootstrap")!=null)throw new Exception("default_diagnostic_leak");
  if(typeof(IPptReader).GetMethods().Length!=3||typeof(IPptReaderLifecycle).GetMethods().Length!=4)throw new Exception("dual_shape");
  var c=new Connect();Array custom=null;c.OnConnection(new Host(),1,new Addin(),ref custom);
  if(c.ProtocolVersion()!=3||c.ExtractSlide("synthetic",1)!="")throw new Exception("ordinary_reader");
  c.OnDisconnection(1,ref custom);c.OnConnection(new Host(),1,new Addin(),ref custom);
  if(c.ProtocolVersion()!=3)throw new Exception("reconnect");c.OnDisconnection(1,ref custom);
  Console.WriteLine("DEFAULT_OFF PASS");return 0;
 }
}
