// The guard exists only during a synchronous host-STA shutdown request.
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Threading;

namespace AegisDesk.PptReader
{
    internal sealed class PptShutdownGuard
    {
        private object application;
        private readonly int mainThread;
        private readonly Func<object, IConnectionPoint> resolvePoint;
        private bool disconnected;
        private bool entered;
        private bool attempted;
        private bool broken;

        internal PptShutdownGuard(object host, int thread, Func<object, IConnectionPoint> resolver)
        { application = host; mainThread = thread; resolvePoint = resolver; }

        internal void Disconnect() { disconnected = true; application = null; }
        internal bool Entered { get { return entered; } }

        internal string TryQuitIfIdle(bool reading)
        {
            if (disconnected || broken) return "not_supported";
            if (entered || attempted || reading) return "protected";
            if (Thread.CurrentThread.ManagedThreadId != mainThread
                || Thread.CurrentThread.GetApartmentState() != ApartmentState.STA) return "not_supported";
            entered = true;
            IConnectionPoint point = null;
            int cookie = 0;
            bool advised = false;
            bool quitStarted = false;
            bool noMoreRpc = false;
            var sink = new PptCloseEventSink();
            // Keep these locals alive through host disconnect callbacks and Quit return.
            object host = application;
            string result = "failed";
            try
            {
                try
                {
                    point = resolvePoint(host);
                    point.Advise(sink, out cookie);
                    advised = true;
                }
                catch (Exception exc)
                {
                    broken = true;
                    COMException fatal = FatalTransport(exc);
                    if (fatal != null) { noMoreRpc = true; throw fatal; }
                    if (!disconnected && point != null && cookie != 0)
                    {
                        try { point.Unadvise(cookie); }
                        catch { /* Inactive sink; capability remains disabled. */ }
                    }
                    return "not_supported";
                }
                if (disconnected) return "not_supported";
                ((dynamic)host).DisplayAlerts = 2; // ppAlertsAll; never suppress save prompts.
                if (disconnected) return "not_supported";
                int alerts = (int)((dynamic)host).DisplayAlerts;
                if (disconnected) return "not_supported";
                if (alerts != 2) return "failed";
                sink.Active = true;
                object presentations = ((dynamic)host).Presentations;
                if (disconnected) return "not_supported";
                if ((int)((dynamic)presentations).Count != 0) return "protected";
                if (disconnected) return "not_supported";
                object protectedViews = ((dynamic)host).ProtectedViewWindows;
                if (disconnected) return "not_supported";
                if ((int)((dynamic)protectedViews).Count != 0) return "protected";
                if (disconnected) return "not_supported";
                object slideShows = ((dynamic)host).SlideShowWindows;
                if (disconnected) return "not_supported";
                if ((int)((dynamic)slideShows).Count != 0) return "protected";
                // Count/alert RPCs can reenter Office. Never request after disconnect.
                if (disconnected || sink.Cancelled) return "protected";
                attempted = true;
                quitStarted = true;
                ((dynamic)host).Quit();
                result = sink.Cancelled ? "protected" : "quit_requested";
            }
            catch (Exception exc)
            {
                COMException fatal = FatalTransport(exc);
                if (fatal != null)
                {
                    noMoreRpc = true;
                    broken = true;
                    throw new COMException("ppt_transport_failed", fatal.ErrorCode);
                }
                if (quitStarted)
                {
                    noMoreRpc = true;
                    throw new COMException("ppt_quit_outcome_unknown", Marshal.GetHRForException(exc));
                }
                result = "failed";
            }
            finally
            {
                // Even Unadvise failure must not obstruct subsequent manual closes.
                sink.Active = false;
                if (advised && !noMoreRpc && !disconnected)
                {
                    try { point.Unadvise(cookie); }
                    catch { broken = true; }
                }
                GC.KeepAlive(sink);
                GC.KeepAlive(point);
                GC.KeepAlive(host);
                entered = false;
            }
            return result;
        }

        private static COMException FatalTransport(Exception error)
        {
            for (int depth = 0; error != null && depth < 8; depth++, error = error.InnerException)
            {
                COMException com = error as COMException;
                if (com == null) continue;
                switch (unchecked((uint)com.ErrorCode))
                {
                    case 0x800706BA: case 0x800706BE: case 0x800706BF: case 0x80010006:
                    case 0x80010007: case 0x80010012: case 0x80010108: case 0x800401FD: return com;
                }
            }
            return null;
        }
    }
}
