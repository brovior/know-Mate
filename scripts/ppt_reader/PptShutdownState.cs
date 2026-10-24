using System.Runtime.InteropServices;

namespace AegisDesk.PptReader
{
    /// <summary>Managed-only state shared with the optional event sink.</summary>
    [ComVisible(false)]
    public sealed class PptShutdownState
    {
        /// <summary>True only while the synchronous host-STA guard is active.</summary>
        public bool Active;
        /// <summary>Sticky cancellation observation for this guard invocation.</summary>
        public bool Cancelled;
        /// <summary>True only after a close event has actually been cancelled.</summary>
        public bool CloseCancelled;
        /// <summary>Allows a later manual close after a deferred automatic close was cancelled.</summary>
        public bool DeferredProtection;
        /// <summary>Once a user arrives during shutdown, never reacquire this invocation.</summary>
        public void ObserveUserActivity() { if (Active) Cancelled = true; }
#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
        internal ExternalTrace Telemetry;
#endif
        /// <summary>Record primitive cancellation state without touching event arguments.</summary>
        public void RecordCancellation(int kind) {
#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
            if(Telemetry!=null)Telemetry.Record(ExternalTrace.Stage.Cancel,ExternalTrace.Result.Success,kind,Active?1:0,Cancelled?1:0);
#endif
        }
        /// <summary>Record callback input/output without reading a presentation object.</summary>
        public void EventBoundary(int kind,bool enter,bool incoming,bool outgoing,bool setByAegis) {
#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
            if(Telemetry!=null)Telemetry.EventBoundary(kind,enter,(incoming?1:0)|(outgoing?2:0)|(setByAegis?4:0)|(Active?8:0)|(Cancelled?16:0)|(Broken?32:0));
#endif
        }
        private bool broken;
        /// <summary>Failure cannot be cleared by a callback or a later RPC.</summary>
        public bool Broken { get { return broken; } }
        /// <summary>Disable shutdown capability after an internal callback failure.</summary>
        public void MarkBroken() { broken=true; }
    }
}

// REVIEW DRAFT: diagnostic-only buffer; no Office objects or references are stored.
namespace AegisDesk.PptReader
{
    internal sealed class PptLifecycleTrace
    {
        private struct Entry { internal int stage, flags, detail, thread; }
        // Allocated when Connect is constructed, before lifecycle callbacks.
        private readonly bool enabled;
        private readonly Entry[] entries;
        private readonly string outputDirectory;
        internal PptLifecycleTrace()
        {
#if AEGIS_PPT_CAPTURE_DIAGNOSTIC
            if(System.Environment.GetEnvironmentVariable("AEGIS_PPT_CAPTURE_DIAGNOSTIC")=="1")return;
#endif
            // Object construction, not an Office lifecycle callback.
            try
            {
                if (System.Environment.GetEnvironmentVariable("AEGIS_PPT_LIFECYCLE_TRACE") == "1")
                {
                    outputDirectory = PptLifecycleFile.CaptureDirectory(
                        System.Environment.GetEnvironmentVariable("AEGIS_PPT_LIFECYCLE_DIR"));
                    enabled = outputDirectory != null;
                }
            }
            catch { enabled = false; }
            if (enabled) entries = new Entry[64];
        }
        private int sequence, active, ended, eligible, exportConsumed;
        private bool IsOwner()
        { return enabled && System.Threading.Thread.CurrentThread.ManagedThreadId ==
            System.Threading.Interlocked.CompareExchange(ref ownerThread,0,0); }
        internal int Begin()
        {
            if (!IsOwner() || active != 0 || sequence == System.Int32.MaxValue) return 0;
            active = ++sequence; eligible = 0; return active;
        }
        internal void End(int token)
        { if (IsOwner() && token != 0 && active == token) { active = 0; ended = token; } }
        internal int Complete(int token)
        {
            if (!IsOwner() || token == 0 || active != 0 || ended != token || exportConsumed != 0) return 0;
            eligible = token; return token;
        }
        private int count, dropped, ownerThread, offThreadDroppedLowerBound;
        internal void BindOwner(int thread)
        { if (enabled) System.Threading.Interlocked.CompareExchange(ref ownerThread, thread, 0); }
        private void NoteOffThread()
        {
            // One bounded CAS attempt; contention may undercount but cannot wrap.
            int old=System.Threading.Interlocked.CompareExchange(ref offThreadDroppedLowerBound,0,0);
            if(old<65535) System.Threading.Interlocked.CompareExchange(ref offThreadDroppedLowerBound,old+1,old);
        }
        internal void Record(int stage, int flags, int detail)
        {
            try
            {
                if (!enabled) return;
                int current=System.Threading.Thread.CurrentThread.ManagedThreadId;
                int owner=System.Threading.Interlocked.CompareExchange(ref ownerThread,0,0);
                if(owner==0 || current!=owner) { NoteOffThread(); return; }
                if (count >= entries.Length) { if (dropped < 65535) dropped++; return; }
                entries[count] = new Entry { stage=stage, flags=flags, detail=detail, thread=current };
                count++; // Publish only after the owner has written the complete value.
            }
            catch { /* Diagnostics cannot change callback behavior. */ }
        }
        internal void FlushNormalReturn(int token)
        {
            try
            {
                // Only the completed outer owner-STA invocation owns this capability.
                if (!IsOwner() || token == 0 || active != 0 || token != eligible || exportConsumed != 0) return;
                exportConsumed = token;
                int snapshotCount=count, snapshotDropped=dropped;
                int snapshotOffThread=System.Threading.Interlocked.CompareExchange(ref offThreadDroppedLowerBound,0,0);
                var snapshot=new Entry[snapshotCount];
                System.Array.Copy(entries,snapshot,snapshotCount);
                var text=new System.Text.StringBuilder();
                text.AppendLine("schema=1 count="+snapshotCount+" dropped="+snapshotDropped+" off_thread_dropped_lower_bound="+snapshotOffThread);
                for(int i=0;i<snapshotCount;i++)
                { Entry e=snapshot[i]; text.AppendLine(i+" "+e.stage+" "+e.flags+" "+e.detail+" "+e.thread); }
                PptLifecycleFile.WriteOnce(text.ToString(), outputDirectory);
            }
            catch { /* Diagnostic failure cannot change outcome. No retry. */ }
        }
    }
}

