using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Threading;
using Microsoft.Win32;

namespace AegisDesk.PptSetup
{
    internal sealed class MachineLock : IDisposable
    {
        private Mutex mutex;
        public MachineLock()
        {
            var acl = new MutexSecurity();
            acl.AddAccessRule(new MutexAccessRule(new SecurityIdentifier(WellKnownSidType.BuiltinUsersSid, null),
                MutexRights.Synchronize | MutexRights.Modify, AccessControlType.Allow));
            acl.AddAccessRule(new MutexAccessRule(new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null),
                MutexRights.FullControl, AccessControlType.Allow));
            acl.AddAccessRule(new MutexAccessRule(new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null),
                MutexRights.FullControl, AccessControlType.Allow));
            bool created;
            try { mutex = Mutex.OpenExisting(Contract.MutexName, MutexRights.Synchronize | MutexRights.Modify); }
            catch (WaitHandleCannotBeOpenedException)
            {
                try { mutex = new Mutex(false, Contract.MutexName, out created, acl); }
                catch (UnauthorizedAccessException)
                { mutex = Mutex.OpenExisting(Contract.MutexName, MutexRights.Synchronize | MutexRights.Modify); }
            }
            try
            {
                try { if (!mutex.WaitOne(0)) throw new IOException("mutex_busy"); }
                catch (AbandonedMutexException) { /* caller must inspect journal/reservations */ }
            }
            catch { mutex.Dispose(); mutex = null; throw; }
        }
        public void Dispose() { if (mutex != null) { mutex.ReleaseMutex(); mutex.Dispose(); mutex = null; } }
    }

    internal static class Machine
    {
        public static string Requests { get { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "AegisDesk", "PptReaderRequests"); } }
        public static string Root { get { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "AegisDesk", "PptReader"); } }
        public static string Journal { get { return Path.Combine(Root, "journal.json"); } }
        public static string Record { get { return Path.Combine(Root, "installation.json"); } }
        public static string InstalledDll(string version) { return Path.Combine(Root, version, Contract.Dll); }
        public static bool Admin { get { using (var identity = WindowsIdentity.GetCurrent()) return new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator); } }

        public static void NoLinks(string path)
        {
            for (string current = Path.GetFullPath(path); current != null; current = Path.GetDirectoryName(current))
                if ((File.Exists(current) || Directory.Exists(current)) && (File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
                    throw new IOException("path_reparse");
        }
        public static bool ReservationsSafe(string own = null)
        {
            NoLinks(Requests);
            if (!Directory.Exists(Requests)) return true;
            foreach (string path in Directory.GetFileSystemEntries(Requests))
            {
                NoLinks(path);
                if (!File.Exists(path) || Path.GetExtension(path) != ".json") return false;
                if (new FileInfo(path).Length > 8192) return false;
                var data = Contract.Read(path);
                if (!Reservation.Valid(data, Path.GetFileNameWithoutExtension(path))) return false;
                if ((string)data["state"] == "complete") continue;
                if (own != null && Path.GetFileNameWithoutExtension(path) == own) continue;
                // Never expire by age or by the disappearance of the helper.
                // Orphan/UAC/child identity remains unresolved until recovery.
                return false;
            }
            return true;
        }
        private static bool MetadataTemporaryFiles()
        {
            if (!Directory.Exists(Root)) return false;
            foreach (string pattern in new[] { "journal.json.*.tmp", "installation.json.*.tmp" })
                if (Directory.GetFileSystemEntries(Root, pattern).Length != 0) return true;
            return false;
        }
        public static bool OfficeClosed()
        {
            foreach (Process process in Process.GetProcesses())
                using (process) { if (String.Equals(process.ProcessName, "POWERPNT", StringComparison.OrdinalIgnoreCase)) return false; }
            return true;
        }
        public static int OfficeBitness()
        {
            var candidates = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (RegistryView view in new[] { RegistryView.Registry32, RegistryView.Registry64 })
                using (RegistryKey root = RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, view))
                using (RegistryKey key = root.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\App Paths\POWERPNT.EXE"))
                {
                    string value = key == null ? null : key.GetValue("") as string;
                    if (value != null) candidates.Add(Path.GetFullPath(value.Trim('"')));
                }
            if (candidates.Count != 1) throw new IOException("office_unknown");
            foreach (string path in candidates)
                return ReadPeBitness(path);
            throw new IOException("office_unknown");
        }
        public static int ReadPeBitness(string path)
        {
            NoLinks(path);
            using (var reader = new BinaryReader(File.OpenRead(path)))
            {
                if (reader.ReadUInt16() != 0x5a4d) throw new IOException("office_unknown");
                reader.BaseStream.Position = 0x3c;
                int offset = reader.ReadInt32();
                if (offset < 64 || offset > reader.BaseStream.Length - 24) throw new IOException("office_unknown");
                reader.BaseStream.Position = offset;
                if (reader.ReadUInt32() != 0x4550) throw new IOException("office_unknown");
                ushort machine = reader.ReadUInt16();
                if (machine == 0x14c) return 32;
                if (machine == 0x8664) return 64;
            }
            throw new IOException("office_unknown");
        }
        public static RegistryView View(int bitness) { return bitness == 32 ? RegistryView.Registry32 : RegistryView.Registry64; }
        public static Result Check(string operation, bool held = false, bool includeUser = true, string own = null)
        {
            var result = new Result(operation);
            MachineLock gate = null;
            try
            {
                if (!held) gate = new MachineLock();
                NoLinks(Root);
                if (!ReservationsSafe(own)) { result.status = "busy"; result.reason = "reservation_unresolved"; return result; }
                var ownedRecord = Mutation.VersionBoundary();
                if (ownedRecord != null) result.installed_version = (string)ownedRecord["version"];
                if (File.Exists(Journal) || MetadataTemporaryFiles()) { result.status = "busy"; result.reason = "journal_unresolved"; return result; }
                result.machine_safe = true;
                result.office_bitness = OfficeBitness();
                using (RegistryKey machine = RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, View(result.office_bitness.Value)))
                using (RegistryKey user = RegistryKey.OpenBaseKey(RegistryHive.CurrentUser, View(result.office_bitness.Value)))
                {
                    if (includeUser)
                        foreach (string path in Contract.Keys)
                            using (RegistryKey shadow = user.OpenSubKey(path))
                                if (shadow != null) { result.status = "conflict"; result.reason = "hkcu_shadow"; return result; }
                    int keys = 0;
                    foreach (string path in Contract.Keys) using (RegistryKey key = machine.OpenSubKey(path)) if (key != null) keys++;
                    if (keys == 0 && ownedRecord == null) { result.status = "missing"; result.reason = "not_installed"; return result; }
                    if (!Mutation.Owned()) { result.status = "conflict"; result.reason = "registration_foreign"; return result; }
                    var installed = Contract.Read(Record);
                    if (Convert.ToInt32(installed["bitness"]) != result.office_bitness.Value)
                    { result.status = "conflict"; result.reason = "registration_foreign"; return result; }
                    result.installed_version = (string)installed["version"];
                    using (RegistryKey addin = machine.OpenSubKey(Contract.Keys[2]))
                        if (addin != null && !Object.Equals(addin.GetValue("LoadBehavior"), 3))
                        { result.status = "disabled"; result.reason = "disabled"; return result; }
                    if (keys != 3 || !Mutation.RegistrationValid(machine, result.installed_version))
                    { result.status = "repairable"; result.reason = "registration_missing"; return result; }
                    Version installedVersion;
                    if (!Version.TryParse(result.installed_version, out installedVersion)) throw new IOException("registration_version");
                    if (installedVersion < new Version(Contract.Version)) { result.status = "outdated"; result.reason = "version_old"; return result; }
                    if (result.installed_version != Contract.Version) { result.status = "conflict"; result.reason = "registration_foreign"; return result; }
                    result.status = "ready"; result.reason = "registration_ok"; return result;
                }
            }
            catch (IOException ex) { if (ex.Message == "downgrade_blocked" || ex.Message == "installation_unknown") { result.status = "conflict"; result.reason = ex.Message; return result; } result.reason = ex.Message == "mutex_busy" ? "mutex_busy" : "office_unknown"; result.status = result.reason == "mutex_busy" ? "busy" : "unknown"; if (result.reason == "mutex_busy") result.machine_safe = false; return result; }
            catch { result.status = "unknown"; result.reason = "check_failed"; result.machine_safe = false; return result; }
            finally { if (gate != null) gate.Dispose(); }
        }
    }
}
