#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
using System;
// Owner of already-adopted resources. Never constructed for invalid input handles.
internal sealed class DiagnosticResources:IDisposable {
 Action cleanup;internal int Attempts;internal Exception Error;
 internal DiagnosticResources(Action release){cleanup=release;}
 public void Dispose(){Action owned=cleanup;if(owned==null)return;cleanup=null;Attempts++;try{owned();}catch(Exception e){Error=e;throw;}}
}

#endif
