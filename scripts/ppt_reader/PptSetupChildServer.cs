using System;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Threading;

namespace AegisDesk.PptSetup
{
    /// <summary>A bounded observer, independent of the install process lifetime.</summary>
    internal sealed partial class ChildDiagnosticServer : IDisposable
    {
        static partial void ObservePeer(NamedPipeServerStream stream);
        static partial void ObserveFinished();
        private readonly object gate=new object();
        internal readonly Guid Nonce=Guid.NewGuid();
        private readonly Guid request;
        private readonly string phase;
        private NamedPipeServerStream pipe;
        private byte[] reading=new byte[4],payload;
        private int offset,step;
        private bool done,disposed;
        private uint? peer;
        private string failure="unavailable";
        private ChildDiagnosticServer(Guid request,string phase){this.request=request;this.phase=phase;}
        internal static ChildDiagnosticServer Start(string id,string phase)
        {
            Guid request;Guid.TryParseExact(id,"D",out request);var server=new ChildDiagnosticServer(request,phase);
            try {server.pipe=ChildDiagnosticPipe.Create(server.Nonce);server.pipe.BeginWaitForConnection(server.Accepted,null);}catch {server.Dispose();}
            return server;
        }
        internal string Arguments
        {
            get
            {
                try {if(pipe==null||disposed)return "";using(var process=Process.GetCurrentProcess())return " /diagnostic "+Nonce.ToString("N")+" "+process.Id.ToString(System.Globalization.CultureInfo.InvariantCulture)+" "+process.StartTime.ToUniversalTime().Ticks.ToString(System.Globalization.CultureInfo.InvariantCulture);}
                catch{return "";}
            }
        }
        private void FinishRead(string state)
        {lock(gate){if(done)return;failure=state;done=true;Monitor.PulseAll(gate);}ObserveFinished();}
        private void Accepted(IAsyncResult operation)
        {
            try
            {
                pipe.EndWaitForConnection(operation);uint pid;
                if(!ChildDiagnosticPipe.GetNamedPipeClientProcessId(pipe.SafePipeHandle,out pid)){FinishRead("peer_mismatch");return;}
                lock(gate){peer=pid;if(disposed)return;}
                ObservePeer(pipe);Read();
            }
            catch {FinishRead("unavailable");}
        }
        private void Read()
        {
            try {lock(gate){if(disposed)return;}pipe.BeginRead(reading,offset,reading.Length-offset,ReadCompleted,null);}
            catch {FinishRead("malformed");}
        }
        private void ReadCompleted(IAsyncResult operation)
        {
            try
            {
                int received=pipe.EndRead(operation);lock(gate){if(disposed)return;}
                if(step==2){FinishRead(received==0?"received":"malformed");return;}
                if(received==0){FinishRead("malformed");return;}
                offset+=received;if(offset<reading.Length){Read();return;}
                if(step==0)
                {
                    int size=BitConverter.ToInt32(reading,0);if(size<1||size>ChildDiagnosticWire.Maximum){FinishRead("malformed");return;}
                    reading=new byte[size];offset=0;step=1;Read();
                }
                else {payload=reading;reading=new byte[1];offset=0;step=2;Read();}
            }
            catch {FinishRead("malformed");}
        }
        internal ChildDiagnosticObservation Finish(IntPtr process,uint? code)
        {
            var result=new ChildDiagnosticObservation {Phase=phase,ExitCode=code};
            try
            {
                if(pipe==null||disposed||process==IntPtr.Zero||!code.HasValue)return result;
                uint pid=ChildDiagnosticPipe.GetProcessId(process);if(pid==0)return result;
                var clock=Stopwatch.StartNew();
                lock(gate)
                {
                    while(!done&&clock.ElapsedMilliseconds<1000)Monitor.Wait(gate,(int)Math.Max(1,1000-clock.ElapsedMilliseconds));
                    if(peer.HasValue&&peer.Value!=pid){result.Transport="peer_mismatch";return result;}
                    if(!done){result.Transport="timeout";return result;}
                    result.Transport=failure;
                    if(failure!="received")return result;
                    if(!peer.HasValue){result.Transport="peer_mismatch";return result;}
                    try {return ChildDiagnosticWire.Decode(payload,request,Nonce,pid,code.Value,phase);}
                    catch {result.Transport="malformed";return result;}
                }
            }
            catch {return result;}
            finally {Dispose();}
        }
        public void Dispose()
        {
            NamedPipeServerStream close;
            lock(gate){if(disposed)return;disposed=true;close=pipe;Monitor.PulseAll(gate);}
            if(close!=null){ChildDiagnosticPipe.Cancel(close);try {close.Dispose();}catch {}}
        }
    }
}
