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
            return values;
        }
        public void Emit() { Console.WriteLine(Contract.Json.Serialize(Values())); }
    }

    internal static class Contract
    {
        public const string Version = "1.0.3.0";
        public const string ProgId = "AegisDesk.PptReader";
        public const string ClassId = "{287A80D2-804A-46D3-A0E9-CA31C6FC3D09}";
        public const string ReaderIid = "9A20F4A6-6364-4BA7-930B-3E8857BEAF60";
        public const string ReaderClass = "AegisDesk.PptReader.Connect";
        public const string Dll = "AegisDesk.PptReader.dll";
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
                Dictionary<string, object> data = Read(Path.Combine(PayloadDirectory, "manifest.json"));
                if (Convert.ToInt32(data["schema"]) != 1 || (string)data["reader_version"] != Version ||
                    (string)data["auto_install_mode"] != AutoInstallMode ||
                    !(data["auto_install_enabled"] is bool) || (bool)data["auto_install_enabled"] != AutoInstallEnabled ||
                    !(data["auto_install_validated"] is bool) || (bool)data["auto_install_validated"] != AutoInstallValidated ||
                    !(data["auto_shutdown_enabled"] is bool) || (bool)data["auto_shutdown_enabled"] != AutoShutdownEnabled ||
                    !String.Equals((string)data["reader_iid"], ReaderIid, StringComparison.OrdinalIgnoreCase) ||
                    Convert.ToInt32(data["protocol"]) != 3 || Convert.ToInt32(data["payload_schema"]) != 1 ||
                    (string)data["reader_class"] != ReaderClass || (string)data["prog_id"] != ProgId ||
                    !String.Equals((string)data["reader_clsid"], ClassId.Trim('{', '}'), StringComparison.OrdinalIgnoreCase)) return false;
                Dictionary<string, object> files = (Dictionary<string, object>)data["files"];
                foreach (string name in new[] { Dll, "PptReaderSetup.exe" })
                {
                    string path = Path.Combine(PayloadDirectory, name);
                    Machine.NoLinks(path);
                    if (Hash(path) != (string)files[name] || AssemblyName.GetAssemblyName(path).Version.ToString() != Version) return false;
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
            Machine.NoLinks(Path.GetDirectoryName(path));
            try
            {
                using (FileStream stream = new FileStream(temp, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                using (StreamWriter writer = new StreamWriter(stream))
                { writer.Write(Json.Serialize(value)); writer.Flush(); stream.Flush(true); }
            }
            catch (Exception error) { if (request) throw SetupFailure.From("request_create", error); throw; }
            try { if (security != null) File.SetAccessControl(temp, security); }
            catch (Exception error) { if (request) throw SetupFailure.From("request_acl", error); throw; }
            try { if (File.Exists(path)) File.Replace(temp, path, null); else File.Move(temp, path); }
            catch (Exception error) { if (request) throw SetupFailure.From("request_commit", error); throw; }
            // An interrupted write is unresolved evidence. Do not erase the
            // temporary file automatically; /check must keep mutation blocked.
        }
    }
}
