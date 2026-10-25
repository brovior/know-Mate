using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using AegisDesk.PptSetup;
using Microsoft.Win32;
using File=AegisDesk.PptSetup.File;

internal static class ProductSetupFixtures
{
    private static int cases;
    private static void Need(bool ok,string code) { if(!ok) throw new Exception(code); }
    private static void Payload(string version)
    {
        Contract.Version=version;
        foreach(string name in Contract.InstalledFiles)
            File.Blobs[Path.Combine(Contract.PayloadDirectory,name)]=new Blob(version,new string(version=="1.0.4.0"?'a':'b',64));
    }
    private static void ResetCounts()
    {
        RegistryKey.Writes=RegistryKey.Creates=RegistryKey.Deletes=0;
        File.CopyCalls=File.AclCalls=File.Deletes=0;
        Contract.RecordWrites=Contract.JournalCreates=RequestDirectory.Creates=0;
    }
    private static void ZeroWrites()
    {
        Need(RegistryKey.Writes==0 && RegistryKey.Creates==0 && RegistryKey.Deletes==0,"registry_mutated");
        Need(Contract.RecordWrites==0 && Contract.JournalCreates==0 && File.Deletes==0,"record_journal_mutated");
        Need(File.CopyCalls==0 && File.AclCalls==0 && RequestDirectory.Creates==0,"file_acl_mutated");
    }
    private static void Seed(string version)
    {
        RegistryKey.Reset(); Contract.Metadata.Clear(); File.Blobs.Clear(); File.Trusted=true;
        File.FailCopy=File.FailAcl=0; Contract.FailRecord=false; TestHost.Admin=TestHost.Closed=true;
        const string office=@"C:\Synthetic\POWERPNT.EXE";
        byte[] pe=new byte[256]; pe[0]=0x4d; pe[1]=0x5a; pe[0x3c]=128;
        pe[128]=0x50; pe[129]=0x45; pe[132]=0x64; pe[133]=0x86;
        File.Blobs[office]=new Blob("0.0.0.0",""){Bytes=pe};
        using(var root=RegistryKey.OpenBaseKey(RegistryHive.LocalMachine,RegistryView.Registry64))
        using(var key=root.CreateSubKey(@"Software\Microsoft\Windows\CurrentVersion\App Paths\POWERPNT.EXE"))
            key.SetValue("",office,RegistryValueKind.String);
        Payload(version); Mutation.Apply(false,"synthetic",new RequestDirectoryLease());
        if(new Version(version)<=new Version("1.0.4.0")) {
            Contract.Read(Machine.Record)["schema"]=1; Contract.Read(Machine.Record).Remove("files");
        }
        Payload("1.0.5.14"); ResetCounts();
    }
    private static void Journal(string target)
    {
        string helper=Contract.Version; Contract.Version=target;
        using(var root=RegistryKey.OpenBaseKey(RegistryHive.LocalMachine,RegistryView.Registry64))
            typeof(Mutation).GetMethod("Journal",BindingFlags.Static|BindingFlags.NonPublic).Invoke(null,new object[]{root,64});
        Contract.Version=helper;
    }
    private static void Denied(string name,string status)
    {
        ResetCounts(); string registry=RegistryKey.Dump();
        string metadata=new System.Web.Script.Serialization.JavaScriptSerializer().Serialize(Contract.Metadata);
        Result check=Machine.Check("check",true,false,"synthetic");
        Need(check.status==status,"check_status:"+name+":"+check.status+":"+check.reason); ZeroWrites();
        foreach(bool remove in new[]{false,true}) {
            bool denied=false;
            try { Mutation.Apply(remove,"synthetic",new RequestDirectoryLease()); }
            catch(IOException) { denied=true; }
            Need(denied,"apply_allowed:"+name); ZeroWrites();
        }
        Need(registry==RegistryKey.Dump(),"registry_state:"+name);
        Need(metadata==new System.Web.Script.Serialization.JavaScriptSerializer().Serialize(Contract.Metadata),"record_state:"+name);
        cases++; Console.WriteLine("SETUP PASS "+name+" real_Check_real_Apply writes_deletes_journal_create=0");
    }
    public static int Main()
    {
        try {
            foreach(string shape in new[]{"valid","missing","partial","mismatched"}) {
                Seed("1.0.6.0");
                using(var root=RegistryKey.OpenBaseKey(RegistryHive.LocalMachine,RegistryView.Registry64)) {
                    if(shape=="missing") foreach(string key in Contract.Keys) root.DeleteSubKeyTree(key,false);
                    if(shape=="partial") root.DeleteSubKeyTree(Contract.Keys[0],false);
                    if(shape=="mismatched") using(var key=root.CreateSubKey(Contract.Keys[0]+@"\InprocServer32")) key.SetValue("Assembly","Foreign, Version=9.0.0.0",RegistryValueKind.String);
                }
                Denied("newer_"+shape,"conflict");
            }
            Seed("1.0.5.14"); File.Trusted=false; Denied("untrusted_record","conflict");
            Seed("1.0.5.14"); Contract.Read(Machine.Record)["path"]=@"C:\Foreign\Reader.dll"; Denied("inconsistent_identity","conflict");
            Seed("1.0.5.14"); Contract.Read(Machine.Record)["version"]="01.0.5.14"; Denied("noncanonical_version","conflict");
            Seed("1.0.6.0"); Journal("1.0.6.0"); Denied("pending_journal_newer_current","conflict");
            bool directDenied=false; try { Mutation.Recover(); } catch(IOException) { directDenied=true; }
            Need(directDenied,"direct_recover_newer"); ZeroWrites();
            Seed("1.0.6.0"); Journal("1.0.6.0"); File.Delete(Machine.Record); Denied("pending_journal_newer_without_current","busy");
            Seed("1.0.5.14"); Journal("1.0.5.14"); Contract.Read(Machine.Journal)["schema"]=1; Denied("legacy_ambiguous_journal","busy");
            Seed("1.0.5.14"); Journal("1.0.5.14");
            ((Dictionary<string,object>)Contract.Read(Machine.Journal)["record"])["path"]=@"C:\Foreign\Reader.dll";
            Denied("journal_inconsistent_record","busy");
            Seed("1.0.6.0"); Journal("1.0.5.14"); File.Delete(Machine.Record);
            Denied("journal_newer_snapshot_record","busy");
            Seed("1.0.5.14"); Journal("1.0.5.14");
            var snapshots=(Dictionary<string,object>)Contract.Read(Machine.Journal)["keys"];
            var clsid=(Dictionary<string,object>)snapshots[Contract.Keys[0]];
            var children=(Dictionary<string,object>)clsid["children"];
            var server=(Dictionary<string,object>)children["InprocServer32"];
            var values=(Dictionary<string,object>)server["values"];
            ((Dictionary<string,object>)values["Assembly"])["value"]="AegisDesk.PptReader, Version=1.0.6.0, Culture=neutral, PublicKeyToken=null";
            Denied("journal_newer_registry_snapshot","busy");
            foreach(string version in new[]{"1.0.5.14","1.0.4.0"}) {
                Seed(version);
                if(version=="1.0.5.14") using(var root=RegistryKey.OpenBaseKey(RegistryHive.LocalMachine,RegistryView.Registry64))
                    foreach(string key in Contract.Keys) root.DeleteSubKeyTree(key,false);
                ResetCounts(); Result before=Machine.Check("check",true,false,"synthetic");
                Need(before.status==(version=="1.0.5.14"?"repairable":"outdated"),"allowed_preflight"); ZeroWrites();
                Mutation.Apply(false,"synthetic",new RequestDirectoryLease());
                Need(Machine.Check("check",true,false,"synthetic").status=="ready","allowed_apply");
                Need(Contract.JournalCreates==1 && Contract.RecordWrites==1,"allowed_transaction");
                cases++; Console.WriteLine("SETUP PASS "+(version=="1.0.5.14"?"same_version_repair":"older_upgrade"));
            }
            for(int boundary=1;boundary<=18;boundary++) {
                Seed("1.0.4.0"); string before=RegistryKey.Dump();
                if(boundary<=17) RegistryKey.FailAt=boundary; else Contract.FailRecord=true;
                bool failed=false;
                try { Mutation.Apply(false,"synthetic",new RequestDirectoryLease()); } catch(IOException) { failed=true; }
                Need(failed,"fault_not_reached:"+boundary);
                Need(RegistryKey.Dump()==before,"registry_not_restored:"+boundary);
                Need((string)Contract.Read(Machine.Record)["version"]=="1.0.4.0","record_not_restored");
                Need(!File.Exists(Machine.Journal),"journal_not_consumed"); cases++;
            }
            Console.WriteLine("SETUP PASS rollback_registry_record_boundaries=18");
            foreach(bool acl in new[]{false,true}) foreach(int boundary in new[]{1,2}) {
                Seed("1.0.4.0"); string before=RegistryKey.Dump();
                if(acl) File.FailAcl=boundary; else File.FailCopy=boundary;
                bool failed=false;
                try { Mutation.Apply(false,"synthetic",new RequestDirectoryLease()); } catch(IOException) { failed=true; }
                Need(failed && RegistryKey.Dump()==before,"copy_acl_restore");
                Need((string)Contract.Read(Machine.Record)["version"]=="1.0.4.0" && !File.Exists(Machine.Journal),"copy_acl_record");
                Mutation.Apply(false,"synthetic",new RequestDirectoryLease());
                Need(Machine.Check("check",true,false,"synthetic").status=="ready","copy_acl_retry"); cases++;
            }
            Console.WriteLine("SETUP PASS rollback_copy_acl_boundaries=4 retry_ready");
            Console.WriteLine("SETUP_RESULT PASS arch="+IntPtr.Size*8+" cases="+cases+" actual_Machine_Check_Apply_Recovery memory_IO_only"); return 0;
        } catch(Exception error) { Console.WriteLine("SETUP_RESULT FAIL "+error); return 1; }
    }
}
