// Office 없이 실제 추가 기능의 내부 순회를 검증하는 합성 객체.
using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Threading;

public sealed class ProbeShape
{
    public int Type { get; set; }
    public int HasTable { get; set; }
    public int HasTextFrame { get; set; }
    public ProbeTable Table { get; set; }
    public List<ProbeShape> GroupItems { get; set; }
    public ProbeFrame TextFrame { get; set; }
}
public sealed class ProbeFrame { public ProbeRange TextRange { get; set; } }
public sealed class ProbeRange { public string Text { get; set; } }
public sealed class ProbeCount { public int Count { get; set; } }
public sealed class ProbeCell { public ProbeShape Shape { get; set; } }
public sealed class ProbeTable
{
    private readonly string[][] values;
    public ProbeCount Rows { get; private set; }
    public ProbeCount Columns { get; private set; }
    public ProbeTable(string[][] grid)
    {
        values = grid;
        Rows = new ProbeCount { Count = grid.Length };
        Columns = new ProbeCount { Count = grid.Length == 0 ? 0 : grid[0].Length };
    }
    public ProbeCell Cell(int row, int column)
    {
        return new ProbeCell { Shape = Harness.Text(values[row - 1][column - 1]) };
    }
}
public sealed class BrokenShape
{
    public int Type { get { throw new COMException("SECRET", unchecked((int)0x80010108)); } }
}
public sealed class BrokenHost
{
    public object Presentations { get { throw new COMException("SECRET", unchecked((int)0x80004005)); } }
}
public sealed class ProbePresentation
{
    public string FullName { get; set; }
    public ProbeSlides Slides { get { return new ProbeSlides(); } }
}
public sealed class ProbeSlides
{
    public int Count { get { return 1; } }
    public ProbeSlide this[int index] { get { return new ProbeSlide(); } }
}
public sealed class ProbeSlide
{
    public List<ProbeShape> Shapes { get { return new List<ProbeShape> { Harness.Text("SECRET_HOST") }; } }
}
public sealed class ProbeDocuments
{
    public List<ProbePresentation> Items = new List<ProbePresentation>();
    public int Count { get { return Items.Count; } }
    public ProbePresentation this[int index] { get { return Items[index - 1]; } }
}
public sealed class ProbeHost
{
    public ProbeDocuments Presentations = new ProbeDocuments();
}
public sealed class BrokenTextShape
{
    public int Type { get { return 1; } }
    public int HasTable { get { return 0; } }
    public int HasTextFrame { get { return -1; } }
    public object TextFrame { get { throw new COMException("SECRET", unchecked((int)0x80004005)); } }
}
public sealed class BrokenTableShape
{
    public int Type { get { return 19; } }
    public int HasTable { get { return -1; } }
    public int HasTextFrame { get { return 0; } }
    public BrokenTable Table { get { return new BrokenTable(); } }
}
public sealed class BrokenTable
{
    public ProbeCount Rows { get { return new ProbeCount { Count = 2 }; } }
    public ProbeCount Columns { get { return new ProbeCount { Count = 2 }; } }
    public object Cell(int row, int column) { throw new COMException("SECRET", unchecked((int)0x80004005)); }
}

public static class Harness
{
    public static ProbeShape Text(string value)
    {
        return new ProbeShape { Type = 1, HasTextFrame = -1,
            TextFrame = new ProbeFrame { TextRange = new ProbeRange { Text = value } } };
    }

