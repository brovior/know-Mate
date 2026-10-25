using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using Microsoft.Win32;

namespace AegisDesk.PptSetup
{
    internal static partial class Mutation
    {
        internal static bool RecordIdentity(Dictionary<string, object> data)
        {
            try {
                Version parsed;
                if (!Version.TryParse((string)data["version"], out parsed) || parsed.ToString() != (string)data["version"] || parsed.Revision < 0) return false;
                bool legacy = parsed <= new Version("1.0.4.0");
                if (!(data["schema"] is int) || (int)data["schema"] != (legacy ? 1 : 2) ||
                    !(data["bitness"] is int) || ((int)data["bitness"] != 32 && (int)data["bitness"] != 64)) return false;
                if (!(data["sha256"] is string) || !System.Text.RegularExpressions.Regex.IsMatch((string)data["sha256"], "\\A[0-9a-f]{64}\\z")) return false;
                if (!legacy) {
                    var files = data["files"] as Dictionary<string, object>;
                    if (files == null || files.Count != Contract.InstalledFiles.Length) return false;
                    foreach (string name in Contract.InstalledFiles)
                        if (!files.ContainsKey(name) || !(files[name] is string) ||
                            !System.Text.RegularExpressions.Regex.IsMatch((string)files[name], "\\A[0-9a-f]{64}\\z")) return false;
                    if (!Object.Equals(data["sha256"], files[Contract.Dll])) return false;
                }
                return (string)data["prog_id"] == Contract.ProgId && (string)data["class_id"] == Contract.ClassId &&
                    (string)data["scope"] == "Machine" && String.Equals((string)data["path"],
                    Machine.InstalledDll((string)data["version"]), StringComparison.OrdinalIgnoreCase);
            } catch { return false; }
        }
        public static bool Owned()
        {
            try {
                Machine.NoLinks(Machine.Record);
                return Protected(Machine.Record) && RecordIdentity(Contract.Read(Machine.Record));
            } catch { return false; }
        }
        // Also called before Recover: a stale helper must not restore/delete a newer record.
        internal static Dictionary<string, object> VersionBoundary()
        {
            Machine.NoLinks(Machine.Record);
            if (!File.Exists(Machine.Record)) {
                if (Directory.Exists(Machine.Record)) throw new IOException("installation_unknown");
                return null;
            }
            if (!Protected(Machine.Record)) throw new IOException("installation_unknown");
            var record = Contract.Read(Machine.Record);
            if (!RecordIdentity(record)) throw new IOException("installation_unknown");
            if (new Version((string)record["version"]) > new Version(Contract.Version)) throw new IOException("downgrade_blocked");
            return record;
        }
        private static Dictionary<string, object> ReadRecoveryJournal()
        {
            try {
            var data = Contract.Read(Machine.Journal);
            if (!(data["schema"] is int) || (int)data["schema"] != 2 || (string)data["prog_id"] != Contract.ProgId ||
                (string)data["target_version"] != Contract.Version) throw new IOException("journal_unresolved");
            if (!(data["bitness"] is int)) throw new IOException("journal_unresolved");
            int bitness = (int)data["bitness"];
            if (bitness != 32 && bitness != 64) throw new IOException("journal_unresolved");
            var snapshots = (Dictionary<string, object>)data["keys"];
            if (snapshots.Count != Contract.Keys.Length) throw new IOException("journal_unresolved");
            var previous = data["record"] as Dictionary<string, object>;
            if (data["record"] != null && (previous == null || !RecordIdentity(previous) ||
                new Version((string)previous["version"]) > new Version(Contract.Version) || (int)previous["bitness"] != bitness))
                throw new IOException("journal_unresolved");
            foreach (string key in Contract.Keys) {
                if (!snapshots.ContainsKey(key) || (previous == null && snapshots[key] != null)) throw new IOException("journal_unresolved");
                ValidateSnapshot(snapshots[key], previous, 0);
            }
                return data;
            } catch (Exception) { throw new IOException("journal_unresolved"); }
        }
        private static void ValidateSnapshot(object snapshot, Dictionary<string, object> previous, int depth)
        {
            if (snapshot == null) return;
            if (previous == null || depth > 5) throw new IOException("journal_unresolved");
            var data = snapshot as Dictionary<string, object>;
            if (data == null || data.Count != 2) throw new IOException("journal_unresolved");
            var values = data["values"] as Dictionary<string, object>;
            var children = data["children"] as Dictionary<string, object>;
            if (values == null || children == null || values.Count > 32 || children.Count > 32) throw new IOException("journal_unresolved");
            string version = (string)previous["version"];
            foreach (var item in values) {
                var entry = item.Value as Dictionary<string, object>;
                if (entry == null || entry.Count != 2 || !(entry["kind"] is int)) throw new IOException("journal_unresolved");
                var kind = (RegistryValueKind)(int)entry["kind"];
                object value = entry["value"];
                switch (kind) {
                    case RegistryValueKind.String: case RegistryValueKind.ExpandString: if (!(value is string)) throw new IOException("journal_unresolved"); break;
                    case RegistryValueKind.Binary: case RegistryValueKind.None: Convert.FromBase64String((string)value); break;
                    case RegistryValueKind.DWord: if (!(value is int)) throw new IOException("journal_unresolved"); break;
                    case RegistryValueKind.QWord: Int64.Parse((string)value, System.Globalization.CultureInfo.InvariantCulture); break;
                    case RegistryValueKind.MultiString:
                        if (!(value is IEnumerable) || value is string) throw new IOException("journal_unresolved");
                        foreach (object text in (IEnumerable)value) if (!(text is string)) throw new IOException("journal_unresolved");
                        break;
                    default: throw new IOException("journal_unresolved");
                }
                if (item.Key == "Assembly") {
                    var identity = new AssemblyName((string)value);
                    if (identity.Name != Path.GetFileNameWithoutExtension(Contract.Dll) || identity.Version == null ||
                        identity.Version > new Version(version)) throw new IOException("journal_unresolved");
                    version = identity.Version.ToString();
                }
                if (item.Key == "Class" && !Object.Equals(value, Contract.ReaderClass)) throw new IOException("journal_unresolved");
            }
            if (values.ContainsKey("CodeBase"))
            {
                object codeBase = ((Dictionary<string, object>)values["CodeBase"])["value"];
                bool current = Object.Equals(codeBase, RegistrationCodeBase(Machine.InstalledDll(version)));
                // 1.0.0.0의 실제 설치 journal만 역사적 공백 인코딩을 복원한다.
                bool legacy = (string)previous["version"] == "1.0.0.0" && version == "1.0.0.0" &&
                    Object.Equals(codeBase, new Uri(Machine.InstalledDll(version)).AbsoluteUri);
                if (!current && !legacy) throw new IOException("journal_unresolved");
            }
            foreach (var child in children) {
                if (String.IsNullOrEmpty(child.Key) || child.Key.IndexOfAny(new[]{'\\','/'}) >= 0) throw new IOException("journal_unresolved");
                Version childVersion;
                if (Version.TryParse(child.Key, out childVersion) && childVersion > new Version((string)previous["version"])) throw new IOException("journal_unresolved");
                ValidateSnapshot(child.Value, previous, depth + 1);
            }
        }
    }
}
