using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Security.Cryptography;
using System.Web.Script.Serialization;

namespace AegisDesk.PptSetup
{
    internal sealed class Result
    {
        public int schema = 1;
        public string operation, status = "unknown", reason = "check_failed";
        public string required_version = Contract.Version, installed_version;
        public int? office_bitness;
        public string scope = "Machine";
        public bool machine_safe, auto_install_validated = Contract.AutoInstallValidated;
        public string auto_install_mode = Contract.AutoInstallMode;
        public string stage, error_type;
        public string native_stage, ntstatus, io_status;
        public int? winerror;
        public Result(string op) { operation = op; }
        internal Dictionary<string, object> Values()
        {
            var values = new Dictionary<string, object> {
                { "schema", schema }, { "operation", operation }, { "status", status }, { "reason", reason },
                { "required_version", required_version }, { "installed_version", installed_version },
                { "office_bitness", office_bitness }, { "scope", scope }, { "machine_safe", machine_safe },
                { "auto_install_validated", auto_install_validated }, { "auto_install_mode", auto_install_mode }
            };
            if (stage != null) { values["stage"] = stage; values["error_type"] = error_type; values["winerror"] = winerror; }
            if (native_stage != null) { values["native_stage"] = native_stage; values["ntstatus"] = ntstatus; values["io_status"] = io_status; }
            return values;
        }
        public void Emit() { Console.WriteLine(Contract.Json.Serialize(Values())); }
    }

    internal static partial class Contract
    {
        // No implementation in shipping builds: compiler removes every call.
        static partial void TraceAtomic(string stage);
        public const string Version = "1.0.5.0";
        public const string ProgId = "AegisDesk.PptReader";
        public const string ClassId = "{287A80D2-804A-46D3-A0E9-CA31C6FC3D09}";
        public const string ReaderIid = "9A20F4A6-6364-4BA7-930B-3E8857BEAF60";
        public const string ReaderClass = "AegisDesk.PptReader.Connect";
        public const string Dll = "AegisDesk.PptReader.dll";
        public const string EventsDll = "AegisDesk.PptReader.Events.dll";
        public static readonly string[] InstalledFiles = { Dll, EventsDll };
        public static readonly string[] PayloadFiles = { Dll, EventsDll, "PptReaderSetup.exe" };
        public const string MutexName = @"Global\AegisDesk.PptReader.Machine.v1";
        // Shipping automatic preparation requires the actual Windows prototype.
        // This cannot be enabled via the caller's manifest or user config.
        public const string AutoInstallMode = BuildPolicy.AutoInstallMode;
        public const bool AutoShutdownEnabled = AutoInstallMode == "candidate";
        public const bool AutoInstallValidated = AutoInstallMode == "released";
        public const bool AutoInstallEnabled = AutoInstallMode == "candidate" || AutoInstallMode == "released";
        public static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 1048576 };
        public static readonly string[] Keys = {
            @"Software\Classes\CLSID\" + ClassId,
            @"Software\Classes\" + ProgId,
            @"Software\Microsoft\Office\PowerPoint\Addins\" + ProgId
        };
        public static string PayloadDirectory { get { return Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location); } }
        public static string Hash(string path)
        {
            using (SHA256 sha = SHA256.Create())
            using (FileStream stream = File.OpenRead(path))
                return BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
        }
        public static bool PayloadValid()
        {
            try
            {
                string manifest = Path.Combine(PayloadDirectory, "manifest.json");
                Machine.NoLinks(manifest);
                Dictionary<string, object> data = Read(manifest);
                if (!(data["schema"] is int) || (int)data["schema"] != 1 || (string)data["reader_version"] != Version ||
                    (string)data["auto_install_mode"] != AutoInstallMode ||
                    !(data["auto_install_enabled"] is bool) || (bool)data["auto_install_enabled"] != AutoInstallEnabled ||
                    !(data["auto_install_validated"] is bool) || (bool)data["auto_install_validated"] != AutoInstallValidated ||
                    !(data["auto_shutdown_enabled"] is bool) || (bool)data["auto_shutdown_enabled"] != AutoShutdownEnabled ||
                    !String.Equals((string)data["reader_iid"], ReaderIid, StringComparison.OrdinalIgnoreCase) ||
                    !(data["protocol"] is int) || (int)data["protocol"] != 3 || !(data["payload_schema"] is int) || (int)data["payload_schema"] != 2 ||
                    (string)data["reader_class"] != ReaderClass || (string)data["prog_id"] != ProgId ||
                    !String.Equals((string)data["reader_clsid"], ClassId.Trim('{', '}'), StringComparison.OrdinalIgnoreCase)) return false;
                Dictionary<string, object> files = (Dictionary<string, object>)data["files"];
                Dictionary<string, object> versions = (Dictionary<string, object>)data["assembly_versions"];
                if (files.Count != PayloadFiles.Length || versions.Count != PayloadFiles.Length) return false;
                foreach (string name in PayloadFiles)
                {
                    string path = Path.Combine(PayloadDirectory, name);
                    Machine.NoLinks(path);
                    AssemblyName identity = AssemblyName.GetAssemblyName(path);
                    if ((string)versions[name] != Version || Hash(path) != (string)files[name] || identity.Version.ToString() != Version ||
                        identity.Name != Path.GetFileNameWithoutExtension(name)) return false;
                }
                AssemblyName assembly = AssemblyName.GetAssemblyName(Path.Combine(PayloadDirectory, Dll));
                return assembly.Name == "AegisDesk.PptReader";
            }
            catch { return false; }
        }
        public static Dictionary<string, object> Read(string path)
        {
            if (new FileInfo(path).Length > 1048576) throw new InvalidDataException("metadata_size");
            return Json.Deserialize<Dictionary<string, object>>(File.ReadAllText(path));
        }
        public static void AtomicWrite(string path, object value, System.Security.AccessControl.FileSecurity security = null, bool request = false)
        {
            string temp = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
            TraceAtomic("atomic_path_check");
            Machine.NoLinks(Path.GetDirectoryName(path));
            try
            {
                TraceAtomic("atomic_file_open");
                using (FileStream stream = new FileStream(temp, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                {
                    TraceAtomic("atomic_writer_open");
                    using (StreamWriter writer = new StreamWriter(stream))
                    {
                        TraceAtomic("atomic_serialize"); writer.Write(Json.Serialize(value));
                        TraceAtomic("atomic_writer_flush"); writer.Flush();
                        TraceAtomic("atomic_disk_flush"); stream.Flush(true);
                        TraceAtomic("atomic_writer_close");
                    }
                    TraceAtomic("atomic_file_close");
                }
            }
            catch (Exception error) { if (request) throw SetupFailure.From("request_create", error); throw; }
            try { TraceAtomic("atomic_acl_apply"); if (security != null) File.SetAccessControl(temp, security); }
            catch (Exception error) { if (request) throw SetupFailure.From("request_acl", error); throw; }
            try
            {
                TraceAtomic("atomic_commit_probe");
                if (File.Exists(path)) { TraceAtomic("atomic_replace_call"); File.Replace(temp, path, null); }
                else { TraceAtomic("atomic_move_call"); File.Move(temp, path); }
                TraceAtomic("atomic_done");
            }
            catch (Exception error) { if (request) throw SetupFailure.From("request_commit", error); throw; }
            // An interrupted write is unresolved evidence. Do not erase the
            // temporary file automatically; /check must keep mutation blocked.
        }
    }
}
