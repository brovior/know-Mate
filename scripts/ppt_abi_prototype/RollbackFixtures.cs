using System;
using AegisDesk.PptSetup;
using Microsoft.Win32;

internal static class RollbackFixtures
{
    private static void Check(bool ok,string reason) { if(!ok) throw new Exception(reason); }
    private static void Payload(string version,string hash)
    { Contract.Version=version; foreach(string name in Contract.InstalledFiles) File.Blobs[Contract.PayloadDirectory+"\\"+name]=new Blob(version,hash); }
    private static void Baseline()
    {
        RegistryKey.Reset(); File.Blobs.Clear(); Contract.Metadata.Clear(); Contract.FailRecord=false;
        File.CopyCalls=File.AclCalls=File.FailCopy=File.FailAcl=0;
        Machine.Admin=Machine.Closed=Machine.Reservations=true;
        Payload("1.0.4.0",new string('a',64)); Mutation.Apply(false,"synthetic",new RequestDirectoryLease());
        Contract.Read(Machine.Record)["schema"]=1; Contract.Read(Machine.Record).Remove("files");
        Check((string)Contract.Read(Machine.Record)["version"]=="1.0.4.0","baseline");
    }
    public static int Main()
    {
        try {
            int passed=0;
            for(int boundary=1;boundary<=18;boundary++) {
                Baseline(); string before=RegistryKey.Dump(); Payload("1.0.5.14",new string('b',64)); RegistryKey.Writes=0;
                if(boundary<=17) RegistryKey.FailAt=boundary; else Contract.FailRecord=true;
                bool failed=false;
                try { Mutation.Apply(false,"synthetic",new RequestDirectoryLease()); } catch(System.IO.IOException) { failed=true; }
                Check(failed,"fault_not_reached:"+boundary);
                Check(RegistryKey.Dump()==before,"registry_not_restored:"+boundary);
                Check((string)Contract.Read(Machine.Record)["version"]=="1.0.4.0" && (string)Contract.Read(Machine.Record)["sha256"]==new string('a',64),"record_not_restored");
                Check(!File.Exists(Machine.Journal),"journal_not_consumed");
                Check(Contract.Hash(Machine.InstalledDll("1.0.4.0"))==new string('a',64),"old_payload_changed");
                passed++;
            }
            Console.WriteLine("ROLLBACK PASS interrupted_update_boundaries="+passed);
            foreach(bool acl in new[]{false,true}) foreach(int boundary in new[]{1,2}) {
                Baseline(); string before=RegistryKey.Dump(); Payload("1.0.5.14",new string('b',64));
                File.CopyCalls=File.AclCalls=0;
                if(acl) File.FailAcl=boundary; else File.FailCopy=boundary;
                bool failed=false;
                try { Mutation.Apply(false,"synthetic",new RequestDirectoryLease()); } catch(System.IO.IOException) { failed=true; }
                Check(failed && RegistryKey.Dump()==before,"copy_acl_registry_restore");
                Check((string)Contract.Read(Machine.Record)["version"]=="1.0.4.0","copy_acl_record_restore");
                Check(!File.Exists(Machine.Journal),"copy_acl_journal");
                Mutation.Apply(false,"synthetic",new RequestDirectoryLease());
                Check((int)Contract.Read(Machine.Record)["schema"]==2,"retry_schema");
                using(var root=RegistryKey.OpenBaseKey(RegistryHive.LocalMachine,RegistryView.Registry64)) {
                    string events=System.IO.Path.Combine(System.IO.Path.GetDirectoryName(Machine.InstalledDll("1.0.5.14")),Contract.EventsDll);
                    Blob valid=File.Blobs[events]; File.Blobs[events]=new Blob("1.0.5.14",new string('c',64));
                    Check(!Mutation.RegistrationValid(root,"1.0.5.14"),"events_corruption_accepted");
                    File.Blobs[events]=valid; Check(Mutation.RegistrationValid(root,"1.0.5.14"),"retry_invalid");
                    File.Delete(events); Check(!Mutation.RegistrationValid(root,"1.0.5.14"),"partial_install_accepted");
                }
            }
            Console.WriteLine("ROLLBACK PASS copy_acl_faults=4 retry_and_partial_payload_rejection");
            Baseline(); Payload("1.0.5.14",new string('b',64)); Mutation.Apply(false,"synthetic",new RequestDirectoryLease());
            string installed=RegistryKey.Dump(); Payload("1.0.4.0",new string('a',64)); bool denied=false;
            try { Mutation.Apply(false,"synthetic",new RequestDirectoryLease()); } catch(System.IO.IOException e) { denied=e.Message=="mutation_blocked"; }
            Check(denied && RegistryKey.Dump()==installed,"direct_downgrade_not_blocked");
            Payload("1.0.5.14",new string('b',64)); Mutation.Apply(true,"synthetic",new RequestDirectoryLease());
            Check(!File.Exists(Machine.Record),"remove_record_remaining");
            Payload("1.0.4.0",new string('a',64)); Mutation.Apply(false,"synthetic",new RequestDirectoryLease());
            Check((string)Contract.Read(Machine.Record)["version"]=="1.0.4.0","reinstall_old_failed");
            Check(File.Exists(Machine.InstalledDll("1.0.5.14")),"new_payload_deleted");
            Console.WriteLine("ROLLBACK PASS normal_remove_then_old_reinstall old_and_new_payload_retained");
            Contract.Read(Machine.Record)["schema"]=1; Contract.Read(Machine.Record).Remove("files");
            string restored=RegistryKey.Dump(); Payload("1.0.4.0",new string('c',64)); denied=false;
            try { Mutation.Apply(false,"synthetic",new RequestDirectoryLease()); } catch(System.IO.IOException e) { denied=e.Message=="same_version_changed"; }
            Check(denied && RegistryKey.Dump()==restored && Contract.Hash(Machine.InstalledDll("1.0.4.0"))==new string('a',64),"same_version_overwrite");
            Console.WriteLine("ROLLBACK PASS same_version_changed_rejected");
            Machine.Closed=false; denied=false;
            try { Mutation.Apply(true,"synthetic",new RequestDirectoryLease()); } catch(System.IO.IOException e) { denied=e.Message=="office_busy"; }
            Check(denied && RegistryKey.Dump()==restored,"busy_remove_not_blocked");
            Console.WriteLine("ROLLBACK_RESULT PASS arch="+(IntPtr.Size*8)+" real_Mutation memory_dependencies environment_preflight_modeled"); return 0;
        } catch(Exception e) { Console.WriteLine("ROLLBACK_RESULT FAIL "+e.Message); return 1; }
    }
}
