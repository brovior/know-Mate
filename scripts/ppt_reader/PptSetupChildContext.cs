using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.IO;

namespace AegisDesk.PptSetup
{
    /// <summary>In-memory, fail-safe diagnostic context; it never authorizes mutation.</summary>
    internal static class ChildDiagnostics
    {
        [ThreadStatic] private static ChildDiagnosticObservation active;
        [ThreadStatic] private static ChildDiagnosticError pending;
        [ThreadStatic] private static List<object> observations;
        internal static void Reset() { active=null;pending=null;observations=new List<object>(); }
        internal static object Observations { get { return observations!=null&&observations.Count!=0?observations.ToArray():null; } }
        internal static void Add(ChildDiagnosticObservation observation)
        { try { if(observations==null)observations=new List<object>();if(observations.Count<2)observations.Add(observation.Values()); } catch { } }
        internal static ChildDiagnosticObservation Start(string phase)
        {
            active=new ChildDiagnosticObservation {Phase=phase,LastStage="child_payload_check",LastTarget="payload"};pending=null;return active;
        }
        internal static void Stop() { active=null;pending=null; }
        internal static void Mark(string stage,string target)
        {
            try { if(active==null)return; if(!ChildDiagnosticContract.Has(ChildDiagnosticContract.Stages,stage)||!ChildDiagnosticContract.Has(ChildDiagnosticContract.Targets,target))return;
                active.LastStage=stage;active.LastTarget=target;pending=null; } catch { }
        }
        internal static T At<T>(string stage,string target,Func<T> action) { Mark(stage,target);return action(); }
        internal static bool HasPrimary {get {return active!=null&&active.Primary!=null;}}
        private sealed class SavedStage:IDisposable
        {
            internal string Stage,Target;
            public void Dispose(){try{if(active!=null){active.LastStage=Stage;active.LastTarget=Target;}}catch{}}
        }
        internal static IDisposable Scope(string stage,string target)
        {
            try {if(active==null)return new SavedStage();var saved=new SavedStage {Stage=active.LastStage,Target=active.LastTarget};Mark(stage,target);return saved;}catch{return new SavedStage();}
        }
        internal static void Pending(Exception error)
        {
            try { if(active==null)return; var origin=error.Data["aegis_child_origin"] as ChildDiagnosticError;
                pending=origin==null?ChildDiagnosticError.From(error,active.LastStage,active.LastTarget):origin.Copy(); } catch { }
        }
        internal static void NativeFailure(int code)
        { try { if(active!=null&&code>=0)pending=new ChildDiagnosticError {Stage=active.LastStage,Target=active.LastTarget,ErrorType="Win32Exception",WinError=code}; } catch { } }
        internal static void Policy(string reason)
        { try { if(active!=null&&pending==null&&ChildDiagnosticContract.Has(ChildDiagnosticContract.Policies,reason))pending=new ChildDiagnosticError {Stage=active.LastStage,Target=active.LastTarget,ErrorType="policy",PolicyReason=reason}; } catch { } }
        internal static void Attach(Exception error)
        { try { if(active!=null&&pending!=null)error.Data["aegis_child_origin"]=pending.Copy(); } catch { } }
        internal static void AttachNative(SetupFailure error)
        {
            try {if(active==null)return;var origin=error.Data["aegis_child_origin"] as ChildDiagnosticError;
                if(origin==null)origin=ChildDiagnosticError.From(error,active.LastStage,active.LastTarget);else origin=origin.Copy();
                origin.Stage="request_commit";origin.NativeStage=error.NativeStage;origin.NtStatus=error.NtStatus;origin.IoStatus=error.IoStatus;
                error.Data["aegis_child_origin"]=origin;pending=origin.Copy();
            }catch {}
        }
        internal static void Capture(Exception error)
        { try { if(active==null||active.Primary!=null)return; if(pending==null)Pending(error);active.Primary=pending==null?null:pending.Copy(); } catch { } }
        internal static void CaptureSecondary(Exception error)
        { try { if(active==null||active.Primary==null||active.Secondary!=null)return; Pending(error);active.Secondary=pending==null?null:pending.Copy(); } catch { } }
        internal static void Gate(Result check)
        {
            try { if(active==null||active.Primary!=null)return;if(pending==null)Policy("mutation_blocked");
                if(ChildDiagnosticContract.Has(ChildDiagnosticContract.Statuses,check.status)&&ChildDiagnosticContract.Has(ChildDiagnosticContract.Reasons,check.reason)){pending.CheckStatus=check.status;pending.CheckReason=check.reason;}
                active.Primary=pending.Copy(); } catch { }
        }
        internal static string Target(string directory)
        {
            try {
                if(String.Equals(directory,Machine.Root,StringComparison.OrdinalIgnoreCase))return "reader_root";
                if(String.Equals(directory,Path.GetDirectoryName(Machine.Root),StringComparison.OrdinalIgnoreCase))return "program_files_parent";
                return "version_directory";
            }catch{return "none";}
        }
        internal static void Complete(uint code)
        {
            try { if(active==null)return;active.ExitCode=code;if(code==0){active.Primary=null;active.Secondary=null;Mark("child_complete","none");}
                else if(active.Primary==null)Capture(new IOException()); } catch { }
        }
    }

    /// <summary>Optional six-argument diagnostic attachment keeps legacy calls valid.</summary>
    internal sealed class ChildDiagnosticSession : IDisposable
    {
        private Guid request,nonce;
        private int parent;
        private string ticks;
        private ChildDiagnosticObservation observation;
        internal static ChildDiagnosticSession Begin(string[] args,out string[] normalized)
        {
            normalized=args;
            try
            {
                if(args.Length!=6||args[2]!="/diagnostic")return null;
                string phase=ChildDiagnosticContract.Phase(args[0].TrimStart('/'));Guid request,nonce;int pid;long creation;
                if(phase==null||!Guid.TryParseExact(args[1],"D",out request)||request.ToString("D")!=args[1]||!Guid.TryParseExact(args[3],"N",out nonce)||nonce.ToString("N")!=args[3]||
                    !Int32.TryParse(args[4],NumberStyles.None,CultureInfo.InvariantCulture,out pid)||pid<=0||!Int64.TryParse(args[5],NumberStyles.None,CultureInfo.InvariantCulture,out creation)||creation<=0)return null;
                normalized=new[]{args[0],args[1]};
                return new ChildDiagnosticSession {request=request,nonce=nonce,parent=pid,ticks=args[5],observation=ChildDiagnostics.Start(phase)};
            }
            catch { return null; }
        }
        internal void Finish(int code)
        {
            try { ChildDiagnostics.Complete(unchecked((uint)code));using(var process=Process.GetCurrentProcess())ChildDiagnosticClient.Send(nonce,parent,ticks,ChildDiagnosticWire.Encode(request,nonce,unchecked((uint)process.Id),observation)); } catch { }
        }
        public void Dispose() { ChildDiagnostics.Stop(); }
    }
}
