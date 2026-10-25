using System;
using System.IO;
using System.Text;

namespace AegisDesk.PptSetup
{
    /// <summary>Small ordered binary frame; no untrusted JSON parser is needed.</summary>
    internal static class ChildDiagnosticWire
    {
        internal const int Maximum = 2048;
        private static void Text(BinaryWriter writer, string text, int maximum)
        {
            if (text == null || text.Length > maximum) throw new InvalidDataException();
            byte[] bytes = Encoding.ASCII.GetBytes(text);
            if (Encoding.ASCII.GetString(bytes) != text) throw new InvalidDataException();
            writer.Write((byte)bytes.Length); writer.Write(bytes);
        }
        private static string Text(BinaryReader reader, int maximum)
        {
            int length = reader.ReadByte(); if (length > maximum) throw new InvalidDataException();
            byte[] bytes = reader.ReadBytes(length); if (bytes.Length != length) throw new EndOfStreamException();
            foreach (byte value in bytes) if (value > 127) throw new InvalidDataException();
            return Encoding.ASCII.GetString(bytes);
        }
        private static bool Flag(BinaryReader reader) { byte value=reader.ReadByte(); if(value>1)throw new InvalidDataException();return value==1; }
        private static void NullableText(BinaryWriter writer,string value,int max) { writer.Write((byte)(value==null?0:1));if(value!=null)Text(writer,value,max); }
        private static string NullableText(BinaryReader reader,int max) { return Flag(reader)?Text(reader,max):null; }
        private static void Error(BinaryWriter writer,ChildDiagnosticError error)
        {
            writer.Write((byte)(error==null?0:1));if(error==null)return;
            if(!error.Valid())throw new InvalidDataException();
            Text(writer,error.Stage,63);Text(writer,error.Target,31);Text(writer,error.ErrorType,31);NullableText(writer,error.PolicyReason,63);
            writer.Write((byte)(error.WinError.HasValue?1:0));if(error.WinError.HasValue)writer.Write(error.WinError.Value);
            NullableText(writer,error.HResult,8);NullableText(writer,error.NativeStage,31);NullableText(writer,error.NtStatus,8);NullableText(writer,error.IoStatus,8);NullableText(writer,error.CheckStatus,31);NullableText(writer,error.CheckReason,63);
        }
        private static ChildDiagnosticError Error(BinaryReader reader)
        {
            if(!Flag(reader))return null;
            var value=new ChildDiagnosticError {Stage=Text(reader,63),Target=Text(reader,31),ErrorType=Text(reader,31),PolicyReason=NullableText(reader,63)};
            value.WinError=Flag(reader)?(int?)reader.ReadInt32():null;
            value.HResult=NullableText(reader,8);value.NativeStage=NullableText(reader,31);value.NtStatus=NullableText(reader,8);value.IoStatus=NullableText(reader,8);value.CheckStatus=NullableText(reader,31);value.CheckReason=NullableText(reader,63);
            if(!value.Valid())throw new InvalidDataException();return value;
        }
        private static byte Phase(string phase) { return phase=="parent_repair"?(byte)1:phase=="install"?(byte)2:phase=="remove"?(byte)3:(byte)0; }
        internal static byte[] Encode(Guid request,Guid nonce,uint pid,ChildDiagnosticObservation value)
        {
            if(!value.ValidReceived()||Phase(value.Phase)==0)throw new InvalidDataException();
            using(var memory=new MemoryStream())using(var writer=new BinaryWriter(memory))
            {
                writer.Write(0x44475041U);writer.Write((byte)1);writer.Write(request.ToByteArray());writer.Write(nonce.ToByteArray());Text(writer,Contract.Version,32);writer.Write(Phase(value.Phase));writer.Write(pid);writer.Write(value.ExitCode.Value);
                Text(writer,value.LastStage,63);Text(writer,value.LastTarget,31);Error(writer,value.Primary);Error(writer,value.Secondary);writer.Flush();
                byte[] payload=memory.ToArray();if(payload.Length>Maximum)throw new InvalidDataException();
                using(var frame=new MemoryStream())using(var output=new BinaryWriter(frame)){output.Write(payload.Length);output.Write(payload);output.Flush();return frame.ToArray();}
            }
        }
        internal static ChildDiagnosticObservation Decode(byte[] payload,Guid request,Guid nonce,uint pid,uint code,string phase)
        {
            if(payload==null||payload.Length<1||payload.Length>Maximum)throw new InvalidDataException();
            using(var memory=new MemoryStream(payload))using(var reader=new BinaryReader(memory))
            {
                if(reader.ReadUInt32()!=0x44475041U||reader.ReadByte()!=1||new Guid(reader.ReadBytes(16))!=request||new Guid(reader.ReadBytes(16))!=nonce||Text(reader,32)!=Contract.Version||reader.ReadByte()!=Phase(phase)||reader.ReadUInt32()!=pid||reader.ReadUInt32()!=code)throw new InvalidDataException();
                var result=new ChildDiagnosticObservation {Phase=phase,Transport="received",ExitCode=code,LastStage=Text(reader,63),LastTarget=Text(reader,31),Primary=Error(reader),Secondary=Error(reader)};
                if(memory.Position!=memory.Length||!result.ValidReceived())throw new InvalidDataException();return result;
            }
        }
    }
}
