using System;
using System.Reflection;
using System.Runtime.InteropServices;
using AegisDesk.PptReader;
[assembly: AssemblyVersion("1.0.5.10")]
[assembly: AssemblyFileVersion("1.0.5.10")]
[assembly: ComVisible(false)]
namespace AegisDesk.PptEvents
{
    /// <summary>Full imported event interface; constructed only by the core's verified loader.</summary>
    [ComVisible(true), ClassInterface(ClassInterfaceType.None)]
    public sealed partial class PptEventSink : Microsoft.Office.Interop.PowerPoint.EApplication
    {
        private readonly PptShutdownState state;
        /// <summary>Use core-owned state without introducing a PIA dependency into the core.</summary>
        public PptEventSink(PptShutdownState state)
        { if(state==null) throw new ArgumentNullException("state"); this.state=state; }
        partial void CancelCheckpoint();
        private void Cancel(ref bool cancel,int kind)
        {
            bool incoming=cancel,set=false;
            state.EventBoundary(kind,true,incoming,incoming,false);
            try {
                if(!state.Active){state.RecordCancellation(kind);return;}
                cancel=true;set=true;state.Cancelled=true;state.CloseCancelled=true;
                if(state.DeferredProtection)state.Active=false;
                state.RecordCancellation(kind);
                try { CancelCheckpoint(); } catch { state.MarkBroken(); }
            } finally { state.EventBoundary(kind,false,incoming,cancel,set); }
        }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void WindowSelectionChange(Microsoft.Office.Interop.PowerPoint.Selection p0){ state.EventBoundary(101,true,false,false,false); state.EventBoundary(101,false,false,false,false); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void WindowBeforeRightClick(Microsoft.Office.Interop.PowerPoint.Selection p0, ref bool p1){ state.EventBoundary(102,true,p1,p1,false); state.EventBoundary(102,false,p1,p1,false); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void WindowBeforeDoubleClick(Microsoft.Office.Interop.PowerPoint.Selection p0, ref bool p1){ state.EventBoundary(103,true,p1,p1,false); state.EventBoundary(103,false,p1,p1,false); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void PresentationClose(Microsoft.Office.Interop.PowerPoint.Presentation p0){ state.EventBoundary(104,true,false,false,false); state.EventBoundary(104,false,false,false,false); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void PresentationSave(Microsoft.Office.Interop.PowerPoint.Presentation p0){ state.EventBoundary(105,true,false,false,false); state.EventBoundary(105,false,false,false,false); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void PresentationOpen(Microsoft.Office.Interop.PowerPoint.Presentation p0){ state.ObserveUserActivity(); state.EventBoundary(106,true,false,false,false); state.EventBoundary(106,false,false,false,false); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void NewPresentation(Microsoft.Office.Interop.PowerPoint.Presentation p0){ state.ObserveUserActivity(); state.EventBoundary(107,true,false,false,false); state.EventBoundary(107,false,false,false,false); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void PresentationNewSlide(Microsoft.Office.Interop.PowerPoint.Slide p0){ state.EventBoundary(108,true,false,false,false); state.EventBoundary(108,false,false,false,false); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void WindowActivate(Microsoft.Office.Interop.PowerPoint.Presentation p0, Microsoft.Office.Interop.PowerPoint.DocumentWindow p1){ state.ObserveUserActivity(); state.EventBoundary(109,true,false,false,false); state.EventBoundary(109,false,false,false,false); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void WindowDeactivate(Microsoft.Office.Interop.PowerPoint.Presentation p0, Microsoft.Office.Interop.PowerPoint.DocumentWindow p1){ state.EventBoundary(110,true,false,false,false); state.EventBoundary(110,false,false,false,false); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void SlideShowBegin(Microsoft.Office.Interop.PowerPoint.SlideShowWindow p0){ state.EventBoundary(111,true,false,false,false); state.EventBoundary(111,false,false,false,false); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void SlideShowNextBuild(Microsoft.Office.Interop.PowerPoint.SlideShowWindow p0){ state.EventBoundary(112,true,false,false,false); state.EventBoundary(112,false,false,false,false); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void SlideShowNextSlide(Microsoft.Office.Interop.PowerPoint.SlideShowWindow p0){ state.EventBoundary(113,true,false,false,false); state.EventBoundary(113,false,false,false,false); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void SlideShowEnd(Microsoft.Office.Interop.PowerPoint.Presentation p0){ state.EventBoundary(114,true,false,false,false); state.EventBoundary(114,false,false,false,false); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void PresentationPrint(Microsoft.Office.Interop.PowerPoint.Presentation p0){ state.EventBoundary(115,true,false,false,false); state.EventBoundary(115,false,false,false,false); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void SlideSelectionChanged(Microsoft.Office.Interop.PowerPoint.SlideRange p0){ state.EventBoundary(116,true,false,false,false); state.EventBoundary(116,false,false,false,false); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void ColorSchemeChanged(Microsoft.Office.Interop.PowerPoint.SlideRange p0){ state.EventBoundary(117,true,false,false,false); state.EventBoundary(117,false,false,false,false); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void PresentationBeforeSave(Microsoft.Office.Interop.PowerPoint.Presentation p0, ref bool p1){ state.EventBoundary(118,true,p1,p1,false); state.EventBoundary(118,false,p1,p1,false); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void SlideShowNextClick(Microsoft.Office.Interop.PowerPoint.SlideShowWindow p0, Microsoft.Office.Interop.PowerPoint.Effect p1){ state.EventBoundary(119,true,false,false,false); state.EventBoundary(119,false,false,false,false); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void AfterNewPresentation(Microsoft.Office.Interop.PowerPoint.Presentation p0){ state.ObserveUserActivity(); state.EventBoundary(120,true,false,false,false); state.EventBoundary(120,false,false,false,false); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void AfterPresentationOpen(Microsoft.Office.Interop.PowerPoint.Presentation p0){ state.ObserveUserActivity(); state.EventBoundary(121,true,false,false,false); state.EventBoundary(121,false,false,false,false); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void PresentationSync(Microsoft.Office.Interop.PowerPoint.Presentation p0, Microsoft.Office.Core.MsoSyncEventType p1){ state.EventBoundary(122,true,false,false,false); state.EventBoundary(122,false,false,false,false); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void SlideShowOnNext(Microsoft.Office.Interop.PowerPoint.SlideShowWindow p0){ state.EventBoundary(123,true,false,false,false); state.EventBoundary(123,false,false,false,false); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void SlideShowOnPrevious(Microsoft.Office.Interop.PowerPoint.SlideShowWindow p0){ state.EventBoundary(124,true,false,false,false); state.EventBoundary(124,false,false,false,false); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void PresentationBeforeClose(Microsoft.Office.Interop.PowerPoint.Presentation p0, ref bool p1){ Cancel(ref p1,1); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void ProtectedViewWindowOpen(Microsoft.Office.Interop.PowerPoint.ProtectedViewWindow p0){ state.ObserveUserActivity(); state.EventBoundary(125,true,false,false,false); state.EventBoundary(125,false,false,false,false); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void ProtectedViewWindowBeforeEdit(Microsoft.Office.Interop.PowerPoint.ProtectedViewWindow p0, ref bool p1){ state.EventBoundary(126,true,p1,p1,false); state.EventBoundary(126,false,p1,p1,false); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void ProtectedViewWindowBeforeClose(Microsoft.Office.Interop.PowerPoint.ProtectedViewWindow p0, Microsoft.Office.Interop.PowerPoint.PpProtectedViewCloseReason p1, ref bool p2){ Cancel(ref p2,2); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void ProtectedViewWindowActivate(Microsoft.Office.Interop.PowerPoint.ProtectedViewWindow p0){ state.ObserveUserActivity(); state.EventBoundary(127,true,false,false,false); state.EventBoundary(127,false,false,false,false); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void ProtectedViewWindowDeactivate(Microsoft.Office.Interop.PowerPoint.ProtectedViewWindow p0){ state.EventBoundary(128,true,false,false,false); state.EventBoundary(128,false,false,false,false); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void PresentationCloseFinal(Microsoft.Office.Interop.PowerPoint.Presentation p0){ state.EventBoundary(129,true,false,false,false); state.EventBoundary(129,false,false,false,false); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void AfterDragDropOnSlide(Microsoft.Office.Interop.PowerPoint.Slide p0, System.Single p1, System.Single p2){ state.EventBoundary(130,true,false,false,false); state.EventBoundary(130,false,false,false,false); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void AfterShapeSizeChange(Microsoft.Office.Interop.PowerPoint.Shape p0){ state.EventBoundary(131,true,false,false,false); state.EventBoundary(131,false,false,false,false); }
    }
}
