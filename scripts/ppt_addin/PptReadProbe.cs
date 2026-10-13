// Office 2016용 시험 추가 기능. 파일·로그·클립보드에 추출 내용을 쓰지 않는다.
using System;
using System.Collections.Generic;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Threading;

[assembly: AssemblyVersion("0.1.3.0")]
[assembly: ComVisible(false)]

namespace AegisDesk.PptProbe
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

    /// <summary>외부 비교 스크립트에 노출하는 최소 추출 계약.</summary>
    [ComVisible(true), Guid("819985A8-742C-4AF1-90AF-BF7B135968D5")]
    [InterfaceType(ComInterfaceType.InterfaceIsDual)]
    public interface IPptReadProbe
    {
        [DispId(1)] int ProtocolVersion();
        [DispId(2)] string ExtractSlide([MarshalAs(UnmanagedType.BStr)] string presentationPath, int slide);
    }

    /// <summary>PowerPoint 주 STA에서 슬라이드를 읽는 시험 추가 기능.</summary>
    [ComVisible(true), Guid("02BE8BA9-C02B-48AD-ADBB-9B5686D72B1E")]
    [ProgId("AegisDesk.PptReadProbe"), ClassInterface(ClassInterfaceType.None)]
    [ComDefaultInterface(typeof(IPptReadProbe))]
    public sealed class Connect : StandardOleMarshalObject, IDTExtensibility2, IPptReadProbe
    {
        private object application;
        private int mainThread;
        private bool reading;

        /// <summary>Office가 로드할 때 주 STA와 자동화 객체를 연결한다.</summary>
        public void OnConnection(object app, int connectMode, object addin, ref Array custom)
        {
            application = null;
            mainThread = 0;
            if (Thread.CurrentThread.GetApartmentState() != ApartmentState.STA)
                throw new COMException("probe_requires_office_sta");
            if (app == null || addin == null)
                throw new COMException("probe_missing_host");
            application = app;
            mainThread = Thread.CurrentThread.ManagedThreadId;
            try { ((dynamic)addin).Object = this; }
            catch (Exception exc)
            {
                // 연결 실패 뒤 ProtocolVersion이 정상 연결로 오판하지 않게 한다.
                application = null;
                mainThread = 0;
                throw new COMException("probe_connect_failed", Marshal.GetHRForException(exc));
            }
        }

        /// <summary>Office 연결이 끝나면 보유 참조만 해제한다.</summary>
        public void OnDisconnection(int removeMode, ref Array custom) { application = null; mainThread = 0; }
        /// <summary>추가 기능 목록 변경에는 작업하지 않는다.</summary>
        public void OnAddInsUpdate(ref Array custom) { }
        /// <summary>Office 시작 완료에는 문서를 열거나 읽지 않는다.</summary>
        public void OnStartupComplete(ref Array custom) { }
        /// <summary>Office 종료에는 보유 참조만 해제한다.</summary>
        public void OnBeginShutdown(ref Array custom) { application = null; mainThread = 0; }

        private void RequireHostThread()
        {
            // StandardOleMarshalObject가 외부 호출을 원래 apartment로 전달한다.
            if (application == null) throw new COMException("probe_disconnected");
            if (mainThread != Thread.CurrentThread.ManagedThreadId) throw new COMException("probe_wrong_thread");
            if (Thread.CurrentThread.GetApartmentState() != ApartmentState.STA)
                throw new COMException("probe_requires_office_sta");
        }

        /// <summary>시험 스크립트와 맞춰야 하는 응답 버전을 반환한다.</summary>
        public int ProtocolVersion() { RequireHostThread(); return 2; }

        /// <summary>Already opened host document only; shared core preserves trial output.</summary>
        public string ExtractSlide(string presentationPath, int slide)
        {
            RequireHostThread();
            if (reading) throw new COMException("probe_reentrant_request");
            reading = true;
            try { return AegisDesk.PptShared.PptExtractionCore.ExtractSlide(application, presentationPath, slide); }
            finally { reading = false; }
        }

        // Reflection harness contracts remain on the trial wrapper.
        private static void ReadShape(object item, List<Dictionary<string, object>> nodes, Dictionary<string, int> counts)
        { AegisDesk.PptShared.PptExtractionCore.ReadShape(item, nodes, counts); }
        private static void ReadShapeTracked(object item, List<Dictionary<string, object>> nodes,
            Dictionary<string, int> counts, List<Dictionary<string, object>> errors, string position)
        { AegisDesk.PptShared.PptExtractionCore.ReadShapeTracked(item, nodes, counts, errors, position); }
    }
}
