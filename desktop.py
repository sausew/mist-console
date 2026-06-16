# /// script
# dependencies = ["flask", "pywebview"]
# ///
"""
desktop.py — native macOS window (WKWebView via pywebview) hosting the MIST
Console. Low RAM: system webview + one Python process, no Chromium, no Node.
Run via: uv run --script desktop.py

Adds:
- a native Edit menu so Cmd+X/C/V/A/Z and the right-click menu work in WKWebView
- a file-picker bridge (window.pywebview.api.pick_file) → native open dialog
"""
import json
import os
import subprocess
import threading
import time

import webview

import app as appmod

PORT = 5014
QUIET_MARKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".quiet-launch")
QW, QH = 780, 200   # quick-entry overlay size (collapsed)
QH_EXPANDED = QH + 340   # taller, to fit the conversation picker above the box
OFFSCREEN = -6000   # where the overlay is parked when not summoned
Q_BOTTOM_MARGIN = 24   # gap between the overlay's bottom edge and the screen bottom

_main_window = None
_quick_window = None
_quiet_launch = False   # True when summoned via hotkey with the console hidden


def _activate():
    try:
        from AppKit import NSApplication
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception:
        pass


def _set_activation_policy(accessory):
    """Accessory (LSUIElement) lets the overlay panel take key focus and float
    OVER another app's fullscreen Space without switching Spaces — a Regular app
    can't do that. We run Accessory while the overlay is the only thing showing,
    and flip back to Regular when the main console window surfaces (Dock icon +
    Cmd-Tab while you're actually using her). Setting the same policy twice is a
    no-op, so calling this on every summon won't flicker. Main thread only."""
    try:
        from AppKit import (NSApplication, NSApplicationActivationPolicyAccessory,
                            NSApplicationActivationPolicyRegular)
        pol = (NSApplicationActivationPolicyAccessory if accessory
               else NSApplicationActivationPolicyRegular)
        NSApplication.sharedApplication().setActivationPolicy_(pol)
    except Exception:
        pass


# ---- native overlay: a non-activating NSPanel hosting a WKWebView --------------
# A pywebview NSWindow cannot float over a fullscreen app; a non-activating NSPanel
# with CanJoinAllSpaces|FullScreenAuxiliary + a high level can — and can take key
# focus WITHOUT activating the app (which would exit the fullscreen Space).
_panel = None
_webview = None
_handler = None
_handler_class = None
_panel_class = None
_summon_app = None                     # frontmost app at the moment the hotkey fired
SHOT_PATH = "/tmp/mist-quickshot.png"  # selection screenshot lands here

# how to ask each browser for its current page URL
BROWSER_SCRIPTS = {
    "Safari": 'tell application "Safari" to return URL of front document',
    "Google Chrome": 'tell application "Google Chrome" to return URL of active tab of front window',
    "Brave Browser": 'tell application "Brave Browser" to return URL of active tab of front window',
    "Microsoft Edge": 'tell application "Microsoft Edge" to return URL of active tab of front window',
    "Arc": 'tell application "Arc" to return URL of active tab of front window',
    "Vivaldi": 'tell application "Vivaldi" to return URL of active tab of front window',
    "Chromium": 'tell application "Chromium" to return URL of active tab of front window',
}


def _run_main(fn):
    """Run fn on the main thread (AppKit is main-thread only)."""
    try:
        from Foundation import NSOperationQueue
        NSOperationQueue.mainQueue().addOperationWithBlock_(fn)
    except Exception:
        fn()


def _take_screenshot():
    """Hide the overlay, let the user drag a selection (screencapture -i), then
    re-show the overlay with the shot attached. Runs the blocking capture off the
    main thread."""
    def _work():
        _run_main(_hide_panel)
        time.sleep(0.2)   # let the overlay vanish before the selection UI
        try:
            os.remove(SHOT_PATH)
        except Exception:
            pass
        try:
            subprocess.run(["/usr/sbin/screencapture", "-i", SHOT_PATH], timeout=180)
        except Exception:
            pass
        ok = os.path.exists(SHOT_PATH)

        def _after():
            _show_panel()
            if _webview is not None:
                arg = json.dumps(SHOT_PATH) if ok else "null"
                _webview.evaluateJavaScript_completionHandler_(
                    "window.__attachShot && window.__attachShot(%s)" % arg, None)
        _run_main(_after)

    threading.Thread(target=_work, daemon=True).start()


def _get_url():
    """If the app that was frontmost when summoned is a browser, fetch its current
    URL (AppleScript) and attach it. macOS prompts for Automation on first use."""
    def _work():
        url = None
        try:
            script = BROWSER_SCRIPTS.get(_summon_app)
            if script:
                r = subprocess.run(["/usr/bin/osascript", "-e", script],
                                   capture_output=True, text=True, timeout=5)
                url = (r.stdout or "").strip() or None
        except Exception:
            pass

        def _after():
            if url and _webview is not None:
                _webview.evaluateJavaScript_completionHandler_(
                    "window.__attachUrl && window.__attachUrl(%s)" % json.dumps(url), None)
        _run_main(_after)

    threading.Thread(target=_work, daemon=True).start()


