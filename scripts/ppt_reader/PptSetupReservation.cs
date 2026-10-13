using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;

namespace AegisDesk.PptSetup
{
    internal sealed class Reservation
    {
        public int schema = 1;
        public string request_id, required_version = Contract.Version, helper_sid;
        public int helper_pid;
        public string helper_creation_ticks;
        public int? child_pid;
        public string child_creation_ticks;
        public string state = "reserved";
        public string Path { get { return System.IO.Path.Combine(Machine.Requests, request_id + ".json"); } }
        public static bool Valid(Dictionary<string, object> data, string filename)
        {
            try
            {
                var expected = new HashSet<string> { "schema", "request_id", "required_version", "helper_sid", "helper_pid",
                    "helper_creation_ticks", "child_pid", "child_creation_ticks", "state" };
                if (data.Count != expected.Count) return false;
                foreach (string key in data.Keys) if (!expected.Contains(key)) return false;
                Guid uuid;
                long creation;
                if (!(data["schema"] is int) || (int)data["schema"] != 1 || !Guid.TryParseExact((string)data["request_id"], "D", out uuid) ||
                    uuid.ToString("D") != filename || !System.Text.RegularExpressions.Regex.IsMatch((string)data["required_version"], @"^\d+\.\d+\.\d+\.\d+$") ||
                    !(data["helper_pid"] is int) || (int)data["helper_pid"] <= 0 ||
                    !Int64.TryParse((string)data["helper_creation_ticks"], out creation) || creation <= 0) return false;
                new SecurityIdentifier((string)data["helper_sid"]);
                string state = (string)data["state"];
                if (state != "reserved" && state != "elevated" && state != "unresolved" && state != "complete") return false;
                if (data["child_pid"] == null) return data["child_creation_ticks"] == null && state != "elevated";
                return data["child_pid"] is int && (int)data["child_pid"] > 0 &&
                    Int64.TryParse((string)data["child_creation_ticks"], out creation) && creation > 0;
            }
            catch { return false; }
        }
        private static object Values(Reservation value)
        {
            return new Dictionary<string, object> { { "schema", value.schema }, { "request_id", value.request_id },
                { "required_version", value.required_version }, { "helper_sid", value.helper_sid }, { "helper_pid", value.helper_pid },
                { "helper_creation_ticks", value.helper_creation_ticks }, { "child_pid", value.child_pid },
                { "child_creation_ticks", value.child_creation_ticks }, { "state", value.state } };
        }
        public void Save()
        {
            var acl = new FileSecurity();
            acl.SetAccessRuleProtection(true, false);
            acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(helper_sid), FileSystemRights.FullControl, AccessControlType.Allow));
            foreach (WellKnownSidType sid in new[] { WellKnownSidType.BuiltinAdministratorsSid, WellKnownSidType.LocalSystemSid })
                acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(sid, null), FileSystemRights.FullControl, AccessControlType.Allow));
            acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(WellKnownSidType.BuiltinUsersSid, null), FileSystemRights.Read, AccessControlType.Allow));
            Contract.AtomicWrite(Path, Values(this), acl);
        }
        public static Reservation Create()
        {
            // Prototype candidate: first-user creation/ACL is deliberately gated.
            // Failure here stops before UAC, never requests manual bootstrap.
            Machine.NoLinks(Machine.Requests);
            Directory.CreateDirectory(Machine.Requests);
            var acl = new DirectorySecurity();
            acl.SetAccessRuleProtection(true, false);
            foreach (WellKnownSidType sid in new[] { WellKnownSidType.BuiltinAdministratorsSid, WellKnownSidType.LocalSystemSid })
                acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(sid, null), FileSystemRights.FullControl,
                    InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None, AccessControlType.Allow));
            var users = new SecurityIdentifier(WellKnownSidType.BuiltinUsersSid, null);
            acl.AddAccessRule(new FileSystemAccessRule(users, FileSystemRights.ReadAndExecute | FileSystemRights.CreateFiles,
                InheritanceFlags.None, PropagationFlags.None, AccessControlType.Allow));
            acl.AddAccessRule(new FileSystemAccessRule(users, FileSystemRights.Read,
                InheritanceFlags.ObjectInherit, PropagationFlags.InheritOnly, AccessControlType.Allow));
            acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(WellKnownSidType.CreatorOwnerSid, null), FileSystemRights.FullControl,
                InheritanceFlags.ObjectInherit, PropagationFlags.InheritOnly, AccessControlType.Allow));
            // Never alter an established directory's ACL as another user.
            if (Directory.GetFiles(Machine.Requests).Length == 0) Directory.SetAccessControl(Machine.Requests, acl);
            using (Process current = Process.GetCurrentProcess())
            using (WindowsIdentity identity = WindowsIdentity.GetCurrent())
            {
                var request = new Reservation { request_id = Guid.NewGuid().ToString("D"), helper_sid = identity.User.Value,
                    helper_pid = current.Id, helper_creation_ticks = current.StartTime.ToUniversalTime().Ticks.ToString() };
                request.Save();
                return request;
            }
        }
        public static Reservation Load(string id)
        {
            Guid guid;
            if (!Guid.TryParseExact(id, "D", out guid) || guid.ToString("D") != id) throw new IOException("reservation_identity");
            string path = System.IO.Path.Combine(Machine.Requests, id + ".json");
            Machine.NoLinks(path);
            var data = Contract.Read(path);
            if (!Valid(data, id)) throw new IOException("reservation_identity");
            return new Reservation { request_id = id, helper_sid = (string)data["helper_sid"], helper_pid = (int)data["helper_pid"],
                required_version = (string)data["required_version"], helper_creation_ticks = (string)data["helper_creation_ticks"], child_pid = data["child_pid"] == null ? (int?)null : (int)data["child_pid"],
                child_creation_ticks = data["child_creation_ticks"] as string, state = (string)data["state"] };
        }
        public void EnterChild()
        {
            using (Process helper = Process.GetProcessById(helper_pid))
            using (Process current = Process.GetCurrentProcess())
            {
                if (required_version != Contract.Version || helper.HasExited || helper.StartTime.ToUniversalTime().Ticks.ToString() != helper_creation_ticks || state != "reserved") throw new IOException("reservation_identity");
                child_pid = current.Id; child_creation_ticks = current.StartTime.ToUniversalTime().Ticks.ToString(); state = "elevated"; Save();
            }
        }
        public static bool ProcessGone(int pid, string ticks)
        {
            try
            {
                using (Process process = Process.GetProcessById(pid))
                    return process.StartTime.ToUniversalTime().Ticks.ToString() == ticks && process.HasExited;
            }
            catch (ArgumentException) { return true; }
            catch { return false; } // inaccessible or reused PID is unresolved
        }
        public void Recover()
        {
            if (!Machine.Admin || !Machine.OfficeClosed() || !ProcessGone(helper_pid, helper_creation_ticks) ||
                (child_pid != null && !ProcessGone(child_pid.Value, child_creation_ticks))) throw new IOException("reservation_unresolved");
            // A later UAC child must revalidate state under the same mutex and
            // cannot enter mutation after this explicit recovery completes it.
            state = "complete"; Save();
        }
    }

    internal static class Elevation
    {
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct ExecuteInfo
        {
            public int size; public uint mask; public IntPtr hwnd;
            [MarshalAs(UnmanagedType.LPWStr)] public string verb, file, parameters, directory;
            public int show; public IntPtr instance, idList;
            [MarshalAs(UnmanagedType.LPWStr)] public string className;
            public IntPtr classKey; public uint hotKey; public IntPtr icon, process;
        }
        [DllImport("shell32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool ShellExecuteEx(ref ExecuteInfo info);
        [DllImport("kernel32.dll")] private static extern uint WaitForSingleObject(IntPtr handle, uint ms);
        [DllImport("kernel32.dll")] private static extern bool GetExitCodeProcess(IntPtr process, out uint code);
        [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr handle);

        public static int Run(bool remove, Reservation request)
        {
            var info = new ExecuteInfo { size = Marshal.SizeOf(typeof(ExecuteInfo)), mask = 0x40 | 0x100,
                verb = "runas", file = System.Reflection.Assembly.GetExecutingAssembly().Location,
                parameters = (remove ? "/elevated-remove " : "/elevated-install ") + request.request_id, show = 0 };
            if (!ShellExecuteEx(ref info)) return Marshal.GetLastWin32Error() == 1223 ? 11 : 14;
            if (info.process == IntPtr.Zero) return 14; // request may still exist; never mark complete
            try
            {
                // Intentionally no timeout or kill-on-close; app observation is
                // independent and may end while this helper remains alive.
                if (WaitForSingleObject(info.process, 0xffffffff) != 0) return 14;
                uint code;
                return GetExitCodeProcess(info.process, out code) ? (int)code : 14;
            }
            finally { CloseHandle(info.process); }
        }
    }
}
