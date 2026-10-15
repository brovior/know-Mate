// Office·등록 없이 실제 CCW의 IDTExtensibility2 vtable을 호출한다.
using System;
using System.Reflection;
using System.Runtime.InteropServices;

[ComVisible(true), ClassInterface(ClassInterfaceType.AutoDispatch)]
public sealed class ProbeHost { }

[ComVisible(true), ClassInterface(ClassInterfaceType.AutoDispatch)]
public sealed class ProbeAddin
{
    private object value;
    public bool Reject { get; set; }
    public object Object
    {
        get { return value; }
        set
        {
            if (Reject) throw new COMException("SECRET_HOST_ERROR", unchecked((int)0x80004005));
            this.value = value;
        }
    }
}

public static class ConnectionHarness
{
    [DllImport("oleaut32.dll")]
    private static extern IntPtr SafeArrayCreateVector(ushort kind, int lowerBound, uint count);
    [DllImport("oleaut32.dll")]
    private static extern int SafeArrayPutElement(IntPtr array, ref int index, IntPtr value);
    [DllImport("oleaut32.dll")]
    private static extern int SafeArrayGetElement(IntPtr array, ref int index, IntPtr value);
    [DllImport("oleaut32.dll")]
    private static extern int SafeArrayDestroy(IntPtr array);
    [DllImport("oleaut32.dll")]
    private static extern int VariantClear(IntPtr variant);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate int Connection(IntPtr self, IntPtr app, int mode, IntPtr addin, ref IntPtr custom);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate int Disconnection(IntPtr self, int mode, ref IntPtr custom);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate int Notification(IntPtr self, ref IntPtr custom);

    private static Delegate Slot(IntPtr pointer, int number, Type signature)
    {
        IntPtr table = Marshal.ReadIntPtr(pointer);
        return Marshal.GetDelegateForFunctionPointer(Marshal.ReadIntPtr(table, number * IntPtr.Size), signature);
    }

    private static void ArrayResult(int result, string stage)
    {
        // 의도적인 COM 실패의 IErrorInfo가 남을 수 있으므로 배열 API의 HRESULT만 사용한다.
        if (result < 0)
            throw new COMException(stage + "_hr_0x" + unchecked((uint)result).ToString("X8"), result);
    }

    private static void CustomValue(IntPtr array, bool write, string stage)
    {
        if (array == IntPtr.Zero) throw new Exception(stage + "_array_null");
        IntPtr variant = Marshal.AllocCoTaskMem(32); // x86·x64 VARIANT 크기 이상.
        for (int offset = 0; offset < 32; offset += 8) Marshal.WriteInt64(variant, offset, 0);
        try
        {
            int index = 0;
            if (write)
            {
                Marshal.GetNativeVariantForObject(42, variant);
                ArrayResult(SafeArrayPutElement(array, ref index, variant), stage + "_put");
            }
            else
            {
                ArrayResult(SafeArrayGetElement(array, ref index, variant), stage + "_get");
                if (!Object.Equals(Marshal.GetObjectForNativeVariant(variant), 42))
                    throw new Exception("native_custom_array_changed");
            }
        }
        finally { VariantClear(variant); Marshal.FreeCoTaskMem(variant); }
    }

    private static void Disconnected(Type type, object instance)
    {
        try
        {
            type.GetMethod("ProtocolVersion").Invoke(instance, null);
            throw new Exception("disconnected_host_accepted");
        }
        catch (TargetInvocationException exc)
        {
            if (!(exc.InnerException is COMException)) throw;
        }
    }

    private static int Protocol(Type type) { return type.Namespace == "AegisDesk.PptReader" ? 3 : 2; }

