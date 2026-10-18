#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
using System;
using AegisDesk.PptReader;
internal sealed class DiagnosticOperation {
 internal readonly PptCleanupErrors Errors=new PptCleanupErrors();
 internal readonly System.Collections.Generic.List<Exception> CleanupFailures=new System.Collections.Generic.List<Exception>();
 internal void Primary(Exception error){if(error is PptCleanupOutcomeUnknown)Errors.Unknown=true;Errors.Observe(error,true);}
 internal void Cleanup(Action action){try{action();}catch(Exception error){CleanupFailures.Add(error);Errors.Unknown=true;Errors.Observe(error,false);}}
 internal T Run<T>(Func<T> action,Action cleanup){
  T result=default(T);
  try{result=action();}catch(Exception error){Primary(error);}
  Cleanup(cleanup);
  Errors.Throw();return result;
 }
}

#endif
