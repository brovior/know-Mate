using System;
using System.Runtime.InteropServices;
namespace AegisDesk.PptReader {
/// <summary>Host UI shutdown contract; both existing dual interfaces retain their ABI.</summary>
[ComVisible(true),Guid("EC5AF9D4-8022-48CD-9D9F-1EF5CC647CC7"),InterfaceType(ComInterfaceType.InterfaceIsIDispatch)]
public interface IPptReaderClientShutdown {
 [DispId(1)] int ProtocolVersion();
 [DispId(2)] string ExtractSlide([MarshalAs(UnmanagedType.BStr)] string path,int slide);
 [DispId(3)] string ReaderVersion();
 [DispId(4)] string TryQuitIfIdle();
 [DispId(5)] string TryQuitIfIdleWithCaller([In,MarshalAs(UnmanagedType.IDispatch)] object caller);
 [DispId(6)] string ConfirmCallerQuit();
 [DispId(7)] string QueryQuitOutcome();
}
public sealed partial class Connect : IPptReaderClientShutdown {
 /// <summary>The former external Quit path is unsafe and cannot be used.</summary>
 public string TryQuitIfIdleWithCaller(object caller) { return "not_supported"; }
 /// <summary>The former callback confirmation cannot grant shutdown authority.</summary>
 public string ConfirmCallerQuit() { return "not_supported"; }
 /// <summary>Observe deferred cancellation without reading or closing documents.</summary>
 public string QueryQuitOutcome() {
#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC || AEGIS_PPT_LIFETIME_DIAGNOSTIC
  return "not_supported";
#else
  RequireHostThread();
  return shutdown==null?"not_supported":shutdown.QueryOutcome();
#endif
 }
}
}
