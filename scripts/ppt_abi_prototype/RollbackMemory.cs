// TEST ONLY: in-memory dependencies for compiling the unchanged PptSetupMutation.cs.
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
        internal static int Writes,FailAt;
        private RegistryKey(Node node) { this.node=node; }
        internal static void Reset() { root=new Node(); Writes=FailAt=0; }
        internal static string Dump() { return Dump(root); }
        private static string Dump(Node n) {
            return "["+String.Join(";",n.Values.OrderBy(x=>x.Key).Select(x=>x.Key+":"+x.Value.Item2+":"+new JavaScriptSerializer().Serialize(x.Value.Item1)))+"]{"+
                String.Join(";",n.Children.OrderBy(x=>x.Key).Select(x=>x.Key+":"+Dump(x.Value)))+"}";
        }
        public static RegistryKey OpenBaseKey(RegistryHive hive,RegistryView view) { return new RegistryKey(root); }
        public RegistryKey OpenSubKey(string path) { return Find(path,false); }
        public RegistryKey CreateSubKey(string path) { return Find(path,true); }
        private RegistryKey Find(string path,bool create) {
            Node current=node;
            foreach(string part in path.Split('\\')) { Node child; if(!current.Children.TryGetValue(part,out child)) {
                if(!create) return null; child=new Node(); current.Children.Add(part,child);
            } current=child; }
            return new RegistryKey(current);
        }
        public void DeleteSubKeyTree(string path,bool missing) {
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
    internal sealed class Blob { internal string Version,Hash; internal Blob(string version,string hash) { Version=version; Hash=hash; } }
    internal static class File
    {
        internal static int CopyCalls,AclCalls,FailCopy,FailAcl;
        internal static readonly Dictionary<string,Blob> Blobs=new Dictionary<string,Blob>();
        internal static bool Exists(string path) { return Blobs.ContainsKey(path)||Contract.Metadata.ContainsKey(path); }
        internal static void Delete(string path) { Blobs.Remove(path); Contract.Metadata.Remove(path); }
        internal static void Copy(string source,string target,bool overwrite) {
            CopyCalls++; if(CopyCalls==FailCopy) { FailCopy=0; throw new System.IO.IOException("synthetic_copy_failure"); }
            if(Blobs.ContainsKey(target)&&!overwrite) throw new System.IO.IOException("exists"); Blobs[target]=Blobs[source];
        }
        internal static FileSecurity GetAccessControl(string path) {
            var acl=new FileSecurity(); acl.SetOwner(new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid,null)); return acl;
        }
        internal static void SetAccessControl(string path,FileSecurity acl) {
            AclCalls++; if(AclCalls==FailAcl) { FailAcl=0; throw new System.IO.IOException("synthetic_acl_failure"); }
        }
    }
    internal static class Directory
    {
        internal static bool Exists(string path) { return true; }
        internal static DirectorySecurity GetAccessControl(string path) {
            var acl=new DirectorySecurity(); acl.SetOwner(new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid,null)); return acl;
        }
    }
    internal static class RequestDirectory { internal static void Create(string path,DirectorySecurity acl) { } }
    internal sealed class RequestDirectoryLease { internal int Checks; internal void Recheck() { Checks++; } }
    internal sealed class AssemblyName
    {
        internal Version Version; internal string FullName,Name;
        internal static AssemblyName GetAssemblyName(string path) {
            string version=File.Blobs[path].Version; return new AssemblyName { Name=System.IO.Path.GetFileNameWithoutExtension(path),Version=new Version(version),FullName="Synthetic, Version="+version };
        }
    }
    internal sealed class Result { internal bool machine_safe=true; internal int? office_bitness=64; internal string status; }
    internal static class Machine
    {
        internal static string Root=@"C:\Synthetic\PptReader",Record=Root+@"\installation.json",Journal=Root+@"\journal.json";
        internal static bool Admin=true,Closed=true,Reservations=true;
        internal static void NoLinks(string path) { }
        internal static bool OfficeClosed() { return Closed; }
        internal static bool ReservationsSafe(string own) { return Reservations; }
        internal static Microsoft.Win32.RegistryView View(int bitness) { return Microsoft.Win32.RegistryView.Registry64; }
        internal static string InstalledDll(string version) { return Root+"\\"+version+"\\"+Contract.Dll; }
        // Environment/version preflight model only; Mutation itself is unchanged product code.
        internal static Result Check(string operation,bool held,bool includeUser,string own) {
            var result=new Result();
            if(!Contract.Metadata.ContainsKey(Record)) result.status="missing";
            else {
                string version=(string)Contract.Read(Record)["version"];
                result.status=version==Contract.Version?"ready":new Version(version)<new Version(Contract.Version)?"outdated":"conflict";
            }
            return result;
        }
    }
    internal static class Contract
    {
        internal static string Version="1.0.4.0",PayloadDirectory=@"C:\Synthetic\Payload";
        internal const string Dll="AegisDesk.PptReader.dll",ProgId="Synthetic.Reader",ClassId="{00000000-0000-0000-0000-000000000001}",ReaderClass="Synthetic.Connect";
        internal const string EventsDll="AegisDesk.PptReader.Events.dll";
        internal static string[] InstalledFiles { get { return new Version(Version)<=new Version("1.0.4.0")?new[]{Dll}:new[]{Dll,EventsDll}; } }
        internal static string[] Keys={@"Software\Classes\CLSID\Synthetic",@"Software\Classes\Synthetic.Reader",@"Software\Microsoft\Office\PowerPoint\Addins\Synthetic.Reader"};
        internal static Dictionary<string,object> Metadata=new Dictionary<string,object>();
        internal static bool FailRecord;
        internal static bool PayloadValid() { return true; }
        internal static string Hash(string path) { return File.Blobs[path].Hash; }
        internal static Dictionary<string,object> Read(string path) { return (Dictionary<string,object>)Metadata[path]; }
        internal static void AtomicWrite(string path,object value,FileSecurity acl) {
            if(FailRecord&&path==Machine.Record) { FailRecord=false; throw new System.IO.IOException("synthetic_record_failure"); }
            var json=new JavaScriptSerializer(); Metadata[path]=json.DeserializeObject(json.Serialize(value));
        }
    }
}
