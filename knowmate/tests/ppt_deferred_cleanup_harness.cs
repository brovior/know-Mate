using System;
using System.Threading;
using AegisDesk.PptReader;
class GateHarness {
 static void Check(bool ok,string name){if(!ok)throw new Exception(name);}
 [STAThread]static int Main(){try{
  int tests=0;
  foreach(string mode in new[]{"normal","log_throw","log_reentry","timer_throw","first_close_throw","second_close_throw","cleanup_reentry","consumed_cancel","guard_throw"}) {
   bool signal=false;int invokes=0,stops=0,first=0,second=0;PptDeferredGate gate=null;PptDeferredResources resources=null;
   resources=new PptDeferredResources(new IntPtr(11),new IntPtr(22),delegate{stops++;if(mode=="timer_throw")throw new Exception();},
    delegate(IntPtr p){if(p==new IntPtr(11))first++;else second++;if(mode=="cleanup_reentry"){resources.Dispose();gate.Cancel("cleanup");}
     if(mode=="first_close_throw"&&p==new IntPtr(11)||mode=="second_close_throw"&&p==new IntPtr(22))throw new Exception();},delegate{throw new Exception("cleanup_log");});
   gate=new PptDeferredGate(()=>true,()=>signal,()=>true,()=>true,
    delegate{invokes++;if(mode=="consumed_cancel"){gate.Cancel("disconnect");Check(gate.State==PptDeferredGate.Phase.Consumed,"consumed_state");Check(!resources.Empty,"cleanup_not_early");}
     if(mode=="guard_throw")throw new Exception();return "quit_requested";},resources.Dispose,
    delegate(string message){if(mode=="log_throw")throw new Exception();if(mode=="log_reentry"&&message=="callback_entry")gate.Cancel("logging");});
   gate.Enter(true);Check(gate.Arm()=="armed","arm");signal=true;gate.Tick();Check(invokes==0,"nested_block");gate.Leave();
   gate.Tick();gate.Tick();gate.Cancel("after");resources.Dispose();
   Check(invokes==1&&stops==1&&first==1&&second==1&&resources.Empty,"exact_cleanup");
   Check(gate.Outcome==(mode=="guard_throw"?"unknown":"quit_requested"),"outcome_preserved");
   Console.WriteLine("PASS "+mode);tests++;
  }
  Console.WriteLine("GATE_RESULT cases="+tests);return 0;
 }catch(Exception e){Console.WriteLine(e);return 1;}}
}
