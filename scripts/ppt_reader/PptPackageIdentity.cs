using System;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Runtime.Remoting;
using System.Security.Cryptography;

namespace AegisDesk.PptReader
{
    /// <summary>Read only package metadata in a fresh reflection-only load context.</summary>
    [ComVisible(false)]
    public sealed class PptPackageMetadata : ObjectHandle
    {
        /// <summary>No package code is executed; the caller has verified these disk bytes.</summary>
        public PptPackageMetadata(byte[] bytes) : base(Read(bytes)) { }

        private static Guid Read(byte[] bytes)
        {
            if (AppDomain.CurrentDomain.ReflectionOnlyGetAssemblies().Length != 0)
                throw new IOException("ppt_metadata_context_not_empty");
            return Assembly.ReflectionOnlyLoad(bytes).ManifestModule.ModuleVersionId;
        }
    }

    internal static class PptPackageIdentity
    {
        internal static Guid DiskMvid(string path, string expectedHash)
        {
            using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
            {
                if (stream.Length > 32 * 1024 * 1024) throw new IOException("ppt_event_payload_size");
                byte[] bytes;
                using (var reader = new BinaryReader(stream, System.Text.Encoding.UTF8, true)) bytes = reader.ReadBytes(checked((int)stream.Length));
                using (var sha = SHA256.Create())
                    if (BitConverter.ToString(sha.ComputeHash(bytes)).Replace("-", "").ToLowerInvariant() != expectedHash)
                        throw new IOException("ppt_event_payload_changed");
                AppDomain domain = AppDomain.CreateDomain("PptPackageMetadata-" + Guid.NewGuid().ToString("N"));
                try
                {
                    // Cross domains through a BCL base type, not the LoadFrom helper type.
                    // The short object[] overload means activationAttributes, not constructor args.
                    var result = (ObjectHandle)domain.CreateInstanceFromAndUnwrap(
                        typeof(PptPackageMetadata).Assembly.Location, typeof(PptPackageMetadata).FullName,
                        false, BindingFlags.Public | BindingFlags.Instance, null,
                        new object[] { bytes }, null, null);
                    return (Guid)result.Unwrap();
                }
                finally { AppDomain.Unload(domain); }
            }
        }
        internal static void RequireLoaded(Assembly loaded, Guid packageMvid)
        {
            if (loaded.ManifestModule.ModuleVersionId != packageMvid)
                throw new IOException("ppt_event_loaded_mvid_mismatch");
        }
    }
}
