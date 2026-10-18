#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
using System;
using System.Runtime.InteropServices;
using System.Web.Script.Serialization;
namespace AegisDesk.PptReader {
// Process environment survives Connect instances and AppDomains. Never clear it.
internal static class ObservationFence {
 internal const string Key="AEGIS_PPT_IDENTITY_OBSERVATION_V1";
 internal static bool Active {get{return Environment.GetEnvironmentVariable(Key)=="ACTIVE";}}
 internal static bool Open {get{try{string s=Environment.GetEnvironmentVariable(Key);return s==null||s=="COMPLETE";}catch{return false;}}}
 internal static void Require(){if(!Open)throw new COMException("identity_observation_protected",unchecked((int)0x80004005));}
 internal static void Unknown(){Environment.SetEnvironmentVariable(Key,"UNKNOWN");}
 internal static bool Begin(){if(Environment.GetEnvironmentVariable(Key)!=null)return false;Environment.SetEnvironmentVariable(Key,"ACTIVE");return Environment.GetEnvironmentVariable(Key)=="ACTIVE";}
 internal static bool Complete(){if(Environment.GetEnvironmentVariable(Key)!="ACTIVE")return false;Environment.SetEnvironmentVariable(Key,"COMPLETE");return Environment.GetEnvironmentVariable(Key)=="COMPLETE";}
}
public sealed partial class Connect {
 private uint observationOwner;
 /// <summary>One bound owner-STA observation; returns primitive JSON only.</summary>
 public string ObserveExternalIdentity(string nonce,object candidate){
  if(ObservationFence.Active){ObservationFence.Unknown();throw new COMException("observation_reentrant");}
  RequireHostThread();
  int apartment,qualifier;
  if(Environment.GetEnvironmentVariable(ExternalOptIn)!="1")throw new COMException("diagnostic_disabled");
  if(ProcessFence.GetCurrentThreadId()!=observationOwner||ProcessFence.CoGetApartmentType(out apartment,out qualifier)!=0||apartment!=3||qualifier!=0)throw new COMException("owner_rejected");
  if(bootstrapCaps==null||bootstrapCaps.IsClosed||nonce!=bootstrapNonce||nonce!=Environment.GetEnvironmentVariable("AEGIS_TEST_NONCE")||bootstrapGeneration!=externalGeneration)throw new COMException("bootstrap_binding_rejected");
  if(external!=null||reading||shutdown==null||shutdown.Entered)throw new COMException("observation_busy");
  // Consume before acquiring a host interface. Reentry cannot reset this latch.
  if(!ObservationFence.Begin())throw new COMException("observation_consumed");
  int generation=externalGeneration;var guard=shutdown;
  bool complete=false;
  try{
   var report=IdentityObservation.Capture(application,candidate,(int)observationOwner,generation,()=>ObservationFence.Active?externalGeneration:-1);
   if(!report.usable||report.outcome!="complete"||generation!=externalGeneration||!Object.ReferenceEquals(guard,shutdown))return new JavaScriptSerializer().Serialize(report);
   string json=new JavaScriptSerializer().Serialize(report);
   complete=ObservationFence.Complete();
   if(!complete)throw new COMException("identity_observation_protected");
   return json;
  }finally{
   if(!complete){
    // Keep the process blocked even if a subsequent cleanup operation fails.
    try{ObservationFence.Unknown();}finally{guard.NoteExternalUnknown();}
   }
  }
}
}
}
#endif
