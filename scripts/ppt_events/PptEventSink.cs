using System;
using System.Reflection;
using System.Runtime.InteropServices;
using AegisDesk.PptReader;
[assembly: AssemblyVersion("1.0.5.0")]
[assembly: AssemblyFileVersion("1.0.5.0")]
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
            if(!state.Active){state.RecordCancellation(kind);return;}
            cancel=true; state.Cancelled=true;state.RecordCancellation(kind);
            try { CancelCheckpoint(); }
            catch { state.MarkBroken(); }
        }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void WindowSelectionChange(Microsoft.Office.Interop.PowerPoint.Selection p0){  }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void WindowBeforeRightClick(Microsoft.Office.Interop.PowerPoint.Selection p0, ref bool p1){  }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void WindowBeforeDoubleClick(Microsoft.Office.Interop.PowerPoint.Selection p0, ref bool p1){  }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void PresentationClose(Microsoft.Office.Interop.PowerPoint.Presentation p0){  }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void PresentationSave(Microsoft.Office.Interop.PowerPoint.Presentation p0){  }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void PresentationOpen(Microsoft.Office.Interop.PowerPoint.Presentation p0){  }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void NewPresentation(Microsoft.Office.Interop.PowerPoint.Presentation p0){  }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void PresentationNewSlide(Microsoft.Office.Interop.PowerPoint.Slide p0){  }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void WindowActivate(Microsoft.Office.Interop.PowerPoint.Presentation p0, Microsoft.Office.Interop.PowerPoint.DocumentWindow p1){  }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void WindowDeactivate(Microsoft.Office.Interop.PowerPoint.Presentation p0, Microsoft.Office.Interop.PowerPoint.DocumentWindow p1){  }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void SlideShowBegin(Microsoft.Office.Interop.PowerPoint.SlideShowWindow p0){  }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void SlideShowNextBuild(Microsoft.Office.Interop.PowerPoint.SlideShowWindow p0){  }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void SlideShowNextSlide(Microsoft.Office.Interop.PowerPoint.SlideShowWindow p0){  }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void SlideShowEnd(Microsoft.Office.Interop.PowerPoint.Presentation p0){  }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void PresentationPrint(Microsoft.Office.Interop.PowerPoint.Presentation p0){  }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void SlideSelectionChanged(Microsoft.Office.Interop.PowerPoint.SlideRange p0){  }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void ColorSchemeChanged(Microsoft.Office.Interop.PowerPoint.SlideRange p0){  }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void PresentationBeforeSave(Microsoft.Office.Interop.PowerPoint.Presentation p0, ref bool p1){  }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void SlideShowNextClick(Microsoft.Office.Interop.PowerPoint.SlideShowWindow p0, Microsoft.Office.Interop.PowerPoint.Effect p1){  }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void AfterNewPresentation(Microsoft.Office.Interop.PowerPoint.Presentation p0){  }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void AfterPresentationOpen(Microsoft.Office.Interop.PowerPoint.Presentation p0){  }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void PresentationSync(Microsoft.Office.Interop.PowerPoint.Presentation p0, Microsoft.Office.Core.MsoSyncEventType p1){  }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void SlideShowOnNext(Microsoft.Office.Interop.PowerPoint.SlideShowWindow p0){  }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void SlideShowOnPrevious(Microsoft.Office.Interop.PowerPoint.SlideShowWindow p0){  }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void PresentationBeforeClose(Microsoft.Office.Interop.PowerPoint.Presentation p0, ref bool p1){ Cancel(ref p1,1); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void ProtectedViewWindowOpen(Microsoft.Office.Interop.PowerPoint.ProtectedViewWindow p0){  }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void ProtectedViewWindowBeforeEdit(Microsoft.Office.Interop.PowerPoint.ProtectedViewWindow p0, ref bool p1){  }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void ProtectedViewWindowBeforeClose(Microsoft.Office.Interop.PowerPoint.ProtectedViewWindow p0, Microsoft.Office.Interop.PowerPoint.PpProtectedViewCloseReason p1, ref bool p2){ Cancel(ref p2,2); }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void ProtectedViewWindowActivate(Microsoft.Office.Interop.PowerPoint.ProtectedViewWindow p0){  }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void ProtectedViewWindowDeactivate(Microsoft.Office.Interop.PowerPoint.ProtectedViewWindow p0){  }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void PresentationCloseFinal(Microsoft.Office.Interop.PowerPoint.Presentation p0){  }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void AfterDragDropOnSlide(Microsoft.Office.Interop.PowerPoint.Slide p0, System.Single p1, System.Single p2){  }
        /// <summary>Preserve the imported PowerPoint event ABI.</summary>
        public void AfterShapeSizeChange(Microsoft.Office.Interop.PowerPoint.Shape p0){  }
    }
}