namespace AegisDesk.PptReader
{
    // Diagnostic-only, fail-closed writer. No method is called from callbacks.
    internal static class PptLifecycleFile
    {
        [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]
        private struct Info
        {
            internal uint attributes;
            internal System.Runtime.InteropServices.ComTypes.FILETIME created, accessed, written;
            internal uint volume, sizeHigh, sizeLow, links, indexHigh, indexLow;
        }
        [System.Runtime.InteropServices.DllImport("kernel32.dll", CharSet=System.Runtime.InteropServices.CharSet.Unicode, SetLastError=true)]
        private static extern Microsoft.Win32.SafeHandles.SafeFileHandle CreateFile(string path, uint access, uint share,
            System.IntPtr security, uint disposition, uint flags, System.IntPtr template);
        [System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError=true)]
        private static extern bool GetFileInformationByHandle(Microsoft.Win32.SafeHandles.SafeFileHandle handle, out Info info);
        [System.Runtime.InteropServices.DllImport("kernel32.dll", CharSet=System.Runtime.InteropServices.CharSet.Unicode, SetLastError=true)]
        private static extern uint GetFinalPathNameByHandle(Microsoft.Win32.SafeHandles.SafeFileHandle handle,
            System.Text.StringBuilder path, uint length, uint flags);
        [System.Runtime.InteropServices.DllImport("kernel32.dll", CharSet=System.Runtime.InteropServices.CharSet.Unicode)]
        private static extern uint GetDriveType(string root);
        [System.Runtime.InteropServices.DllImport("kernel32.dll", CharSet=System.Runtime.InteropServices.CharSet.Unicode, SetLastError=true)]
        private static extern bool GetVolumeInformationByHandleW(Microsoft.Win32.SafeHandles.SafeFileHandle handle,
            System.Text.StringBuilder volumeName, uint length, out uint serial, out uint maxComponent, out uint flags,
            System.Text.StringBuilder fileSystem, uint fileSystemLength);

        internal static string CaptureDirectory(string input)
        {
            string captured=null;
            WithDirectory(input, delegate(string directory, uint volume) { captured=directory; });
            return captured;
        }

        internal static void WriteOnce(string text, string directory)
        {
            if (text == null || text.Length > 16384) return;
            WithDirectory(directory, delegate(string verified, uint expectedVolume)
            {
                WriteHeld(text, verified, expectedVolume);
            });
        }