    private static void ManagedLifecycle(Type type)
    {
        object instance = Activator.CreateInstance(type);
        var addin = new ProbeAddin();
        var connect = type.GetMethod("OnConnection");
        object[] args = { new ProbeHost(), 1, addin, null };
        connect.Invoke(instance, args);
        if (!Object.ReferenceEquals(addin.Object, instance)
            || (int)type.GetMethod("ProtocolVersion").Invoke(instance, null) != Protocol(type))
            throw new Exception("managed_connection_failed");
        type.GetMethod("OnDisconnection").Invoke(instance, new object[] { 1, null });
        Disconnected(type, instance);
        connect.Invoke(instance, args);
        type.GetMethod("OnBeginShutdown").Invoke(instance, new object[] { null });
        Disconnected(type, instance);
        addin.Reject = true;
        try
        {
            connect.Invoke(instance, args);
            throw new Exception("managed_failure_accepted");
        }
        catch (TargetInvocationException exc)
        {
            if (!(exc.InnerException is COMException)
                || exc.InnerException.Message != "probe_connect_failed"
                || unchecked((uint)Marshal.GetHRForException(exc.InnerException)) != 0x80004005)
                throw new Exception("managed_failure_contract_changed");
        }
        Disconnected(type, instance);
    }

    private static void ManagedArrayErrors()
    {
        // 앞선 콜백 오류가 남아도 성공 코드와 배열 API의 실제 실패 코드를 혼동하지 않는다.
        Marshal.GetHRForException(new COMException("SECRET_PREVIOUS_ERROR", unchecked((int)0x80004005)));
        ArrayResult(0, "array_check");
        try
        {
            ArrayResult(unchecked((int)0x8002000B), "array_check");
            throw new Exception("array_failure_accepted");
        }
        catch (COMException exc)
        {
            if (exc.ErrorCode != unchecked((int)0x8002000B)
                || exc.Message != "array_check_hr_0x8002000B")
                throw new Exception("array_failure_error_info_mixed");
        }
    }

