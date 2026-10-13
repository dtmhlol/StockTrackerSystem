"""
Only one Stock Tracker may run at a time.

Needed now that closing the window keeps the app running in the tray: opening the shortcut again
must bring the running copy back instead of starting a second one that fights over the database.
Uses a named mutex plus two named events ("show" and "quit") via ctypes. Where Windows refuses
(or on other platforms) every call quietly says "you are the only one".
"""
import ctypes
import sys
import threading

_MUTEX_NAME = "Local\\StockTrackerSystem.Instance"
_SHOW_EVENT = "Local\\StockTrackerSystem.Show"
_QUIT_EVENT = "Local\\StockTrackerSystem.Quit"

_ERROR_ALREADY_EXISTS = 183
_EVENT_MODIFY_STATE = 0x0002
_WAIT_OBJECT_0 = 0


class SingleInstance:
    def __init__(self):
        self._handles = {}
        self._stop = threading.Event()
        self._kernel32 = ctypes.windll.kernel32 if sys.platform == "win32" else None
        if self._kernel32 is not None:
            self._kernel32.CreateMutexW.restype = ctypes.c_void_p
            self._kernel32.CreateEventW.restype = ctypes.c_void_p
            self._kernel32.OpenEventW.restype = ctypes.c_void_p
            self._kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            self._kernel32.SetEvent.argtypes = [ctypes.c_void_p]

    def acquire(self):
        """True if this is the first copy running. False if another copy already holds the mutex."""
        if self._kernel32 is None:
            return True
        mutex = self._kernel32.CreateMutexW(None, False, _MUTEX_NAME)
        if not mutex:
            return True
        self._handles["mutex"] = mutex
        return ctypes.GetLastError() != _ERROR_ALREADY_EXISTS

    def signal_running_copy(self, quit_it=False):
        """Asks the running copy to show its window (or to quit). Returns True if it was reached."""
        if self._kernel32 is None:
            return False
        event = self._kernel32.OpenEventW(_EVENT_MODIFY_STATE, False, _QUIT_EVENT if quit_it else _SHOW_EVENT)
        if not event:
            return False
        try:
            self._kernel32.SetEvent(event)
        finally:
            self._kernel32.CloseHandle(ctypes.c_void_p(event))
        return True

    def listen(self, on_show, on_quit):
        """Starts a background thread that calls on_show / on_quit when another copy signals.
        The callbacks run on that thread, so they should only hand work to the UI thread."""
        if self._kernel32 is None:
            return
        events = []
        for name, callback in ((_SHOW_EVENT, on_show), (_QUIT_EVENT, on_quit)):
            handle = self._kernel32.CreateEventW(None, False, False, name)   # auto-reset
            if handle:
                events.append((handle, callback))
                self._handles[name] = handle
        if not events:
            return

        def watch():
            while not self._stop.is_set():
                for handle, callback in events:
                    if self._kernel32.WaitForSingleObject(handle, 250) == _WAIT_OBJECT_0:
                        try:
                            callback()
                        except Exception:
                            pass

        threading.Thread(target=watch, daemon=True, name="single-instance").start()

    def release(self):
        """Gives up the 'first copy' lock (used just before the app restarts itself)."""
        self._stop.set()
        mutex = self._handles.pop("mutex", None)
        if mutex and self._kernel32 is not None:
            self._kernel32.CloseHandle(ctypes.c_void_p(mutex))