        private static void WithDirectory(string input, System.Action<string, uint> action)
        {
            var held=new System.Collections.Generic.List<Microsoft.Win32.SafeHandles.SafeFileHandle>();
            try
            {
                // Explicit drive-absolute paths only; never resolve a relative or profile fallback.
                if (System.String.IsNullOrWhiteSpace(input) || input.Length < 3 ||
                    !((input[0] >= 'A' && input[0] <= 'Z') || (input[0] >= 'a' && input[0] <= 'z')) ||
                    input[1] != ':' || input[2] != '\\') return;
                string directory=System.IO.Path.GetFullPath(input).TrimEnd('\\');
                string root=System.IO.Path.GetPathRoot(directory);
                if (root.Length != 3 || root[1] != ':' || GetDriveType(root) != 3) return;
                string current=root;
                string relative=directory.Substring(root.Length);
                var paths=new System.Collections.Generic.List<string>(); paths.Add(root);
                foreach(string component in relative.Split('\\'))
                { if(component.Length==0) return; current=System.IO.Path.Combine(current,component); paths.Add(current); }
                uint expectedVolume=0;
                foreach(string path in paths)
                {
                    // FILE_LIST_DIRECTORY participates in sharing checks; attributes-only access does not.
                    // Existing directories only, OPEN_REPARSE_POINT; deny write/delete sharing.
                    var handle=CreateFile(path,0x81,1,System.IntPtr.Zero,3,0x02200000,System.IntPtr.Zero);
                    if(handle.IsInvalid) { handle.Dispose(); return; }
                    held.Add(handle);
                    Info info;
                    if(!GetFileInformationByHandle(handle,out info) || (info.attributes & 0x10)==0 ||
                       (info.attributes & 0x400)!=0) return;
                    if(expectedVolume==0) expectedVolume=info.volume;
                    else if(info.volume!=expectedVolume) return;
                    var actual=new System.Text.StringBuilder(32768);
                    uint size=GetFinalPathNameByHandle(handle,actual,(uint)actual.Capacity,0);
                    if(size==0 || size>=actual.Capacity || !System.String.Equals(actual.ToString().TrimEnd('\\'),
                        (@"\\?\"+path).TrimEnd('\\'),System.StringComparison.OrdinalIgnoreCase)) return;
                }
                uint serial, max, flags; var fs=new System.Text.StringBuilder(32);
                if(!GetVolumeInformationByHandleW(held[held.Count-1],null,0,out serial,out max,out flags,fs,32) ||
                    fs.ToString()!="NTFS" || serial!=expectedVolume) return;
                action(directory, expectedVolume);
            }
            catch { /* Fail closed, never retry, never touch Office. */ }
            finally { for(int i=held.Count-1;i>=0;i--) held[i].Dispose(); }
        }
        #if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
        internal static bool WriteExternalSnapshot(string text,string directory,int snapshot)
        {
            if(text==null||text.Length>16384||snapshot<0||snapshot>1)return false;
            bool written=false;
            WithDirectory(directory,delegate(string verified,uint volume){written=WriteHeldNamed(text,verified,volume,"host-"+System.Diagnostics.Process.GetCurrentProcess().Id+"-external-"+snapshot+".log");});
            return written;
        }
#endif
        private static void WriteHeld(string text,string directory,uint expectedVolume)
        { WriteHeldNamed(text,directory,expectedVolume,"host-"+System.Diagnostics.Process.GetCurrentProcess().Id.ToString(System.Globalization.CultureInfo.InvariantCulture)+".log"); }
        private static bool WriteHeldNamed(string text, string directory, uint expectedVolume,string name)
        {
                string file=System.IO.Path.Combine(directory,name);
                using(var handle=CreateFile(file,0x40000000,0,System.IntPtr.Zero,1,0x00200080,System.IntPtr.Zero))
                {
                    if(handle.IsInvalid) return false;
                    Info info;
                    if(!GetFileInformationByHandle(handle,out info) || (info.attributes & (0x10|0x400))!=0 ||
                        info.links!=1 || info.volume!=expectedVolume) return false;
                    using(var stream=new System.IO.FileStream(handle,System.IO.FileAccess.Write))
                    { byte[] bytes=System.Text.Encoding.ASCII.GetBytes(text); stream.Write(bytes,0,bytes.Length); }
                    return true;
                }
        }
    }
}