    [STAThread]
    public static int Main(string[] args)
    {
        var assembly = Assembly.LoadFrom(args[0]);
        string wrapper = Array.IndexOf(args, "--production") >= 0 ? "AegisDesk.PptReader" : "AegisDesk.PptProbe";
        var type = assembly.GetType(wrapper + ".Connect", true);
        var contract = assembly.GetType(wrapper + ".IDTExtensibility2", true);
        var kind = (InterfaceTypeAttribute)Attribute.GetCustomAttribute(contract, typeof(InterfaceTypeAttribute));
        // 먼저 규약을 확인해 잘못된 dispinterface의 범위 밖 슬롯을 호출하지 않는다.
        if (kind == null || kind.Value != ComInterfaceType.InterfaceIsDual
            || contract.GUID != new Guid("B65AD801-ABAF-11D0-BB8B-00A0C90F2744"))
            throw new Exception("office_dual_contract_mismatch");

        ManagedLifecycle(type);
        ManagedArrayErrors();
        // Linux의 보조 실행은 Dual 메타데이터·관리형 콜백만 확인한다.
        // SAFEARRAY 마샬링과 네이티브 COM 검증은 Windows 경로에서만 판정한다.
        if (Array.IndexOf(args, "--managed-only") >= 0)
        {
            Console.WriteLine("PASS dual metadata/managed callbacks; native COM and Office not verified");
            return 0;
        }
        if (Environment.OSVersion.Platform != PlatformID.Win32NT)
            throw new PlatformNotSupportedException("native_com_requires_windows");
        foreach (var method in contract.GetMethods())
        {
            var parameters = method.GetParameters();
            var customParameter = parameters[parameters.Length - 1];
            var marshal = (MarshalAsAttribute)Attribute.GetCustomAttribute(customParameter, typeof(MarshalAsAttribute));
            if (customParameter.ParameterType != typeof(Array).MakeByRefType()
                || !customParameter.IsIn || !customParameter.IsOut
                || marshal == null || marshal.Value != UnmanagedType.SafeArray
                || marshal.SafeArraySubType != VarEnum.VT_VARIANT)
                throw new Exception("office_custom_array_contract_mismatch");
        }

        object instance = Activator.CreateInstance(type);
        var host = new ProbeHost();
        var addin = new ProbeAddin();
        IntPtr pointer = IntPtr.Zero, appPointer = IntPtr.Zero, addinPointer = IntPtr.Zero;
        IntPtr custom = IntPtr.Zero;
        try
        {
            pointer = Marshal.GetComInterfaceForObject(instance, contract);
            appPointer = Marshal.GetIDispatchForObject(host);
            addinPointer = Marshal.GetIDispatchForObject(addin);
            var connect = (Connection)Slot(pointer, 7, typeof(Connection));
            var disconnect = (Disconnection)Slot(pointer, 8, typeof(Disconnection));
            var update = (Notification)Slot(pointer, 9, typeof(Notification));
            var startup = (Notification)Slot(pointer, 10, typeof(Notification));
            var shutdown = (Notification)Slot(pointer, 11, typeof(Notification));
            custom = SafeArrayCreateVector((ushort)VarEnum.VT_VARIANT, 0, 1);
            if (custom == IntPtr.Zero) throw new Exception("native_custom_array_allocation_failed");
            CustomValue(custom, true, "setup");
            Disconnected(type, instance);
            if (connect(pointer, appPointer, 1, addinPointer, ref custom) != 0
                || !Object.ReferenceEquals(addin.Object, instance)
                || (int)type.GetMethod("ProtocolVersion").Invoke(instance, null) != Protocol(type))
                throw new Exception("native_connection_failed");
            CustomValue(custom, false, "after_connect");
            if (update(pointer, ref custom) != 0) throw new Exception("native_update_failed");
            CustomValue(custom, false, "after_update");
            if (startup(pointer, ref custom) != 0) throw new Exception("native_startup_failed");
            CustomValue(custom, false, "after_startup");
            if (disconnect(pointer, 1, ref custom) != 0) throw new Exception("native_disconnect_failed");
            CustomValue(custom, false, "after_disconnect");
            Disconnected(type, instance);
            if (connect(pointer, appPointer, 1, addinPointer, ref custom) != 0)
                throw new Exception("native_reconnect_failed");
            CustomValue(custom, false, "after_reconnect");
            if (shutdown(pointer, ref custom) != 0) throw new Exception("native_shutdown_failed");
            CustomValue(custom, false, "after_shutdown");
            Disconnected(type, instance);

            // 실패한 콜백의 출력 배열을 정상 결과로 검증하지 않는다. 성공 시 왕복은 위에서 확인한다.
            ArrayResult(SafeArrayDestroy(custom), "success_array_destroy");
            custom = IntPtr.Zero;

            // 실패는 HRESULT로 반환되고 부분 연결 상태나 원래 오류 문구를 남기지 않는다.
            addin.Reject = true;
            int error = connect(pointer, appPointer, 1, addinPointer, ref custom);
            if (error != unchecked((int)0x80004005)) throw new Exception("native_failure_hresult_changed");
            var exception = Marshal.GetExceptionForHR(error);
            if (exception == null || exception.Message.Contains("SECRET"))
                throw new Exception("native_failure_message_leaked");
            Disconnected(type, instance);
            if (connect(pointer, IntPtr.Zero, 1, addinPointer, ref custom) >= 0)
                throw new Exception("native_missing_host_accepted");
            Disconnected(type, instance);
        }
        finally
        {
            if (custom != IntPtr.Zero) SafeArrayDestroy(custom);
            if (addinPointer != IntPtr.Zero) Marshal.Release(addinPointer);
            if (appPointer != IntPtr.Zero) Marshal.Release(appPointer);
            if (pointer != IntPtr.Zero) Marshal.Release(pointer);
            GC.KeepAlive(addin);
            GC.KeepAlive(host);
            GC.KeepAlive(instance);
        }
        Console.WriteLine("PASS native COM connect/disconnect/startup/shutdown/failure contracts; Office not verified");
        return 0;
    }
}
