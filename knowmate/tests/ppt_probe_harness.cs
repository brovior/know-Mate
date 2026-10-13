// Office 없이 실제 추가 기능의 내부 순회를 검증하는 합성 객체.
using System;
using System.Collections.Generic;
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
public sealed class BrokenApplication
{
    public object Application { get { throw new COMException("SECRET", unchecked((int)0x80004005)); } }
}
public sealed class ProbePresentation
{
    public object Application { get; set; }
    public ProbeCount Slides { get { return new ProbeCount { Count = 1 }; } }
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
        // 실제 Office 없이 누락된 속성·COM 실패의 분류와 원문 비노출을 확인한다.
        object host = new object();
        type.GetField("application", BindingFlags.NonPublic | BindingFlags.Instance).SetValue(disconnected, host);
        type.GetField("mainThread", BindingFlags.NonPublic | BindingFlags.Instance).SetValue(disconnected, Thread.CurrentThread.ManagedThreadId);
        var extract = type.GetMethod("ExtractSlide");
        foreach (object input in new object[] { new BrokenApplication(), new object(), null })
        {
            try { extract.Invoke(disconnected, new object[] { input, 1 }); throw new Exception("failure_accepted"); }
            catch (TargetInvocationException exc)
            {
                string message = exc.InnerException.Message;
                string expected = input == null ? "probe_missing_presentation" : input is BrokenApplication
                    ? "probe_extract_failed:application_get:com:80004005" : "probe_extract_failed:application_get:binding:";
                if (!message.StartsWith(expected) || message.Contains("SECRET"))
                    throw new Exception("extract_error_classification_failed");
            }
        }
        foreach (object app in new object[] { host, new object() })
        {
            try
            {
                extract.Invoke(disconnected, new object[] { new ProbePresentation { Application = app }, 2 });
                throw new Exception("guard_failure_accepted");
            }
            catch (TargetInvocationException exc)
            {
                string expected = Object.ReferenceEquals(app, host) ? "probe_invalid_slide" : "probe_wrong_application";
                if (exc.InnerException.Message != expected) throw new Exception("guard_error_was_hidden");
            }
        }
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
        Console.WriteLine("PASS fake C# grouping/table/order/fatal/disconnected contracts; Office not verified");
        return 0;
    }
}
