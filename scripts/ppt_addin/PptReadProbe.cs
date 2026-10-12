// Office 2016용 시험 추가 기능. 파일·로그·클립보드에 추출 내용을 쓰지 않는다.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Threading;
using System.Web.Script.Serialization;

[assembly: AssemblyVersion("0.1.1.0")]
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
    [ComVisible(true), Guid("94D61957-6341-43E3-AD86-D968157BB181")]
    [InterfaceType(ComInterfaceType.InterfaceIsDual)]
    public interface IPptReadProbe
    {
        [DispId(1)] int ProtocolVersion();
        [DispId(2)] string ExtractSlide([MarshalAs(UnmanagedType.IDispatch)] object presentation, int slide);
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
            if (application == null || mainThread != Thread.CurrentThread.ManagedThreadId
                || Thread.CurrentThread.GetApartmentState() != ApartmentState.STA)
                throw new COMException("probe_wrong_thread_or_disconnected");
        }

        /// <summary>시험 스크립트와 맞춰야 하는 응답 버전을 반환한다.</summary>
        public int ProtocolVersion() { RequireHostThread(); return 1; }

        private bool SameApplication(object other)
        {
            IntPtr left = Marshal.GetIUnknownForObject(application);
            IntPtr right = IntPtr.Zero;
            try
            {
                right = Marshal.GetIUnknownForObject(other);
                return left == right;
            }
            finally
            {
                if (right != IntPtr.Zero) Marshal.Release(right);
                Marshal.Release(left);
            }
        }

        /// <summary>이미 열린 같은 Office의 슬라이드를 읽어 JSON을 메모리로 반환한다.</summary>
        public string ExtractSlide(object presentation, int slide)
        {
            RequireHostThread();
            if (reading) throw new COMException("probe_reentrant_request");
            reading = true;
            try
            {
                dynamic prs = presentation;
                if (!SameApplication((object)prs.Application))
                    throw new COMException("probe_wrong_application");
                if (slide < 1 || slide > (int)prs.Slides.Count)
                    throw new COMException("probe_invalid_slide");
                var watch = Stopwatch.StartNew();
                var nodes = new List<Dictionary<string, object>>();
                var counts = new Dictionary<string, int> {
                    {"shapes", 0}, {"groups", 0}, {"tables", 0}, {"cells", 0}, {"errors", 0}
                };
                dynamic shapes = prs.Slides[slide].Shapes;
                foreach (object shape in shapes) ReadShape(shape, nodes, counts);
                watch.Stop();
                var payload = new Dictionary<string, object> {
                    {"schema", 1}, {"slide", slide}, {"nodes", nodes}, {"counts", counts},
                    {"seconds", watch.Elapsed.TotalSeconds}
                };
                var serializer = new JavaScriptSerializer { MaxJsonLength = 32 * 1024 * 1024 };
                return serializer.Serialize(payload);
            }
            catch (Exception exc)
            {
                if (IsFatal(exc)) throw;
                // COM 예외에 셀 내용·경로·JSON을 포함하지 않는다.
                throw new COMException("probe_extract_failed", unchecked((int)0x80004005));
            }
            finally { reading = false; }
        }

        private static bool IsFatal(Exception exc)
        {
            uint code = unchecked((uint)Marshal.GetHRForException(exc));
            return code == 0x800706BA || code == 0x800706BE || code == 0x800706BF
                || code == 0x80010006 || code == 0x80010007 || code == 0x80010012
                || code == 0x80010108 || code == 0x800401FD;
        }

        private static void ReadShape(object item, List<Dictionary<string, object>> nodes,
            Dictionary<string, int> counts)
        {
            counts["shapes"]++;
            dynamic shape = item;
            try
            {
                if ((int)shape.Type == 6)
                {
                    counts["groups"]++;
                    foreach (object child in shape.GroupItems) ReadShape(child, nodes, counts);
                    return;
                }
            }
            catch (Exception exc) { if (IsFatal(exc)) throw; counts["errors"]++; }
            try
            {
                if ((int)shape.HasTable != 0)
                {
                    counts["tables"]++;
                    dynamic table = shape.Table;
                    int rows = (int)table.Rows.Count;
                    int columns = rows == 0 ? 0 : (int)table.Columns.Count;
                    var grid = new List<List<string>>();
                    for (int row = 1; row <= rows; row++)
                    {
                        var cells = new List<string>();
                        for (int column = 1; column <= columns; column++)
                        {
                            counts["cells"]++;
                            cells.Add((string)table.Cell(row, column).Shape.TextFrame.TextRange.Text);
                        }
                        grid.Add(cells);
                    }
                    nodes.Add(new Dictionary<string, object> {{"kind", "table"}, {"cells", grid}});
                    return;
                }
            }
            catch (Exception exc) { if (IsFatal(exc)) throw; counts["errors"]++; }
            try
            {
                if ((int)shape.HasTextFrame != 0)
                {
                    string text = (string)shape.TextFrame.TextRange.Text;
                    nodes.Add(new Dictionary<string, object> {{"kind", "text"}, {"text", text}});
                }
            }
            catch (Exception exc) { if (IsFatal(exc)) throw; counts["errors"]++; }
        }
    }
}
