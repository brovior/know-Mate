using System;
using System.IO;
using System.Security.AccessControl;

namespace AegisDesk.PptSetup
{
    /// <summary>Writer trust for setup roots; never changes directory security.</summary>
    internal static class InstallRootPolicy
    {
        private const string TrustedInstaller = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464";

        internal static bool TrustedWriter(string directory, string readerRoot, string sid,
            InheritanceFlags inheritance, PropagationFlags propagation, FileSystemRights rights)
        {
            if (sid == "S-1-5-32-544" || sid == "S-1-5-18") return true;
            // Servicing may own an inherited, folder-only ACE on the outer container.
            return sid == TrustedInstaller && !String.IsNullOrEmpty(directory) && !String.IsNullOrEmpty(readerRoot) &&
                String.Equals(directory, Path.GetDirectoryName(readerRoot), StringComparison.OrdinalIgnoreCase) &&
                inheritance == InheritanceFlags.None && propagation == PropagationFlags.None &&
                (rights & ~FileSystemRights.FullControl) == 0;
        }
    }
}
