// Managed count fixture with explicit fault boundaries; no Office access.
using System;
namespace AegisDesk.PptReader {
internal sealed class CountInterrupted:Exception {internal string Outcome;internal CountInterrupted(string value){Outcome=value;}}
internal sealed class CountCleanupFailed:Exception {internal CountCleanupFailed(Exception cause=null):base("count_cleanup",cause){}}
internal static class RawCount {
 internal static Action<object,string> Before;
 internal static int Read(object host,string name,Action check){
  check();if(Before!=null)Before(host,name);check();
  object value=host.GetType().GetProperty(name).GetValue(host,null);check();
  int count=(int)value.GetType().GetProperty("Count").GetValue(value,null);check();return count;
 }
}
}
