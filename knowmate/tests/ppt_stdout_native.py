"""Windows handle ownership for the directly launched test Python child only."""

import ctypes

from knowmate.tests.ppt_stdout_lifecycle import WAIT_FAILED, WaitResult


class NativeStdoutChild:
    """Retain one child handle without PID lookup or product-process termination."""

    def __init__(self, process: object) -> None:
        self.process = process
        self.child = None
        self.handle = None
        self.duplicated = False
        self.duplicate_error: int | None = None
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.GetCurrentProcess.restype = ctypes.c_void_p
        self.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.kernel.CloseHandle.restype = ctypes.c_int
        self.kernel.DuplicateHandle.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        self.kernel.DuplicateHandle.restype = ctypes.c_int
        self.kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self.kernel.WaitForSingleObject.restype = ctypes.c_uint32
        self.kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self.kernel.TerminateProcess.restype = ctypes.c_int
        self.kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        self.kernel.GetExitCodeProcess.restype = ctypes.c_int

    def attach(self, child: object) -> None:
        """Own the newly started Python before any operation that could fail."""
        self.child = child
        self.handle = int(child._handle)
        retained = ctypes.c_void_p()
        current = self.kernel.GetCurrentProcess()
        if not self.kernel.DuplicateHandle(current, self.handle, current,
                ctypes.byref(retained), 0, False, 2):
            self.duplicate_error = ctypes.get_last_error()
            # Retain the original control handle until finalization on this
            # early-failure path; do not close it before wait/termination.
            return
        self.handle = retained.value
        self.duplicated = True

    def wait(self, milliseconds: int) -> WaitResult:
        """Return the unchanged wait code and immediate GetLastError on WAIT_FAILED."""
        result = int(self.kernel.WaitForSingleObject(self.handle, milliseconds))
        error = ctypes.get_last_error() if result == WAIT_FAILED else None
        return WaitResult(result, error)

    def terminate(self) -> None:
        """Terminate only this test's directly launched Python using its retained handle."""
        if not self.kernel.TerminateProcess(self.handle, 125):
            error = ctypes.get_last_error()
            # Exit can race the cleanup termination request.
            if self.wait(0).result != 0:
                raise ctypes.WinError(error)

    def exit_code(self) -> int:
        """Read the original exit code after the retained process handle signals."""
        value = ctypes.c_uint32()
        if not self.kernel.GetExitCodeProcess(self.handle, ctypes.byref(value)):
            raise ctypes.WinError(ctypes.get_last_error())
        return value.value

    def close_observation(self) -> None:
        """Close product observation while retaining control on duplication failure."""
        if self.child is not None and not self.duplicated:
            self.process._process = None
        self.process.close()

    def close_verifier(self) -> None:
        """Close the owned verifier once, after the last process wait."""
        if self.handle is None:
            return
        handle, self.handle = self.handle, None
        if self.duplicated:
            if not self.kernel.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            self.child.close_observation()
