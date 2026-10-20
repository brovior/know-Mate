using System;
using System.Collections.Generic;
using AegisDesk.PptReader;
using AegisDesk.PptEvents;

// Executes the real sink bodies with local ABI type stubs; never activates Office.
internal static class CaptureEventHarness {
 delegate void CancelEvent(ref bool value);
 static void Require(bool value){if(!value)throw new Exception("event_evidence");}
 static void Passive(PptShutdownState state,CancelEvent call,int kind){
  foreach(bool initial in new[]{false,true}){
   state.Events.Clear();bool cancel=initial;call(ref cancel);Require(cancel==initial&&state.Events.Count==2);
   for(int i=0;i<2;i++){var row=state.Events[i];Require(row.Kind==kind&&row.Enter==(i==0)&&row.Incoming==initial&&row.Outgoing==initial&&!row.Set);}
  }
 }
 static int Main(){try{
  var state=new PptShutdownState();var sink=new PptEventSink(state);
  Passive(state,delegate(ref bool cancel){sink.WindowBeforeRightClick(null,ref cancel);},102);
  Passive(state,delegate(ref bool cancel){sink.WindowBeforeDoubleClick(null,ref cancel);},103);
  Passive(state,delegate(ref bool cancel){sink.PresentationBeforeSave(null,ref cancel);},118);
  Passive(state,delegate(ref bool cancel){sink.ProtectedViewWindowBeforeEdit(null,ref cancel);},126);
  foreach(bool active in new[]{false,true})foreach(bool initial in new[]{false,true}){
   state.Active=active;state.Events.Clear();bool cancel=initial;sink.PresentationBeforeClose(null,ref cancel);
   Require(cancel==(active||initial)&&state.Events.Count==2);Require(state.Events[0].Incoming==initial&&state.Events[1].Incoming==initial&&state.Events[1].Outgoing==cancel&&state.Events[1].Set==active);
   state.Events.Clear();cancel=initial;sink.ProtectedViewWindowBeforeClose(null,0,ref cancel);
   Require(cancel==(active||initial)&&state.Events.Count==2&&state.Events[1].Set==active);
  }
  Console.WriteLine("{\"result\":\"PASS\",\"office_started\":false}");return 0;
 }catch{Console.WriteLine("{\"result\":\"FAIL\"}");return 1;}}
}
namespace AegisDesk.PptReader {
 public sealed class PptShutdownState {
  internal sealed class Entry {internal int Kind;internal bool Enter,Incoming,Outgoing,Set;}
  internal readonly List<Entry> Events=new List<Entry>();public bool Active,Cancelled;
  public void EventBoundary(int kind,bool enter,bool incoming,bool outgoing,bool set){Events.Add(new Entry{Kind=kind,Enter=enter,Incoming=incoming,Outgoing=outgoing,Set=set});}
  public void RecordCancellation(int kind){}public void MarkBroken(){}
 }
}
namespace Microsoft.Office.Core {public enum MsoSyncEventType {Unused}}
namespace Microsoft.Office.Interop.PowerPoint {
 public interface EApplication {} public sealed class Selection {}public sealed class Presentation {}
 public sealed class Slide {}public sealed class SlideShowWindow {}public sealed class DocumentWindow {}
 public sealed class SlideRange {}public sealed class Effect {}public sealed class ProtectedViewWindow {}
 public sealed class Shape {}public enum PpProtectedViewCloseReason {Unused}
}
