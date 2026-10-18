using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Web.Script.Serialization;
using Microsoft.Win32;

namespace AegisDesk.PptReader
{
    internal static class PptSinkLoader
    {
        internal const string EventsDll="AegisDesk.PptReader.Events.dll";
        private const string CoreDll="AegisDesk.PptReader.dll";
        private static string Hash(string path)
        {
            using(var stream=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.Read))
            using(var sha=SHA256.Create()) {
                if(stream.Length>32*1024*1024) throw new IOException("ppt_event_payload_size");
                return BitConverter.ToString(sha.ComputeHash(stream)).Replace("-","").ToLowerInvariant();
            }
        }
        private static void NoLinks(string path)
        {
            for(string current=Path.GetFullPath(path);current!=null;current=Path.GetDirectoryName(current))
                if((File.Exists(current)||Directory.Exists(current)) && (File.GetAttributes(current)&FileAttributes.ReparsePoint)!=0)
                    throw new IOException("ppt_event_reparse");
        }
        private static void Protected(string path,bool directory)
        {
            NoLinks(path);
            FileSystemSecurity acl=directory?(FileSystemSecurity)Directory.GetAccessControl(path):File.GetAccessControl(path);
            var owner=(SecurityIdentifier)acl.GetOwner(typeof(SecurityIdentifier));
            if(!owner.IsWellKnown(WellKnownSidType.BuiltinAdministratorsSid) && !owner.IsWellKnown(WellKnownSidType.LocalSystemSid))
                throw new IOException("ppt_event_owner");
            foreach(FileSystemAccessRule rule in acl.GetAccessRules(true,true,typeof(SecurityIdentifier))) {
                var sid=(SecurityIdentifier)rule.IdentityReference;
                if(rule.AccessControlType==AccessControlType.Allow && !sid.IsWellKnown(WellKnownSidType.BuiltinAdministratorsSid)
                    && !sid.IsWellKnown(WellKnownSidType.LocalSystemSid)
                    && (rule.FileSystemRights&(FileSystemRights.Write|FileSystemRights.Delete|FileSystemRights.DeleteSubdirectoriesAndFiles|FileSystemRights.ChangePermissions|FileSystemRights.TakeOwnership))!=0)
                    throw new IOException("ppt_event_acl");
            }
        }
        internal static object Create(PptShutdownState state)
        {
            try {
                string programFiles;
                using(var machine=RegistryKey.OpenBaseKey(RegistryHive.LocalMachine,Environment.Is64BitOperatingSystem?RegistryView.Registry64:RegistryView.Registry32))
                using(var current=machine.OpenSubKey(@"SOFTWARE\Microsoft\Windows\CurrentVersion"))
                    programFiles=current==null?null:current.GetValue("ProgramFilesDir") as string;
                if(String.IsNullOrEmpty(programFiles) || !Path.IsPathRooted(programFiles)) throw new IOException("ppt_event_install_root");
                string version=typeof(Connect).Assembly.GetName().Version.ToString();
                string root=Path.Combine(programFiles,"AegisDesk","PptReader"), directory=Path.Combine(root,version);
                string core=Path.Combine(directory,CoreDll), record=Path.Combine(root,"installation.json");
                if(!String.Equals(Path.GetFullPath(typeof(Connect).Assembly.Location),Path.GetFullPath(core),StringComparison.OrdinalIgnoreCase))
                    throw new IOException("ppt_event_install_location");
                foreach(string path in new[]{Path.GetDirectoryName(root),root,directory}) Protected(path,true);
                foreach(string path in new[]{record,core,Path.Combine(directory,EventsDll)}) Protected(path,false);
                return LoadValidated(directory,record,state);
            } catch(Exception) { throw new COMException("ppt_event_dependency_unavailable"); }
        }
        // Core-only seam: callers must establish the protected installation boundary above.
        // Offline fixtures exercise this parser/loader on their own synthetic package.
        internal static object LoadValidated(string directory,string recordPath,PptShutdownState state)
        {
            NoLinks(recordPath);
            if(new FileInfo(recordPath).Length>16384) throw new IOException("ppt_event_record_size");
            var json=new JavaScriptSerializer {MaxJsonLength=16384};
            var record=json.Deserialize<Dictionary<string,object>>(File.ReadAllText(recordPath));
            string version=typeof(Connect).Assembly.GetName().Version.ToString();
            string core=Path.Combine(directory,CoreDll), events=Path.Combine(directory,EventsDll);
            if(!(record["schema"] is int) || (int)record["schema"]!=2 || (string)record["version"]!=version
                || (string)record["prog_id"]!="AegisDesk.PptReader" || (string)record["scope"]!="Machine"
                || (string)record["class_id"]!="{287A80D2-804A-46D3-A0E9-CA31C6FC3D09}"
                || !(record["bitness"] is int) || (int)record["bitness"]!=IntPtr.Size*8
                || !String.Equals((string)record["path"],core,StringComparison.OrdinalIgnoreCase)
                || !String.Equals(typeof(Connect).Assembly.Location,core,StringComparison.OrdinalIgnoreCase))
                throw new IOException("ppt_event_record_identity");
            var files=record["files"] as Dictionary<string,object>;
            if(files==null || files.Count!=2 || !files.ContainsKey(CoreDll) || !files.ContainsKey(EventsDll)) throw new IOException("ppt_event_record_files");
            var packageMvids=new Dictionary<string,Guid>();
            foreach(string name in new[]{CoreDll,EventsDll}) {
                string path=Path.Combine(directory,name); NoLinks(path);
                var identity=AssemblyName.GetAssemblyName(path);
                if(identity.Name!=Path.GetFileNameWithoutExtension(name) || identity.Version.ToString()!=version || Hash(path)!=(string)files[name])
                    throw new IOException("ppt_event_payload_identity");
                packageMvids[name]=PptPackageIdentity.DiskMvid(path,(string)files[name]);
            }
            if((string)record["sha256"]!=(string)files[CoreDll]) throw new IOException("ppt_event_core_hash");
            PptPackageIdentity.RequireLoaded(typeof(Connect).Assembly,packageMvids[CoreDll]);
            foreach(string name in new[]{"Microsoft.Office.Interop.PowerPoint","office"}) {
                string identity=name+", Version=15.0.0.0, Culture=neutral, PublicKeyToken=71e9bce111e9429c";
                Assembly pia=Assembly.Load(identity);
                if(pia.FullName!=identity || !pia.GlobalAssemblyCache) throw new IOException("ppt_event_pia_identity");
            }
            using(var held=new FileStream(events,FileMode.Open,FileAccess.Read,FileShare.Read)) {
                if(Hash(events)!=(string)files[EventsDll]) throw new IOException("ppt_event_payload_changed");
                Assembly assembly=Assembly.LoadFrom(events);
                PptPackageIdentity.RequireLoaded(assembly,packageMvids[EventsDll]);
                if(!String.Equals(assembly.Location,events,StringComparison.OrdinalIgnoreCase) || assembly.GetName().Version.ToString()!=version)
                    throw new IOException("ppt_event_loaded_identity");
                Type type=assembly.GetType("AegisDesk.PptEvents.PptEventSink",true);
                object sink=Activator.CreateInstance(type,new object[]{state});
                IntPtr unknown=Marshal.GetIUnknownForObject(sink), pointer=IntPtr.Zero; Guid iid=PptEventSignatures.Iid;
                try { if(Marshal.QueryInterface(unknown,ref iid,out pointer)!=0) throw new IOException("ppt_event_sink_iid"); }
                finally { if(pointer!=IntPtr.Zero) Marshal.Release(pointer); Marshal.Release(unknown); }
                return sink;
            }
        }
    }
}
