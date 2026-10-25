using System;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using Microsoft.Win32.SafeHandles;

namespace AegisDesk.PptSetup
{
    /// <summary>Native local-only, first-instance overlapped pipe boundary.</summary>
    internal static class ChildDiagnosticPipe
    {
        internal const string Prefix="AegisDesk.PptSetup.Diagnostics.";
        [StructLayout(LayoutKind.Sequential)] private struct Attributes {internal int Length;internal IntPtr Descriptor;internal int Inherit;}
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] private static extern SafePipeHandle CreateNamedPipe(string name,uint mode,uint pipeMode,uint instances,uint output,uint input,uint timeout,ref Attributes attributes);
        [DllImport("kernel32.dll",SetLastError=true)] internal static extern bool GetNamedPipeClientProcessId(SafePipeHandle pipe,out uint pid);
        [DllImport("kernel32.dll",SetLastError=true)] internal static extern bool GetNamedPipeServerProcessId(SafePipeHandle pipe,out uint pid);
        [DllImport("kernel32.dll",SetLastError=true)] internal static extern bool CancelIoEx(SafePipeHandle handle,IntPtr overlapped);
        [DllImport("kernel32.dll",SetLastError=true)] internal static extern uint GetProcessId(IntPtr handle);
        internal static NamedPipeServerStream Create(Guid nonce)
        {
            var acl=new PipeSecurity();acl.SetAccessRuleProtection(true,false);
            using(var identity=WindowsIdentity.GetCurrent())
            {
                acl.SetOwner(identity.User);
                foreach(var sid in new[]{identity.User,new SecurityIdentifier("S-1-5-32-544"),new SecurityIdentifier("S-1-5-18")})
                    acl.AddAccessRule(new PipeAccessRule(sid,PipeAccessRights.ReadWrite,AccessControlType.Allow));
            }
            byte[] descriptor=acl.GetSecurityDescriptorBinaryForm();GCHandle pin=GCHandle.Alloc(descriptor,GCHandleType.Pinned);SafePipeHandle handle=null;
            try
            {
                var attributes=new Attributes {Length=Marshal.SizeOf(typeof(Attributes)),Descriptor=pin.AddrOfPinnedObject()};
                // inbound|overlapped|first-instance; byte/wait|reject-remote, one client.
                handle=CreateNamedPipe(@"\\.\pipe\"+Prefix+nonce.ToString("N"),0x40080001,8,1,4096,4096,0,ref attributes);
                if(handle.IsInvalid)throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
                var stream=new NamedPipeServerStream(PipeDirection.In,true,false,handle);handle=null;return stream;
            }
            finally {if(handle!=null)handle.Dispose();pin.Free();}
        }
        internal static void Cancel(PipeStream stream)
        {try {if(stream!=null&&!stream.SafePipeHandle.IsInvalid&&!stream.SafePipeHandle.IsClosed)CancelIoEx(stream.SafePipeHandle,IntPtr.Zero);}catch {}}
    }

    /// <summary>The elevated writer cannot lend its administrator token to the server.</summary>
    internal static class ChildDiagnosticClient
    {
        internal static bool Send(Guid nonce,int parent,string ticks,byte[] frame)
        {
            NamedPipeClientStream pipe=null;var gate=new object();bool done=false,success=false;
            try
            {
                pipe=new NamedPipeClientStream(".",ChildDiagnosticPipe.Prefix+nonce.ToString("N"),PipeDirection.Out,PipeOptions.Asynchronous,TokenImpersonationLevel.Anonymous);
                pipe.Connect(500);uint peer;
                if(!ChildDiagnosticPipe.GetNamedPipeServerProcessId(pipe.SafePipeHandle,out peer)||peer!=(uint)parent)return false;
                using(var process=Process.GetProcessById(parent))if(process.StartTime.ToUniversalTime().Ticks.ToString(System.Globalization.CultureInfo.InvariantCulture)!=ticks)return false;
                NamedPipeClientStream writer=pipe;
                pipe.BeginWrite(frame,0,frame.Length,delegate(IAsyncResult operation)
                {
                    bool wrote=false;try {writer.EndWrite(operation);wrote=true;}catch {}
                    lock(gate){success=wrote;done=true;System.Threading.Monitor.PulseAll(gate);}
                },null);
                var clock=Stopwatch.StartNew();lock(gate){while(!done&&clock.ElapsedMilliseconds<500)System.Threading.Monitor.Wait(gate,(int)Math.Max(1,500-clock.ElapsedMilliseconds));return done&&success;}
            }
            catch {return false;}
            finally {if(pipe!=null){ChildDiagnosticPipe.Cancel(pipe);try{pipe.Dispose();}catch {}}}
        }
    }
}
