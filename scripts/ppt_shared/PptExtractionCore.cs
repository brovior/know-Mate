// Shared in-memory extraction; no document open/close/save or logging.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Threading;
using System.Web.Script.Serialization;

namespace AegisDesk.PptShared
{
    internal static class PptExtractionCore
    {
        private static bool IsAbsolutePath(string path)
        {
            if (String.IsNullOrWhiteSpace(path) || !Path.IsPathRooted(path)) return false;
            string root = Path.GetPathRoot(path);
            // Windows 드라이브·루트 상대 경로는 현재 디렉터리에 의존하므로 거부한다.
            return !(root.Length == 2 && root[1] == ':')
                && !(Path.DirectorySeparatorChar == '\\' && root == "\\");
        }

        private static object FindHostPresentation(object application, string path, ref string stage)
        {
            stage = "path_validate";
            if (!IsAbsolutePath(path)) throw new COMException("probe_invalid_path");
            string expected;
            try
            {
                expected = Path.GetFullPath(path);
            }
            catch (ArgumentException) { throw new COMException("probe_invalid_path"); }
            catch (NotSupportedException) { throw new COMException("probe_invalid_path"); }
            stage = "host_presentations";
            dynamic documents = ((dynamic)application).Presentations;
            stage = "host_document_count";
            int count = (int)documents.Count;
            object found = null;
            for (int index = 1; index <= count; index++)
            {
                stage = "host_document_get";
                dynamic candidate = documents[index];
                if (candidate == null) throw new COMException("probe_missing_host_document");
                stage = "host_document_path";
                string fullName = (string)candidate.FullName;
                // 저장 전 문서의 상대 이름을 요청한 파일로 오인하지 않는다.
                if (!IsAbsolutePath(fullName)) continue;
                if (!String.Equals(Path.GetFullPath(fullName), expected, StringComparison.OrdinalIgnoreCase)) continue;
                if (found != null) throw new COMException("probe_ambiguous_host_document");
                found = (object)candidate;
            }
            if (found == null) throw new COMException("probe_document_not_open_in_host");
            return found;
        }

        /// <summary>이미 열린 같은 Office의 슬라이드를 읽어 JSON을 메모리로 반환한다.</summary>
        public static string ExtractSlide(object application, string presentationPath, int slide)
        {
            string stage = "path_validate";
            try
            {
                dynamic prs = FindHostPresentation(application, presentationPath, ref stage);
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
                if (exc is COMException && (exc.Message == "probe_invalid_path"
                    || exc.Message == "probe_invalid_slide" || exc.Message == "probe_missing_host_document"
                    || exc.Message == "probe_ambiguous_host_document" || exc.Message == "probe_document_not_open_in_host")) throw;
                // COM 예외에 셀 내용·경로·JSON을 포함하지 않는다.
                // 고정 단계와 숫자 코드만 반환하고 예외 본문은 외부에 노출하지 않는다.
                string code = unchecked((uint)Marshal.GetHRForException(exc)).ToString("X8");
                throw new COMException("probe_extract_failed:" + stage + ":" + ErrorKind(exc) + ":" + code,
                    unchecked((int)0x80004005));
            }
        }

        private static bool IsFatal(Exception exc)
        {
            uint code = unchecked((uint)Marshal.GetHRForException(exc));
            return code == 0x800706BA || code == 0x800706BE || code == 0x800706BF
                || code == 0x80010006 || code == 0x80010007 || code == 0x80010012
                || code == 0x80010108 || code == 0x800401FD;
        }

        public static void ReadShape(object item, List<Dictionary<string, object>> nodes,
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

        public static void ReadShapeTracked(object item, List<Dictionary<string, object>> nodes,
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
