// TEST ONLY: environment/registry/filesystem seams; compile real PptSetupMachine and Mutation/Ownership unchanged.
// No registry, filesystem, ACL, Office or UAC mutations are performed by these types.
using System;
using System.Collections.Generic;
using System.Linq;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Web.Script.Serialization;

namespace Microsoft.Win32
{
    internal sealed class RegistryKey:IDisposable
    {
        internal sealed class Node {
            internal Dictionary<string,Node> Children=new Dictionary<string,Node>();
            internal Dictionary<string,Tuple<object,RegistryValueKind>> Values=new Dictionary<string,Tuple<object,RegistryValueKind>>();
        }
        private static Node root=new Node(); private readonly Node node;
        internal static int Writes,Deletes,Creates,FailAt;
        private RegistryKey(Node node) { this.node=node; }
        internal static void Reset() { root=new Node(); Writes=FailAt=0; }
        internal static string Dump() { return Dump(root); }
        private static string Dump(Node n) {
            return "["+String.Join(";",n.Values.OrderBy(x=>x.Key).Select(x=>x.Key+":"+x.Value.Item2+":"+new JavaScriptSerializer().Serialize(x.Value.Item1)))+"]{"+
                String.Join(";",n.Children.OrderBy(x=>x.Key).Select(x=>x.Key+":"+Dump(x.Value)))+"}";
        }
        public static RegistryKey OpenBaseKey(RegistryHive hive,RegistryView view) { return new RegistryKey(root); }
        public RegistryKey OpenSubKey(string path) { return Find(path,false); }
        public RegistryKey CreateSubKey(string path) { Creates++; return Find(path,true); }
        private RegistryKey Find(string path,bool create) {
            Node current=node;
            foreach(string part in path.Split('\\')) { Node child; if(!current.Children.TryGetValue(part,out child)) {
                if(!create) return null; child=new Node(); current.Children.Add(part,child);
            } current=child; }
            return new RegistryKey(current);
        }
        public void DeleteSubKeyTree(string path,bool missing) { Deletes++;
            string[] parts=path.Split('\\'); Node current=node;
            for(int i=0;i<parts.Length-1;i++) { Node next; if(!current.Children.TryGetValue(parts[i],out next)) return; current=next; }
            current.Children.Remove(parts[parts.Length-1]);
        }
        public void SetValue(string name,object value,RegistryValueKind kind) {
            Writes++; if(FailAt==Writes) { FailAt=0; throw new System.IO.IOException("synthetic_registry_failure"); }
            node.Values[name]=Tuple.Create(value,kind);
        }
        public object GetValue(string name) { return GetValue(name,null,RegistryValueOptions.None); }
        public object GetValue(string name,object fallback,RegistryValueOptions options) { Tuple<object,RegistryValueKind> value; return node.Values.TryGetValue(name,out value)?value.Item1:fallback; }
        public RegistryValueKind GetValueKind(string name) { return node.Values[name].Item2; }
        public string[] GetValueNames() { return node.Values.Keys.ToArray(); }
        public string[] GetSubKeyNames() { return node.Children.Keys.ToArray(); }
        public void Dispose() { }
    }
}
namespace AegisDesk.PptSetup
{
    internal sealed class Blob { internal string Version,Hash; internal byte[] Bytes=new byte[0]; internal Blob(string version,string hash) { Version=version; Hash=hash; } }
    internal static class File
    {
        internal static int CopyCalls,AclCalls,FailCopy,FailAcl,Deletes;
        internal static bool Trusted=true;
        internal static readonly Dictionary<string,Blob> Blobs=new Dictionary<string,Blob>();
        internal static bool Exists(string path) { return Blobs.ContainsKey(path)||Contract.Metadata.ContainsKey(path); }
        internal static void Delete(string path) { Deletes++; Blobs.Remove(path); Contract.Metadata.Remove(path); }
        internal static void Copy(string source,string target,bool overwrite) {
            CopyCalls++; if(CopyCalls==FailCopy) { FailCopy=0; throw new System.IO.IOException("synthetic_copy_failure"); }
            if(Blobs.ContainsKey(target)&&!overwrite) throw new System.IO.IOException("exists"); Blobs[target]=Blobs[source];
        }
        internal static System.IO.FileAttributes GetAttributes(string path) { return System.IO.FileAttributes.Normal; }
        internal static System.IO.Stream OpenRead(string path) { return new System.IO.MemoryStream(Blobs[path].Bytes); }
        internal static FileSecurity GetAccessControl(string path) {
            var acl=new FileSecurity(); acl.SetOwner(new SecurityIdentifier(Trusted?WellKnownSidType.BuiltinAdministratorsSid:WellKnownSidType.BuiltinUsersSid,null)); return acl;
        }
        internal static void SetAccessControl(string path,FileSecurity acl) {
            AclCalls++; if(AclCalls==FailAcl) { FailAcl=0; throw new System.IO.IOException("synthetic_acl_failure"); }
        }
    }
    internal static class Directory
    {
        internal static bool Exists(string path) { return path!=Machine.Requests && !path.EndsWith(".json") && !File.Blobs.ContainsKey(path); }
        internal static string[] GetFileSystemEntries(string path) { return new string[0]; }
        internal static string[] GetFileSystemEntries(string path,string pattern) { return new string[0]; }
        internal static DirectorySecurity GetAccessControl(string path) {
            var acl=new DirectorySecurity(); acl.SetOwner(new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid,null)); return acl;
        }
    }
    internal static class RequestDirectory { internal static int Creates; internal static void Create(string path,DirectorySecurity acl) { Creates++; } }
    internal sealed class RequestDirectoryLease { internal int Checks; internal void Recheck() { Checks++; } }
    internal sealed class AssemblyName
    {
        internal Version Version; internal string FullName,Name;
        internal AssemblyName(string value) { var real=new System.Reflection.AssemblyName(value); Version=real.Version; Name=real.Name; FullName=real.FullName; }
        internal static AssemblyName GetAssemblyName(string path) {
            return new AssemblyName(System.IO.Path.GetFileNameWithoutExtension(path)+", Version="+File.Blobs[path].Version+", Culture=neutral, PublicKeyToken=null");
        }
    }
    internal sealed class Result {
        internal bool machine_safe; internal int? office_bitness; internal string status,reason,installed_version;
        internal Result(string operation) { status="unknown"; }
    }
    internal static class TestHost { internal static bool Admin=true,Closed=true; }
    internal sealed class WindowsIdentity:System.IDisposable {
        internal static WindowsIdentity GetCurrent() { return new WindowsIdentity(); }
        public void Dispose() { }
    }
    internal sealed class WindowsPrincipal {
        internal WindowsPrincipal(WindowsIdentity identity) { }
        internal bool IsInRole(WindowsBuiltInRole role) { return TestHost.Admin; }
    }
    internal static class Environment {
        internal enum SpecialFolder { ProgramFiles,CommonApplicationData }
        internal static string GetFolderPath(SpecialFolder folder) { return @"C:\Synthetic"; }
    }
    internal sealed class Process:System.IDisposable {
        internal string ProcessName="POWERPNT";
        internal static Process[] GetProcesses() { return TestHost.Closed?new Process[0]:new[]{new Process()}; }
        public void Dispose() { }
    }
    internal sealed class FileInfo { internal long Length=1; internal FileInfo(string path) { } }
    internal static class Reservation { internal static bool Valid(Dictionary<string,object> data,string id) { return false; } }
    internal static class Contract
    {
        internal static string Version="1.0.4.0",PayloadDirectory=@"C:\Synthetic\Payload";
        internal const string Dll="AegisDesk.PptReader.dll",ProgId="Synthetic.Reader",ClassId="{00000000-0000-0000-0000-000000000001}",ReaderClass="Synthetic.Connect";
        internal const string MutexName="Local\\AegisDesk.ProductSetupFixture";
        internal const string EventsDll="AegisDesk.PptReader.Events.dll";
        internal static string[] InstalledFiles { get { return new Version(Version)<=new Version("1.0.4.0")?new[]{Dll}:new[]{Dll,EventsDll}; } }
        internal static string[] Keys={@"Software\Classes\CLSID\Synthetic",@"Software\Classes\Synthetic.Reader",@"Software\Microsoft\Office\PowerPoint\Addins\Synthetic.Reader"};
        internal static Dictionary<string,object> Metadata=new Dictionary<string,object>();
        internal static bool FailRecord; internal static int RecordWrites,JournalCreates;
        internal static bool PayloadValid() { return true; }
        internal static string Hash(string path) { return File.Blobs[path].Hash; }
        internal static Dictionary<string,object> Read(string path) { return (Dictionary<string,object>)Metadata[path]; }
        internal static void AtomicWrite(string path,object value,FileSecurity acl) {
            if(path==Machine.Record) RecordWrites++; if(path==Machine.Journal) JournalCreates++;
            if(FailRecord&&path==Machine.Record) { FailRecord=false; throw new System.IO.IOException("synthetic_record_failure"); }
            var json=new JavaScriptSerializer(); Metadata[path]=json.DeserializeObject(json.Serialize(value));
        }
    }
}
