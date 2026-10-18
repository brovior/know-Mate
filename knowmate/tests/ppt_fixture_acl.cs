using System;
using System.IO;
using System.Security.AccessControl;
using System.Security.Principal;
using AegisDesk.PptSetup;

// Test-owned roots only. Descendants inherit a supported legacy parent ACL,
// independently of pytest's mode-0700 OWNER RIGHTS descriptor.
internal static class PptFixtureAcl
{
    private static DirectorySecurity SupportedAcl()
    {
        var acl = new DirectorySecurity();
        var owner = WindowsIdentity.GetCurrent().User;
        acl.SetOwner(owner);
        acl.SetAccessRuleProtection(true, false);
        foreach (var sid in new[] { owner,
            new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null),
            new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null) })
            acl.AddAccessRule(new FileSystemAccessRule(sid, FileSystemRights.FullControl,
                InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
                PropagationFlags.None, AccessControlType.Allow));
        return acl;
    }

    internal static void Create(string path)
    {
        if (!RequestDirectory.Create(path, SupportedAcl(), true))
            throw new InvalidOperationException("fixture_must_be_new");
    }

    // Caller must record successful creation before readback can fail.
    internal static void Verify(string path, bool failReadback = false)
    {
        if (failReadback) throw new InvalidOperationException("fixture_readback_injected");
        var actual = Directory.GetAccessControl(path);
        if (!actual.AreAccessRulesProtected || !RequestDirectory.Compatible(actual, true))
            throw new InvalidOperationException("fixture_parent_policy");
    }

    internal static void VerifyOwnerRightsRejected()
    {
        var acl = SupportedAcl();
        if (!RequestDirectory.Compatible(acl, true))
            throw new InvalidOperationException("supported_parent_rejected");
        acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier("S-1-3-4"),
            FileSystemRights.FullControl, AccessControlType.Allow));
        if (RequestDirectory.Compatible(acl, true))
            throw new InvalidOperationException("owner_rights_parent_accepted");
    }
}
