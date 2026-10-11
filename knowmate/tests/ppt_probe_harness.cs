// Office 없이 실제 추가 기능의 내부 순회를 검증하는 합성 객체.
using System;
using System.Collections.Generic;
using System.Reflection;
using System.Runtime.InteropServices;

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

public static class Harness
{
    public static ProbeShape Text(string value)
    {
        return new ProbeShape { Type = 1, HasTextFrame = -1,
            TextFrame = new ProbeFrame { TextRange = new ProbeRange { Text = value } } };
    }

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
        Console.WriteLine("PASS fake C# grouping/table/order/fatal/disconnected contracts; Office not verified");
        return 0;
    }
}