def _build_panel():
    from AppKit import (NSPanel, NSColor, NSBackingStoreBuffered,
                        NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel,
                        NSScreenSaverWindowLevel,
                        NSWindowCollectionBehaviorCanJoinAllSpaces,
                        NSWindowCollectionBehaviorFullScreenAuxiliary,
                        NSWindowCollectionBehaviorStationary)
    from WebKit import WKWebView, WKWebViewConfiguration, WKUserContentController
    from Foundation import NSObject, NSURL, NSURLRequest, NSMakeRect
    global _panel, _webview, _handler, _handler_class, _panel_class

    if _panel_class is None:
        # A borderless window returns canBecomeKeyWindow=False by default, so it
        # can't take keyboard focus — override it (and main) to True.
        class _MistPanel(NSPanel):
            def canBecomeKeyWindow(self):
                return True

            def canBecomeMainWindow(self):
                return True
        _panel_class = _MistPanel

    if _handler_class is None:
        class _MistHandler(NSObject):
            def userContentController_didReceiveScriptMessage_(self, ucc, message):
                try:
                    action = str(message.body())
                    print("quick-access: panel msg:", action, flush=True)
                    if action == "surface":
                        _surface()
                    elif action == "screenshot":
                        _take_screenshot()
                    elif action == "url":
                        _get_url()
                    elif action == "expand":
                        _set_panel_height(QH_EXPANDED)
                    elif action == "collapse":
                        _set_panel_height(QH)
                    else:
                        _hide_panel()
                except Exception as e:
                    print("quick-access: handler error:", e, flush=True)
        _handler_class = _MistHandler

    rect = NSMakeRect(0, 0, QW, QH)
    panel = _panel_class.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
        NSBackingStoreBuffered, False)
    panel.setLevel_(NSScreenSaverWindowLevel)
    panel.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces
        | NSWindowCollectionBehaviorFullScreenAuxiliary
        | NSWindowCollectionBehaviorStationary)
    panel.setOpaque_(False)
    panel.setBackgroundColor_(NSColor.clearColor())
    panel.setHasShadow_(False)
    panel.setFloatingPanel_(True)
    panel.setBecomesKeyOnlyIfNeeded_(False)
    panel.setHidesOnDeactivate_(False)

    config = WKWebViewConfiguration.alloc().init()
    ucc = WKUserContentController.alloc().init()
    _handler = _handler_class.alloc().init()
    ucc.addScriptMessageHandler_name_(_handler, "mist")
    config.setUserContentController_(ucc)
    wv = WKWebView.alloc().initWithFrame_configuration_(rect, config)
    try:
        wv.setValue_forKey_(False, "drawsBackground")   # transparent
    except Exception:
        pass
    wv.loadRequest_(NSURLRequest.requestWithURL_(
        NSURL.URLWithString_(f"http://127.0.0.1:{PORT}/quickbox.html")))
    panel.setContentView_(wv)
    _panel, _webview = panel, wv


def _position_panel():
    from AppKit import NSScreen
    from Foundation import NSMakeRect
    vf = NSScreen.mainScreen().visibleFrame()   # bottom-left origin
    # Anchored to the bottom of the screen (small margin above the Dock / screen
    # edge). Always (re)summon collapsed; the box grows upward (_set_panel_height
    # keeps this bottom-left origin fixed) so the picker opens above the input.
    _panel.setFrame_display_(NSMakeRect(
        vf.origin.x + (vf.size.width - QW) / 2, vf.origin.y + Q_BOTTOM_MARGIN,
        QW, QH), True)


def _set_panel_height(h):
    """Resize the overlay keeping its bottom-left origin fixed, so it grows/shrinks
    upward — the picker dropdown opens above the bottom-anchored input box."""
    if not _panel:
        return
    from Foundation import NSMakeRect
    f = _panel.frame()
    _panel.setFrame_display_animate_(
        NSMakeRect(f.origin.x, f.origin.y, QW, h), True, False)


def _show_panel():
    try:
        if _panel is None:
            _build_panel()
        # Go Accessory so the panel can float over (and take focus inside) another
        # app's fullscreen Space. No-op if already Accessory, so no Dock flicker.
        _set_activation_policy(True)
        _position_panel()
        _panel.orderFrontRegardless()
        _panel.makeKeyAndOrderFront_(None)
        _panel.makeKeyWindow()
        if _webview is not None:
            _panel.makeFirstResponder_(_webview)   # route keystrokes into the input
        try:
            _webview.evaluateJavaScript_completionHandler_(
                "window.__qfocus && window.__qfocus()", None)
        except Exception:
            pass
        print("quick-access: overlay shown, key =", bool(_panel.isKeyWindow()), flush=True)
    except Exception as e:
        print("quick-access: show error:", e, flush=True)


