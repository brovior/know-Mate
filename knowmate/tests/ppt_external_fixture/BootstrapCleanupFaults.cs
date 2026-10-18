#if AEGIS_BOOTSTRAP_CLEANUP_TEST
using System;
// Fixture-only callbacks. This type and its call sites disappear without the test define.
internal static class BootstrapCleanupFaults {
 internal static Action AfterTimerStop=()=>{};
 internal static Action AfterTimerDispose=()=>{};
 internal static Action AfterGuardDisconnect=()=>{};
}
#endif
