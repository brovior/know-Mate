// Invoke only the setup's read-only payload validator, never its entry point.
using System;
using System.Reflection;

internal static class ProductPayloadCheck
{
    public static int Main(string[] args)
    {
        try {
            Assembly setup=Assembly.LoadFrom(args[0]);
            bool valid=(bool)setup.GetType("AegisDesk.PptSetup.Contract",true)
                .GetMethod("PayloadValid",BindingFlags.Static|BindingFlags.Public).Invoke(null,null);
            bool expected=args[1]=="valid";
            if(valid!=expected) throw new Exception("payload_admission");
            Console.WriteLine("PAYLOAD_RESULT PASS expected="+expected); return 0;
        } catch(Exception error) { Console.WriteLine("PAYLOAD_RESULT FAIL "+error.GetType().Name); return 1; }
    }
}
