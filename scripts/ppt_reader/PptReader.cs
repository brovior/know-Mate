// 운영 PowerPoint 추가 기능. 파일·로그·클립보드에 추출 내용을 쓰지 않는다.
using System;
using System.Collections.Generic;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Threading;

[assembly: AssemblyVersion("1.0.5.16")]
[assembly: AssemblyFileVersion("1.0.5.16")]
[assembly: ComVisible(false)]

namespace AegisDesk.PptReader
{
    /// <summary>Office 추가 기능의 연결·해제 콜백 계약.</summary>
    [ComImport, ComVisible(true), Guid("B65AD801-ABAF-11D0-BB8B-00A0C90F2744")]
    // Office 연결 계약은 IDispatch와 직접 vtable 호출을 모두 지원한다.
    // 동일 IID를 dispinterface로 선언하면 직접 호출의 ABI가 달라진다.
    [InterfaceType(ComInterfaceType.InterfaceIsDual)]
    public interface IDTExtensibility2
    {
        [DispId(1)] void OnConnection([In, MarshalAs(UnmanagedType.IDispatch)] object application,
            [In] int connectMode, [In, MarshalAs(UnmanagedType.IDispatch)] object addin,
            [In, Out, MarshalAs(UnmanagedType.SafeArray, SafeArraySubType = VarEnum.VT_VARIANT)] ref Array custom);
        [DispId(2)] void OnDisconnection([In] int removeMode,
            [In, Out, MarshalAs(UnmanagedType.SafeArray, SafeArraySubType = VarEnum.VT_VARIANT)] ref Array custom);
        [DispId(3)] void OnAddInsUpdate(
            [In, Out, MarshalAs(UnmanagedType.SafeArray, SafeArraySubType = VarEnum.VT_VARIANT)] ref Array custom);
        [DispId(4)] void OnStartupComplete(
            [In, Out, MarshalAs(UnmanagedType.SafeArray, SafeArraySubType = VarEnum.VT_VARIANT)] ref Array custom);
        [DispId(5)] void OnBeginShutdown(
            [In, Out, MarshalAs(UnmanagedType.SafeArray, SafeArraySubType = VarEnum.VT_VARIANT)] ref Array custom);
    }

    /// <summary>운영 인덱싱에 노출하는 최소 추출 계약.</summary>
    [ComVisible(true), Guid("65152412-DFD3-47EC-8346-3F2675ED0C15")]
    [InterfaceType(ComInterfaceType.InterfaceIsDual)]
    public interface IPptReader
    {
        [DispId(1)] int ProtocolVersion();
        [DispId(2)] string ExtractSlide([MarshalAs(UnmanagedType.BStr)] string presentationPath, int slide);
        [DispId(3)] string ReaderVersion();
    }

    /// <summary>Protocol 3 service; the legacy dual IID above remains unchanged.</summary>
    [ComVisible(true), Guid("9A20F4A6-6364-4BA7-930B-3E8857BEAF60")]
    [InterfaceType(ComInterfaceType.InterfaceIsDual)]
    public interface IPptReaderLifecycle
    {
        [DispId(1)] int ProtocolVersion();
        [DispId(2)] string ExtractSlide([MarshalAs(UnmanagedType.BStr)] string presentationPath, int slide);
        [DispId(3)] string ReaderVersion();
        [DispId(4)] string TryQuitIfIdle();
    }

