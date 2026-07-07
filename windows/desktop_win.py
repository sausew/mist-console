"""
desktop_win.py: native Windows window (WebView2 via pywebview) hosting the
MIST Console. One Python process + the system webview; no Chromium bundle.

Much simpler than the macOS desktop.py: Windows needs no Edit-menu shim
(WebView2 has native clipboard shortcuts and context menus), no Dock icon
work, and there is no hotkey overlay in this build. If WebView2 is missing,
the app falls back to the default browser pointed at the local server.
"""
import json
import os
import socket
import subprocess
import threading
import time
import urllib.request

import config_win

PORT = config_win.PORT
_main_window = None
_window_closed = False
_quick_window = None

# Overlay geometry. WebView2 has no transparent-window support in pywebview,
# so unlike the macOS NSPanel (a huge clear sheet for the glow to fade into)
# this is a tight box that grows upward when the conversation picker opens.
QW, QH = 760, 240
QH_EXPANDED = QH + 340


class Api:
    """Exposed to JS as window.pywebview.api."""

    def pick_file(self):
        try:
            import webview
            win = webview.windows[0]
            res = win.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True)
            return list(res) if res else []
        except Exception:
            return []

    def pick_folder(self):
        try:
            import webview
            win = webview.windows[0]
            res = win.create_file_dialog(webview.FOLDER_DIALOG)
            if not res:
                return ""
            return res[0] if isinstance(res, (list, tuple)) else res
        except Exception:
            return ""

    def open_url(self, url):
        try:
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                os.startfile(url)  # noqa: S606 - default browser
        except Exception:
            pass
        return True


class QuickApi:
    """Exposed to the overlay's quickbox.html (its pywebview fallback branch)."""

    def surface(self):
        """Enter was hit: hide the overlay, bring the main window up. The main
        window polls /pending-open and jumps to the seeded chat itself."""
        _quick_hide()
        _surface()
        return True

    def dismiss(self):
        _quick_hide()
        return True

    def expand(self):
        _quick_resize(QH_EXPANDED)
        return True

    def collapse(self):
        _quick_resize(QH)
        return True


def _overlay_origin(h):
    """Bottom-center of the primary screen, above the taskbar."""
    try:
        import webview
        s = webview.screens[0]
        return int((s.width - QW) / 2), int(s.height - h - 80)
    except Exception:
        return 200, 400


def _quick_resize(h):
    if _quick_window is None:
        return
    try:
        x, y = _overlay_origin(h)
        _quick_window.resize(QW, h)
        _quick_window.move(x, y)
    except Exception:
        pass


def _quick_hide():
    try:
        if _quick_window is not None:
            _quick_window.hide()
    except Exception:
        pass


def _quick_show():
    """Summon the overlay (called from the hotkey thread or POST /show-quick;
    pywebview window methods marshal to the UI thread, so this is safe)."""
    if _quick_window is None:
        return False
    try:
        x, y = _overlay_origin(QH)
        _quick_window.resize(QW, QH)   # always resummon collapsed
        _quick_window.move(x, y)
        _quick_window.show()
        try:
            _quick_window.evaluate_js("window.__qfocus && window.__qfocus()")
        except Exception:
            pass
        return True
    except Exception:
        return False


def _run_flask():
    import app_win
    app_win.app.run(host="127.0.0.1", port=PORT, threaded=True, use_reloader=False)


def _wait_for_port(port, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.2):
                return True
        except OSError:
            time.sleep(0.02)
    return False


def _instance_already_running(port):
    """True if a MIST Console is already serving on `port` (probe a route only
    our app answers, so an unrelated listener is not mistaken for us)."""
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/repo", timeout=0.6)
        return True
    except Exception:
        return False


def _raise_existing(port):
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/raise", data=b"{}",
                                     method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as r:
            body = json.loads(r.read() or b"{}")
            return bool(body.get("ok"))
    except Exception:
        return False


def _evict_port(port):
    """Kill whatever holds `port` (a windowless leftover) so this launch can
    bind it. netstat is the stdlib-free way to find the PID on Windows."""
    pids = set()
    try:
        kw = {}
        if config_win.CREATE_NO_WINDOW:
            kw["creationflags"] = config_win.CREATE_NO_WINDOW
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True,
                             text=True, timeout=10, **kw).stdout
        for line in out.splitlines():
            parts = line.split()
            if (len(parts) >= 5 and parts[0] == "TCP"
                    and parts[1].endswith(f":{port}") and parts[3] == "LISTENING"):
                if parts[4].isdigit() and int(parts[4]) != os.getpid():
                    pids.add(parts[4])
        for pid in pids:
            subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True,
                           timeout=10, **kw)
    except Exception:
        return False
    for _ in range(20):
        try:
            with socket.create_connection(("127.0.0.1", port), 0.1):
                pass
        except OSError:
            return True  # connection refused: port is free
        time.sleep(0.1)
    return not pids


def _surface():
    """Bring the window forward (called from a Flask worker via /raise)."""
    if _main_window is None or _window_closed:
        return False
    try:
        _main_window.restore()
        _main_window.show()
        try:
            _main_window.on_top = True
            _main_window.on_top = False
        except Exception:
            pass
        return True
    except Exception:
        return False


def _on_closed():
    """Closing the window fully shuts the app down: stop every claude child,
    flush session metadata, then hard-exit so nothing lingers on the port."""
    global _window_closed
    _window_closed = True
    try:
        import app_win
        for s in list(getattr(app_win, "_sessions", {}).values()):
            try:
                s.stop()
            except Exception:
                pass
        try:
            app_win._save_meta()
        except Exception:
            pass
    except Exception:
        pass

    def _bye():
        time.sleep(0.4)   # let terminate() reach the claude children
        os._exit(0)

    threading.Thread(target=_bye, daemon=True).start()


def main():
    global _main_window, _quick_window
    if _instance_already_running(PORT):
        if _raise_existing(PORT):
            return
        if not _evict_port(PORT):
            return  # couldn't free the port; bail rather than crash on bind

    threading.Thread(target=_run_flask, daemon=True).start()
    if not _wait_for_port(PORT):
        return

    import app_win
    import quickaccess_win
    app_win.surface_main = _surface
    app_win.show_quick = _quick_show
    quickaccess_win.load()

    url = f"http://127.0.0.1:{PORT}"
    try:
        import webview
        _main_window = webview.create_window(
            "MIST Console", url, js_api=Api(),
            width=1120, height=800, min_size=(720, 520),
            background_color="#0E1C2B")
        # The quick-entry overlay: a hidden frameless always-on-top window
        # summoned by the global hotkey (see quickaccess_win).
        qx, qy = _overlay_origin(QH)
        _quick_window = webview.create_window(
            "MIST Quick Entry", url + "/quickbox.html", js_api=QuickApi(),
            width=QW, height=QH, x=qx, y=qy, frameless=True, on_top=True,
            hidden=True, background_color="#0E1C2B")
        quickaccess_win.install(_quick_show)
        try:
            _main_window.events.closed += _on_closed
        except Exception:
            pass
        webview.start()
        # webview.start returning means the window closed; _on_closed handles
        # shutdown, but cover the path where the event never fired.
        _on_closed()
        time.sleep(2)
    except Exception:
        # No WebView2 runtime (or pywebview failed): serve in the default
        # browser instead. The app works identically there.
        import webbrowser
        webbrowser.open(url)
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    main()
