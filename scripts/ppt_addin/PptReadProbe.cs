// Office 2016용 시험 추가 기능. 파일·로그·클립보드에 추출 내용을 쓰지 않는다.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Threading;
using System.Web.Script.Serialization;

[assembly: AssemblyVersion("0.1.2.0")]
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
            if (application == null) throw new COMException("probe_disconnected");
            if (mainThread != Thread.CurrentThread.ManagedThreadId) throw new COMException("probe_wrong_thread");
            if (Thread.CurrentThread.GetApartmentState() != ApartmentState.STA)
                throw new COMException("probe_requires_office_sta");
        }

        /// <summary>시험 스크립트와 맞춰야 하는 응답 버전을 반환한다.</summary>
        public int ProtocolVersion() { RequireHostThread(); return 1; }

        private bool SameApplication(object other, ref string stage)
        {
            stage = "host_identity";
            IntPtr left = Marshal.GetIUnknownForObject(application);
            IntPtr right = IntPtr.Zero;
            try
            {
                stage = "document_identity";
                right = Marshal.GetIUnknownForObject(other);
                stage = "application_compare";
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
            string stage = "application_get";
            try
            {
                if (presentation == null) throw new COMException("probe_missing_presentation");
                dynamic prs = presentation;
                object documentApplication = (object)prs.Application;
                if (!SameApplication(documentApplication, ref stage))
                    throw new COMException("probe_wrong_application");
                stage = "slide_count";
                if (slide < 1 || slide > (int)prs.Slides.Count)
                    throw new COMException("probe_invalid_slide");
                var watch = Stopwatch.StartNew();
                var nodes = new List<Dictionary<string, object>>();
                var counts = new Dictionary<string, int> {
                    {"shapes", 0}, {"groups", 0}, {"tables", 0}, {"cells", 0}, {"errors", 0}
                };
                var errors = new List<Dictionary<string, object>>();
                stage = "slide_get";
                dynamic selectedSlide = prs.Slides[slide];
                stage = "shapes_get";
                dynamic shapes = selectedSlide.Shapes;
                stage = "shape_iter";
                int position = 0;
                foreach (object shape in shapes)
                {
                    position++;
                    stage = "shape_read";
                    ReadShapeTracked(shape, nodes, counts, errors, position.ToString());
                    stage = "shape_iter";
                }
                watch.Stop();
                var payload = new Dictionary<string, object> {
                    {"schema", 1}, {"slide", slide}, {"nodes", nodes}, {"counts", counts},
                    {"seconds", watch.Elapsed.TotalSeconds}, {"read_errors", errors},
                    {"errors_omitted", Math.Max(0, counts["errors"] - errors.Count)}
                };
                var serializer = new JavaScriptSerializer { MaxJsonLength = 32 * 1024 * 1024 };
                stage = "serialize";
                return serializer.Serialize(payload);
            }
            catch (Exception exc)
            {
                if (IsFatal(exc)) throw;
                // 의도적인 거부 사유를 공통 오류로 덮지 않는다.
                if (exc is COMException && (exc.Message == "probe_wrong_application"
                    || exc.Message == "probe_invalid_slide" || exc.Message == "probe_missing_presentation")) throw;
                // COM 예외에 셀 내용·경로·JSON을 포함하지 않는다.
                // 고정 단계와 숫자 코드만 반환하고 예외 본문은 외부에 노출하지 않는다.
                string code = unchecked((uint)Marshal.GetHRForException(exc)).ToString("X8");
                throw new COMException("probe_extract_failed:" + stage + ":" + ErrorKind(exc) + ":" + code,
                    unchecked((int)0x80004005));
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
            ReadShapeTracked(item, nodes, counts, new List<Dictionary<string, object>>(), "1");
        }

        private static string ErrorKind(Exception exc)
        {
            if (exc is COMException) return "com";
            if (exc is Microsoft.CSharp.RuntimeBinder.RuntimeBinderException) return "binding";
            if (exc is ArgumentException) return "argument";
            if (exc is NullReferenceException) return "null_reference";
            if (exc is InvalidOperationException) return "invalid_operation";
            return "other";
        }

        private static void RecordError(Exception exc, string stage, string shape, int row, int column,
            Dictionary<string, int> counts, List<Dictionary<string, object>> errors)
        {
            counts["errors"]++;
            // 진단 객체도 오류 수에 비례해 무제한 누적하지 않는다.
            if (errors.Count >= 16) return;
            errors.Add(new Dictionary<string, object> {
                {"stage", stage}, {"kind", ErrorKind(exc)}, {"shape", shape},
                {"row", row}, {"column", column},
                {"hresult", unchecked((uint)Marshal.GetHRForException(exc))}
            });
        }

        private static void ReadShapeTracked(object item, List<Dictionary<string, object>> nodes,
            Dictionary<string, int> counts, List<Dictionary<string, object>> errors, string position)
        {
            counts["shapes"]++;
            dynamic shape = item;
            string stage = "shape_type";
            try
            {
                if ((int)shape.Type == 6)
                {
                    counts["groups"]++;
                    stage = "group_items";
                    dynamic children = shape.GroupItems;
                    stage = "group_iter";
                    int childIndex = 0;
                    foreach (object child in children)
                    {
                        childIndex++;
                        ReadShapeTracked(child, nodes, counts, errors, position + "." + childIndex);
                    }
                    return;
                }
            }
            catch (Exception exc) { if (IsFatal(exc)) throw; RecordError(exc, stage, position, 0, 0, counts, errors); }
            int activeRow = 0, activeColumn = 0;
            stage = "has_table";
            try
            {
                if ((int)shape.HasTable != 0)
                {
                    counts["tables"]++;
                    stage = "table_get";
                    dynamic table = shape.Table;
                    stage = "table_rows";
                    int rows = (int)table.Rows.Count;
                    stage = "table_columns";
                    int columns = rows == 0 ? 0 : (int)table.Columns.Count;
                    var grid = new List<List<string>>();
                    for (int row = 1; row <= rows; row++)
                    {
                        var cells = new List<string>();
                        for (int column = 1; column <= columns; column++)
                        {
                            activeRow = row; activeColumn = column;
                            counts["cells"]++;
                            stage = "cell_get";
                            dynamic cell = table.Cell(row, column);
                            stage = "cell_shape";
                            dynamic cellShape = cell.Shape;
                            stage = "cell_frame";
                            dynamic frame = cellShape.TextFrame;
                            stage = "cell_range";
                            dynamic range = frame.TextRange;
                            stage = "cell_text";
                            cells.Add((string)range.Text);
                        }
                        grid.Add(cells);
                    }
                    nodes.Add(new Dictionary<string, object> {{"kind", "table"}, {"cells", grid}});
                    return;
                }
            }
            catch (Exception exc) { if (IsFatal(exc)) throw; RecordError(exc, stage, position, activeRow, activeColumn, counts, errors); }
            stage = "has_text_frame";
            try
            {
                if ((int)shape.HasTextFrame != 0)
                {
                    stage = "text_frame";
                    dynamic frame = shape.TextFrame;
                    stage = "text_range";
                    dynamic range = frame.TextRange;
                    stage = "text_read";
                    string text = (string)range.Text;
                    nodes.Add(new Dictionary<string, object> {{"kind", "text"}, {"text", text}});
                }
            }
            catch (Exception exc) { if (IsFatal(exc)) throw; RecordError(exc, stage, position, 0, 0, counts, errors); }
        }
    }
}
