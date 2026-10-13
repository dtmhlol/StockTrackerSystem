"""
System-tray icon and Windows notifications (pystray).

The icon runs on its own thread, so every callback is handed to `post`, which the app uses to run
the real work on the Tk thread. If pystray can't start (or the PC has no tray), `available` stays
False and the app simply behaves as it did before: closing the window quits.
"""
import logging

_NIN_BALLOONUSERCLICK = 0x0405     # the user clicked the notification balloon


class TrayIcon:
    def __init__(self, image, post, on_open, on_actions, on_quit, on_notification_click):
        self.available = False
        self._icon = None
        try:
            import pystray
        except Exception:
            logging.getLogger(__name__).exception("pystray is not available")
            return

        menu = pystray.Menu(
            pystray.MenuItem("Open Stock Tracker", lambda *_: post(on_open), default=True),
            pystray.MenuItem("Show actions", lambda *_: post(on_actions)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda *_: post(on_quit)),
        )
        self._icon = pystray.Icon("StockTracker", image, "Stock Tracker", menu)

        # pystray ignores balloon clicks, so wrap its notify handler to catch them.
        try:
            handlers = self._icon._message_handlers
            from pystray._util import win32
            original = handlers[win32.WM_NOTIFY]

            def handler(wparam, lparam):
                if lparam == _NIN_BALLOONUSERCLICK:
                    post(on_notification_click)
                    return 0
                return original(wparam, lparam)

            handlers[win32.WM_NOTIFY] = handler
        except Exception:
            logging.getLogger(__name__).info("Notification click-through unavailable; the tray icon still works")

    def start(self):
        """Shows the icon. Returns True if it is up."""
        if self._icon is None:
            return False
        try:
            self._icon.run_detached()
            self.available = True
        except Exception:
            logging.getLogger(__name__).exception("Could not start the tray icon")
            self.available = False
        return self.available

    def notify(self, title, message):
        if not self.available:
            return False
        try:
            self._icon.notify(message[:250], title[:60])
            return True
        except Exception:
            logging.getLogger(__name__).exception("Could not show the notification")
            return False

    def stop(self):
        if self._icon is not None and self.available:
            try:
                self._icon.stop()
            except Exception:
                pass
        self.available = False