    [STAThread]
    public static int Main(string[] args)
    {
        var assembly = Assembly.LoadFrom(args[0]);
        var type = assembly.GetType("AegisDesk.PptProbe.Connect", true);
        var read = type.GetMethod("ReadShape", BindingFlags.NonPublic | BindingFlags.Static);
        var counts = new Dictionary<string, int> {
            {"shapes", 0}, {"groups", 0}, {"tables", 0}, {"cells", 0}, {"errors", 0}
        };
        var nodes = new List<Dictionary<string, object>>();
        var group = new ProbeShape { Type = 6, GroupItems = new List<ProbeShape> {
            Text("SECRET_FIRST"), new ProbeShape { Type = 6, GroupItems = new List<ProbeShape> {
                new ProbeShape { Type = 19, HasTable = -1, Table = new ProbeTable(new[] {
                    new[] { "SECRET_CELL\r\nLINE", "" }, new[] { "", "" }
                }) }, Text("SECRET_LAST")
            } }
        } };
        read.Invoke(null, new object[] { group, nodes, counts });
        if (counts["shapes"] != 5 || counts["groups"] != 2 || counts["tables"] != 1
            || counts["cells"] != 4 || counts["errors"] != 0 || nodes.Count != 3)
            throw new Exception("fake_counts_failed");
        if ((string)nodes[0]["text"] != "SECRET_FIRST" || (string)nodes[2]["text"] != "SECRET_LAST")
            throw new Exception("fake_order_failed");
        var grid = (List<List<string>>)nodes[1]["cells"];
        if (grid[0][0] != "SECRET_CELL\r\nLINE" || grid[0][1] != "" || grid[1][0] != "")
            throw new Exception("fake_cells_failed");
        try
        {
            read.Invoke(null, new object[] { new BrokenShape(), nodes, counts });
            throw new Exception("fake_fatal_not_propagated");
        }
        catch (TargetInvocationException exc)
        {
            if (unchecked((uint)Marshal.GetHRForException(exc.InnerException)) != 0x80010108)
                throw new Exception("fake_fatal_changed");
        }
        var disconnected = Activator.CreateInstance(type);
        try
        {
            type.GetMethod("ProtocolVersion").Invoke(disconnected, null);
            throw new Exception("fake_disconnected_host_accepted");
        }
        catch (TargetInvocationException exc)
        {
            if (!(exc.InnerException is COMException)) throw;
        }
        // 콜백 호스트의 문서만 읽고 외부 COM 객체를 전달받지 않는 계약을 검증한다.
        var hostField = type.GetField("application", BindingFlags.NonPublic | BindingFlags.Instance);
        string path = Path.Combine(Path.GetTempPath(), "probe-host-only.ppt");
        type.GetField("mainThread", BindingFlags.NonPublic | BindingFlags.Instance).SetValue(disconnected, Thread.CurrentThread.ManagedThreadId);
        var extract = type.GetMethod("ExtractSlide");
        if (extract.GetParameters()[0].ParameterType != typeof(string)) throw new Exception("path_contract_required");
        foreach (object host in new object[] { new BrokenHost(), new object() })
        {
            hostField.SetValue(disconnected, host);
            try { extract.Invoke(disconnected, new object[] { path, 1 }); throw new Exception("failure_accepted"); }
            catch (TargetInvocationException exc)
            {
                string message = exc.InnerException.Message;
                string expected = host is BrokenHost ? "probe_extract_failed:host_presentations:com:80004005"
                    : "probe_extract_failed:host_presentations:binding:";
                if (!message.StartsWith(expected) || message.Contains("SECRET"))
                    throw new Exception("extract_error_classification_failed");
            }
        }
        var goodHost = new ProbeHost();
        hostField.SetValue(disconnected, goodHost);
        // 다른 인스턴스에만 열린 문서는 이 호스트의 읽기 대상이 아니다.
        var foreignDocument = new ProbePresentation { FullName = path };
        ExpectGuard(extract, disconnected, path, 1, "probe_document_not_open_in_host");
        goodHost.Presentations.Items.Add(new ProbePresentation { FullName = "unsaved.ppt" });
        goodHost.Presentations.Items.Add(new ProbePresentation { FullName = path });
        string reply = (string)extract.Invoke(disconnected, new object[] { path, 1 });
        if (!reply.Contains("SECRET_HOST") || !reply.Contains("\"errors\":0"))
            throw new Exception("host_read_failed");
        string normalizedReply = (string)extract.Invoke(disconnected, new object[] {
            Path.Combine(Path.GetTempPath(), "probe-subfolder", "..", "probe-host-only.ppt"), 1 });
        if (!normalizedReply.Contains("SECRET_HOST")) throw new Exception("normalized_path_failed");
        ExpectGuard(extract, disconnected, path, 2, "probe_invalid_slide");
        ExpectGuard(extract, disconnected, null, 1, "probe_invalid_path");
        ExpectGuard(extract, disconnected, "relative.ppt", 1, "probe_invalid_path");
        ExpectGuard(extract, disconnected, "C:relative.ppt", 1, "probe_invalid_path");
        goodHost.Presentations.Items.Add(foreignDocument);
        ExpectGuard(extract, disconnected, path, 1, "probe_ambiguous_host_document");
        goodHost.Presentations.Items.Clear();
        goodHost.Presentations.Items.Add(null);
        ExpectGuard(extract, disconnected, path, 1, "probe_missing_host_document");
        var tracked = type.GetMethod("ReadShapeTracked", BindingFlags.NonPublic | BindingFlags.Static);
        var errors = new List<Dictionary<string, object>>();
        counts["errors"] = 0;
        tracked.Invoke(null, new object[] { new BrokenTableShape(), nodes, counts, errors, "2.1" });
        if ((string)errors[0]["stage"] != "cell_get" || (int)errors[0]["row"] != 1
            || (int)errors[0]["column"] != 1 || (string)errors[0]["shape"] != "2.1"
            || (uint)errors[0]["hresult"] != 0x80004005) throw new Exception("cell_position_failed");
        for (int index = 0; index < 20; index++)
            tracked.Invoke(null, new object[] { new BrokenTextShape(), nodes, counts, errors, "3" });
        if (counts["errors"] != 21 || errors.Count != 16 || (string)errors[1]["stage"] != "text_frame"
            || (string)errors[1]["kind"] != "com") throw new Exception("bounded_errors_failed");
        Console.WriteLine("PASS fake C# host-document/guards/grouping/table/order/fatal/disconnected contracts; Office not verified");
        return 0;
    }

    private static void ExpectGuard(MethodInfo extract, object instance, string path, int slide, string expected)
    {
        try
        {
            extract.Invoke(instance, new object[] { path, slide });
            throw new Exception("guard_failure_accepted");
        }
        catch (TargetInvocationException exc)
        {
            if (exc.InnerException.Message != expected) throw new Exception("guard_error_was_hidden");
        }
    }
}
