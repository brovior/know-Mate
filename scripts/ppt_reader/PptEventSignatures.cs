using System;
using System.Runtime.InteropServices;
namespace AegisDesk.PptReader
{
    internal sealed class EventArgument
    {
        internal string kind, iid, name, type_iid, library_iid;
        internal int flags;
    }
    internal sealed class EventSignature
    {
        internal int index, dispid, slot; internal string name; internal EventArgument[] args;
        internal EventSignature(int index,string name,int dispid,int slot,params EventArgument[] args)
        { this.index=index; this.name=name; this.dispid=dispid; this.slot=slot; this.args=args; }
    }
    internal static class PptEventSignatures
    {
        internal static readonly Guid Iid=new Guid("914934C2-5A91-11CF-8700-00AA0060263B");
        private static EventArgument O(string iid) { return new EventArgument { kind="interface",iid=iid,flags=1 }; }
        private static EventArgument B() { return new EventArgument { kind="boolptr",flags=3 }; }
        private static EventArgument F() { return new EventArgument { kind="r4",flags=1 }; }
        private static EventArgument E(string name,string iid,string library)
        { return new EventArgument {kind="i4",name=name,type_iid=iid,library_iid=library,flags=1}; }
        internal static readonly EventSignature[] Events={
            new EventSignature(0,"WindowSelectionChange",2001,7,O("{91493454-5A91-11CF-8700-00AA0060263B}")),
            new EventSignature(1,"WindowBeforeRightClick",2002,8,O("{91493454-5A91-11CF-8700-00AA0060263B}"),B()),
            new EventSignature(2,"WindowBeforeDoubleClick",2003,9,O("{91493454-5A91-11CF-8700-00AA0060263B}"),B()),
            new EventSignature(3,"PresentationClose",2004,10,O("{9149349D-5A91-11CF-8700-00AA0060263B}")),
            new EventSignature(4,"PresentationSave",2005,11,O("{9149349D-5A91-11CF-8700-00AA0060263B}")),
            new EventSignature(5,"PresentationOpen",2006,12,O("{9149349D-5A91-11CF-8700-00AA0060263B}")),
            new EventSignature(6,"NewPresentation",2007,13,O("{9149349D-5A91-11CF-8700-00AA0060263B}")),
            new EventSignature(7,"PresentationNewSlide",2008,14,O("{9149346A-5A91-11CF-8700-00AA0060263B}")),
            new EventSignature(8,"WindowActivate",2009,15,O("{9149349D-5A91-11CF-8700-00AA0060263B}"),O("{91493457-5A91-11CF-8700-00AA0060263B}")),
            new EventSignature(9,"WindowDeactivate",2010,16,O("{9149349D-5A91-11CF-8700-00AA0060263B}"),O("{91493457-5A91-11CF-8700-00AA0060263B}")),
            new EventSignature(10,"SlideShowBegin",2011,17,O("{91493453-5A91-11CF-8700-00AA0060263B}")),
            new EventSignature(11,"SlideShowNextBuild",2012,18,O("{91493453-5A91-11CF-8700-00AA0060263B}")),
            new EventSignature(12,"SlideShowNextSlide",2013,19,O("{91493453-5A91-11CF-8700-00AA0060263B}")),
            new EventSignature(13,"SlideShowEnd",2014,20,O("{9149349D-5A91-11CF-8700-00AA0060263B}")),
            new EventSignature(14,"PresentationPrint",2015,21,O("{9149349D-5A91-11CF-8700-00AA0060263B}")),
            new EventSignature(15,"SlideSelectionChanged",2016,22,O("{9149346B-5A91-11CF-8700-00AA0060263B}")),
            new EventSignature(16,"ColorSchemeChanged",2017,23,O("{9149346B-5A91-11CF-8700-00AA0060263B}")),
            new EventSignature(17,"PresentationBeforeSave",2018,24,O("{9149349D-5A91-11CF-8700-00AA0060263B}"),B()),
            new EventSignature(18,"SlideShowNextClick",2019,25,O("{91493453-5A91-11CF-8700-00AA0060263B}"),O("{914934DF-5A91-11CF-8700-00AA0060263B}")),
            new EventSignature(19,"AfterNewPresentation",2020,26,O("{9149349D-5A91-11CF-8700-00AA0060263B}")),
            new EventSignature(20,"AfterPresentationOpen",2021,27,O("{9149349D-5A91-11CF-8700-00AA0060263B}")),
            new EventSignature(21,"PresentationSync",2022,28,O("{9149349D-5A91-11CF-8700-00AA0060263B}"),E("MsoSyncEventType","{00000000-0000-0000-0000-000000000000}","{2DF8D04C-5BFA-101B-BDE5-00AA0044DE52}")),
            new EventSignature(22,"SlideShowOnNext",2023,29,O("{91493453-5A91-11CF-8700-00AA0060263B}")),
            new EventSignature(23,"SlideShowOnPrevious",2024,30,O("{91493453-5A91-11CF-8700-00AA0060263B}")),
            new EventSignature(24,"PresentationBeforeClose",2025,31,O("{9149349D-5A91-11CF-8700-00AA0060263B}"),B()),
            new EventSignature(25,"ProtectedViewWindowOpen",2026,32,O("{BA72E55A-4FF5-48F4-8215-5505F990966F}")),
            new EventSignature(26,"ProtectedViewWindowBeforeEdit",2027,33,O("{BA72E55A-4FF5-48F4-8215-5505F990966F}"),B()),
            new EventSignature(27,"ProtectedViewWindowBeforeClose",2028,34,O("{BA72E55A-4FF5-48F4-8215-5505F990966F}"),E("PpProtectedViewCloseReason","{7F9B36C7-48CC-335E-B058-49658FD8CECE}","{91493440-5A91-11CF-8700-00AA0060263B}"),B()),
            new EventSignature(28,"ProtectedViewWindowActivate",2029,35,O("{BA72E55A-4FF5-48F4-8215-5505F990966F}")),
            new EventSignature(29,"ProtectedViewWindowDeactivate",2030,36,O("{BA72E55A-4FF5-48F4-8215-5505F990966F}")),
            new EventSignature(30,"PresentationCloseFinal",2031,37,O("{9149349D-5A91-11CF-8700-00AA0060263B}")),
            new EventSignature(31,"AfterDragDropOnSlide",2032,38,O("{9149346A-5A91-11CF-8700-00AA0060263B}"),F(),F()),
            new EventSignature(32,"AfterShapeSizeChange",2033,39,O("{91493479-5A91-11CF-8700-00AA0060263B}")),
        };
    }
}
