// Managed-host unit fixture only. Native raw-count coverage lives in test_ppt_raw_count.py.
using System;
namespace AegisDesk.PptReader {
internal sealed class CountInterrupted:Exception {internal string Outcome;internal CountInterrupted(string value){Outcome=value;}}
internal sealed class CountCleanupFailed:Exception {}
internal static class RawCount {
 internal static int Read(object host,string name,Action check){
  check();object value=host.GetType().GetProperty(name).GetValue(host,null);check();
  int count=(int)value.GetType().GetProperty("Count").GetValue(value,null);check();return count;
 }
}
}
