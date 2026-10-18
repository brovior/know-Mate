using System;
using System.Threading;
using AegisDesk.PptReader;
class ExplicitDirectoryProbe {
 static int Main(string[] args) {
  var trace=new PptLifecycleTrace();
  trace.BindOwner(Thread.CurrentThread.ManagedThreadId);
  // Mutate only this disposable child: the captured directory and flag must survive.
  Environment.SetEnvironmentVariable("AEGIS_PPT_LIFECYCLE_TRACE",null);
  Environment.SetEnvironmentVariable("AEGIS_PPT_LIFECYCLE_DIR",null);
  int token=trace.Begin();trace.Record(10,8,0);trace.End(token);
  trace.FlushNormalReturn(trace.Complete(token));
  var off=new PptLifecycleTrace();off.BindOwner(Thread.CurrentThread.ManagedThreadId);
  if(off.Begin()!=0)return 2;
  Console.WriteLine("token="+token);
  return 0;
 }
}