    /// <summary>PowerPoint 주 STA에서 슬라이드를 읽는 운영 추가 기능.</summary>
    [ComVisible(true), Guid("287A80D2-804A-46D3-A0E9-CA31C6FC3D09")]
    [ProgId("AegisDesk.PptReader"), ClassInterface(ClassInterfaceType.None)]
#if AEGIS_PPT_LIFETIME_DIAGNOSTIC
    [ComDefaultInterface(typeof(IPptReaderLifetimeDiagnostic))]
#elif AEGIS_PPT_EXTERNAL_DIAGNOSTIC
    [ComDefaultInterface(typeof(IPptExternalBootstrap))]
#elif AEGIS_PPT_DEFERRED_DIAGNOSTIC
    [ComDefaultInterface(typeof(IPptReaderDeferredDiagnostic))]
#else
    [ComDefaultInterface(typeof(IPptReaderClientShutdown))]
#endif
    public sealed partial class Connect : StandardOleMarshalObject, IDTExtensibility2, IPptReader, IPptReaderLifecycle
#if AEGIS_PPT_LIFETIME_DIAGNOSTIC
        , IPptReaderLifetimeDiagnostic
#endif
#if AEGIS_PPT_DEFERRED_DIAGNOSTIC
        , IPptReaderDeferredDiagnostic
#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
        , IPptExternalBootstrap
#endif
#endif
    {
        private object application;
        private int mainThread;
        private bool reading;
#if !AEGIS_PPT_LIFETIME_DIAGNOSTIC
        private PptShutdownGuard shutdown;
#endif
#if AEGIS_PPT_DEFERRED_DIAGNOSTIC
        private PptDeferredDiagnostic deferred;
        private int connectionGeneration;
#endif
#if !AEGIS_PPT_LIFETIME_DIAGNOSTIC
        private readonly PptLifecycleTrace lifecycle = new PptLifecycleTrace();
#endif

        /// <summary>Office가 로드할 때 주 STA와 자동화 객체를 연결한다.</summary>
        public void OnConnection(object app, int connectMode, object addin, ref Array custom)
        {
#if AEGIS_PPT_LIFETIME_DIAGNOSTIC
            ConnectLifetimeHost(app, addin);
#elif AEGIS_PPT_EXTERNAL_DIAGNOSTIC

            ObservationFence.Require();
            InvalidateExternalSession();
            observationOwner = ProcessFence.GetCurrentThreadId();
            if (Thread.CurrentThread.GetApartmentState() != ApartmentState.STA)
                throw new COMException("probe_requires_office_sta");
            if (app == null || addin == null)
                throw new COMException("probe_missing_host");
            application = app;
            mainThread = Thread.CurrentThread.ManagedThreadId;
            lifecycle.BindOwner(mainThread);
            shutdown = new PptShutdownGuard(app, mainThread, null, PptSinkLoader.Create, lifecycle);
            try {
#if AEGIS_PPT_DEFERRED_DIAGNOSTIC
                int generation = connectionGeneration;
                if (ShutdownBuildPolicy.Enabled) deferred = PptDeferredDiagnostic.Capture(InvokeExistingShutdown,
                    () => generation == connectionGeneration && application != null && shutdown != null,
                    message => lifecycle.Record(20, 0, message == "callback_entry" ? 1 : message.StartsWith("guard_result_") ? 2 : message.StartsWith("guard_exception_") ? 3 : 0));
#endif
                ((dynamic)addin).Object = this;
            }
            catch (Exception exc)
            {
                // 연결 실패 뒤 ProtocolVersion이 정상 연결로 오판하지 않게 한다.
                DisconnectHost();
                throw new COMException("probe_connect_failed", Marshal.GetHRForException(exc));
            }

#else

#if AEGIS_PPT_DEFERRED_DIAGNOSTIC
            if (deferred != null) deferred.Cancel();
            deferred = null;
            connectionGeneration++;
#endif
            if (shutdown != null) shutdown.Disconnect();
            shutdown = null;
            application = null;
            mainThread = 0;
            if (Thread.CurrentThread.GetApartmentState() != ApartmentState.STA)
                throw new COMException("probe_requires_office_sta");
            if (app == null || addin == null)
                throw new COMException("probe_missing_host");
            application = app;
            mainThread = Thread.CurrentThread.ManagedThreadId;
            lifecycle.BindOwner(mainThread);
            shutdown = new PptShutdownGuard(app, mainThread, null, PptSinkLoader.Create, lifecycle);
            try {
#if AEGIS_PPT_DEFERRED_DIAGNOSTIC
                int generation = connectionGeneration;
                if (ShutdownBuildPolicy.Enabled) deferred = PptDeferredDiagnostic.Capture(InvokeExistingShutdown,
                    () => generation == connectionGeneration && application != null && shutdown != null,
                    message => lifecycle.Record(20, 0, message == "callback_entry" ? 1 : message.StartsWith("guard_result_") ? 2 : message.StartsWith("guard_exception_") ? 3 : 0));
#endif
                ((dynamic)addin).Object = this;
            }
            catch (Exception exc)
            {
                // 연결 실패 뒤 ProtocolVersion이 정상 연결로 오판하지 않게 한다.
                DisconnectHost();
                throw new COMException("probe_connect_failed", Marshal.GetHRForException(exc));
            }

#endif
}

        /// <summary>Office 연결이 끝나면 보유 참조만 해제한다.</summary>
        public void OnDisconnection(int removeMode, ref Array custom)
        {
#if AEGIS_PPT_LIFETIME_DIAGNOSTIC
            DisconnectHost();
#else
#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
            externalTrace.LifecycleBoundary(1,true,removeMode);
#endif
            lifecycle.Record(1, HostFlags(), 0);
            try { DisconnectHost(); } finally {
#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
                externalTrace.LifecycleBoundary(1,false,removeMode);
                ExportLifecycleBoundary();
#endif
            }
#endif
        }
        /// <summary>추가 기능 목록 변경에는 작업하지 않는다.</summary>
        public void OnAddInsUpdate(ref Array custom) { }
        /// <summary>Office 시작 완료에는 문서를 열거나 읽지 않는다.</summary>
        public void OnStartupComplete(ref Array custom) { }
        /// <summary>Office 종료에는 보유 참조만 해제한다.</summary>
        public void OnBeginShutdown(ref Array custom)
        {
#if AEGIS_PPT_LIFETIME_DIAGNOSTIC
            DisconnectHost();
#else
#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
            externalTrace.LifecycleBoundary(2,true,0);
#endif
            lifecycle.Record(2, HostFlags(), 0);
            try { DisconnectHost(); } finally {
#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
                externalTrace.LifecycleBoundary(2,false,0);
                ExportLifecycleBoundary();
#endif
            }
#endif
        }

#if !AEGIS_PPT_LIFETIME_DIAGNOSTIC
        private int HostFlags()
        { return (application != null ? 1 : 0) | (shutdown != null ? 2 : 0); }
#endif

        private void DisconnectHost()
        {
#if AEGIS_PPT_LIFETIME_DIAGNOSTIC
            application = null;
            mainThread = 0;
#elif AEGIS_PPT_EXTERNAL_DIAGNOSTIC

            InvalidateExternalSession();
            lifecycle.Record(3, HostFlags(), 0);

#else

            // Never clear/unadvise an active call's local event guard here.
#if AEGIS_PPT_DEFERRED_DIAGNOSTIC
            if (deferred != null) deferred.Cancel();
            deferred = null;
            connectionGeneration++;
#endif
            if (shutdown != null) shutdown.Disconnect();
            shutdown = null;
            application = null;
            mainThread = 0;
            lifecycle.Record(3, HostFlags(), 0);

#endif
}

        private void RequireHostThread()
        {
#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
            ObservationFence.Require();
#endif
            // StandardOleMarshalObject가 외부 호출을 원래 apartment로 전달한다.
            if (application == null) throw new COMException("probe_disconnected");
            if (mainThread != Thread.CurrentThread.ManagedThreadId) throw new COMException("probe_wrong_thread");
            if (Thread.CurrentThread.GetApartmentState() != ApartmentState.STA)
                throw new COMException("probe_requires_office_sta");
        }

        /// <summary>시험 스크립트와 맞춰야 하는 응답 버전을 반환한다.</summary>
        public int ProtocolVersion() { object scope = EnterDeferredService(); try { RequireHostThread(); return 3; } finally { LeaveDeferredService(scope); } }
        /// <summary>Exact production assembly version after trusted connection.</summary>
        public string ReaderVersion() { object scope = EnterDeferredService(); try { RequireHostThread(); return typeof(Connect).Assembly.GetName().Version.ToString(); } finally { LeaveDeferredService(scope); } }

        /// <summary>Candidate-only request; OS process exit is observed by the caller.</summary>
        public string TryQuitIfIdle()
        {
#if AEGIS_PPT_LIFETIME_DIAGNOSTIC
            return "not_supported";
#else
            object scope = EnterDeferredService();
#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
            externalTrace.ServiceBoundary(true);
#endif
            try { return InvokeExistingShutdown(); }
            finally {
#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
                externalTrace.ServiceBoundary(false);
#endif
                LeaveDeferredService(scope);
            }
#endif
        }

#if !AEGIS_PPT_LIFETIME_DIAGNOSTIC
        private string InvokeExistingShutdown()
        {
#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC
            externalTrace.Start();externalEntryDepth++;bool failed=false;
            try { string result=InvokeExistingShutdownCore();externalTrace.Record(ExternalTrace.Stage.Terminal,result=="quit_requested"?ExternalTrace.Result.Success:ExternalTrace.Result.Refused,TraceReason(result));return result; }
            catch(Exception error){failed=true;externalTrace.Error(error,false);throw;}
            finally {externalEntryDepth--;if(externalEntryDepth==0&&(external==null||externalExportPending)){externalExportPending=false;externalTrace.ExportTerminal(failed);}}
#else
            return InvokeExistingShutdownCore();
#endif
        }
        private string InvokeExistingShutdownCore()
        {
#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC

            if (!ShutdownBuildPolicy.Enabled || shutdown == null) return "not_supported";
            string refusal;ExternalDiagnosticServices owned=null;
            return NewDiagnosticOperation().Run(()=>{
            bool permitted=AllowNormalShutdown(out refusal,out owned);
            if (!permitted) return refusal;
            int completedToken;
            string outcome = shutdown.TryQuitIfIdleTracked(reading, out completedToken);
            // Normal-return path only. No finally flush after fatal/unknown outcome.
            lifecycle.FlushNormalReturn(completedToken);
            return outcome;
            },()=>{if(owned!=null)owned.Release();});

#else

            if (!ShutdownBuildPolicy.Enabled || shutdown == null) return "not_supported";
            int completedToken;
            string outcome = shutdown.TryQuitIfIdleTracked(reading, out completedToken);
            // Normal-return path only. No finally flush after fatal/unknown outcome.
            lifecycle.FlushNormalReturn(completedToken);
            return outcome;

#endif
}
#endif

        /// <summary>Already opened host document only; shared core preserves trial output.</summary>
        public string ExtractSlide(string presentationPath, int slide)
        {
#if AEGIS_PPT_EXTERNAL_DIAGNOSTIC

            object scope = EnterDeferredService();
            try {
            RequireHostThread();
            if (!ReaderFence.AllowsRead()) throw new COMException("external_reader_protected");
            if (reading || (shutdown != null && shutdown.Entered)) throw new COMException("probe_reentrant_request");
            reading = true;
            try { return AegisDesk.PptShared.PptExtractionCore.ExtractSlide(application, presentationPath, slide); }
            finally { reading = false; }
            } finally { LeaveDeferredService(scope); }

#else

            object scope = EnterDeferredService();
            try {
            RequireHostThread();
#if AEGIS_PPT_LIFETIME_DIAGNOSTIC
            if (reading) throw new COMException("probe_reentrant_request");
#else
            if (reading || (shutdown != null && shutdown.Entered)) throw new COMException("probe_reentrant_request");
#endif
            reading = true;
            try { return AegisDesk.PptShared.PptExtractionCore.ExtractSlide(application, presentationPath, slide); }
            finally { reading = false; }
            } finally { LeaveDeferredService(scope); }

#endif
}

        private object EnterDeferredService() {
#if AEGIS_PPT_DEFERRED_DIAGNOSTIC
            PptDeferredDiagnostic current = deferred;
            if (current != null) current.Enter();
            return current;
#else
            return null;
#endif
        }
        private void LeaveDeferredService(object scope) {
#if AEGIS_PPT_DEFERRED_DIAGNOSTIC
            if (scope != null) ((PptDeferredDiagnostic)scope).Leave();
#endif
        }
#if AEGIS_PPT_DEFERRED_DIAGNOSTIC
        /// <summary>Diagnostic-only capability entry; ordinary launches are rejected.</summary>
        public string ArmDeferredQuit(int pid, string creation, string nonce) {
            RequireHostThread();
            if (!ShutdownBuildPolicy.Enabled || deferred == null) return "not_supported";
            return deferred.Arm(pid, creation, nonce);
        }
#endif

        // Shared extraction harness entry points.
        private static void ReadShape(object item, List<Dictionary<string, object>> nodes, Dictionary<string, int> counts)
        { AegisDesk.PptShared.PptExtractionCore.ReadShape(item, nodes, counts); }
        private static void ReadShapeTracked(object item, List<Dictionary<string, object>> nodes,
            Dictionary<string, int> counts, List<Dictionary<string, object>> errors, string position)
        { AegisDesk.PptShared.PptExtractionCore.ReadShapeTracked(item, nodes, counts, errors, position); }
    }
}