def _hide_panel():
    try:
        if _panel:
            _panel.orderOut_(None)
    except Exception:
        pass


def _surface():
    """Enter: hide the overlay, bring MIST's main window up. The main window polls
    /pending-open (~1.5s) to jump to the new chat — don't evaluate_js on the main
    thread (it deadlocks)."""
    _hide_panel()
    # Back to a normal Dock app while the full console is in use.
    _set_activation_policy(False)
    _activate()
    try:
        _main_window.restore()
        _main_window.show()
    except Exception:
        pass


def _quick_show(app=None):
    """Summon the overlay. `app` is the frontmost app when the hotkey fired (for
    the URL attachment). Dispatched to the MAIN thread (AppKit is main-thread only;
    the agent calls this from a Flask worker thread)."""
    global _summon_app
    if app is not None:
        _summon_app = app
    try:
        from Foundation import NSOperationQueue
        NSOperationQueue.mainQueue().addOperationWithBlock_(_show_panel)
    except Exception as e:
        print("quick-access: dispatch error:", e, flush=True)
        _show_panel()


class Api:
    """Exposed to JS as window.pywebview.api."""

    def pick_file(self):
        try:
            win = webview.windows[0]
            res = win.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True)
            return list(res) if res else []
        except Exception:
            return []

    def open_url(self, url):
        """Open a link in the default browser instead of navigating the WKWebView."""
        try:
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                subprocess.run(["/usr/bin/open", url], check=False)
        except Exception:
            pass
        return True


def _install_edit_menu():
    """Build a standard Edit menu wired to the first-responder selectors, so the
    focused web view receives cut/copy/paste/select-all/undo. Runs on main thread."""
    try:
        from AppKit import NSApplication, NSMenu, NSMenuItem

        app = NSApplication.sharedApplication()
        mainmenu = app.mainMenu()
        if mainmenu is None:
            mainmenu = NSMenu.alloc().init()
            app.setMainMenu_(mainmenu)

        edit_item = NSMenuItem.alloc().init()
        mainmenu.addItem_(edit_item)
        edit_menu = NSMenu.alloc().initWithTitle_("Edit")
        edit_item.setSubmenu_(edit_menu)

        def add(title, selector, key):
            mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, selector, key)
            edit_menu.addItem_(mi)

        add("Undo", "undo:", "z")
        add("Redo", "redo:", "Z")  # capital -> Cmd+Shift+Z
        edit_menu.addItem_(NSMenuItem.separatorItem())
        add("Cut", "cut:", "x")
        add("Copy", "copy:", "c")
        add("Paste", "paste:", "v")
        add("Select All", "selectAll:", "a")
    except Exception as e:  # never block startup on menu setup
        print("edit menu setup skipped:", e)


def _setup():
    _install_edit_menu()
    # Baseline policy: Accessory when launched quietly as the overlay (so it floats
    # over fullscreen); Regular when the console was opened directly to chat.
    _set_activation_policy(_quiet_launch)
    # The double-tap-Option hotkey is owned by the always-on mist-hotkey-agent (so
    # it works even when MIST is closed). Expose a main-thread summon hook that the
    # agent calls via POST /show-quick.
    # pywebview window methods are thread-safe (they marshal to the UI thread),
    # so the Flask worker thread can call this directly.
    appmod.show_quick = _quick_show


def _on_start():
    # Schedule main-thread setup (we're on a worker thread here).
    try:
        from PyObjCTools import AppHelper

        AppHelper.callAfter(_setup)
    except Exception:
        _setup()


def _run_flask():
    appmod.app.run(host="127.0.0.1", port=PORT, threaded=True, use_reloader=False)


def _wait_for_port(port, timeout=6.0):
    """Block only until Flask is actually accepting connections, instead of a
    fixed sleep — the window then appears the instant the server is ready."""
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.2):
                return True
        except OSError:
            time.sleep(0.02)
    return False


def main():
    global _main_window, _quiet_launch
    threading.Thread(target=_run_flask, daemon=True).start()
    _wait_for_port(PORT)  # ready in ~0.2s instead of a hardcoded 1.0s sleep
    # quiet quick-access launch: start with the console window hidden (only the
    # overlay shows); it reveals itself when the user hits Enter (QuickApi.surface).
    quiet = False
    try:
        if os.path.exists(QUIET_MARKER):
            quiet = time.time() - os.path.getmtime(QUIET_MARKER) < 30
            os.remove(QUIET_MARKER)
    except Exception:
        pass
    _quiet_launch = quiet
    _main_window = webview.create_window(
        "MIST Console", f"http://127.0.0.1:{PORT}",
        js_api=Api(), width=1120, height=800, min_size=(720, 520),
        background_color="#0E1C2B", hidden=quiet)
    # The quick-entry overlay is a native NSPanel (built lazily in _show_panel),
    # not a pywebview window — only that can float over fullscreen apps.
    webview.start(_on_start)


if __name__ == "__main__":
    main()
