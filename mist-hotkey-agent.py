# /// script
# dependencies = ["pyobjc-framework-Cocoa", "pyobjc-framework-WebKit", "setproctitle"]
# ///
"""
mist-hotkey-agent.py — a tiny always-on background process that owns the MIST
quick-access gesture (double-tap Option) AND the glowing quick-entry overlay.

WHY THE OVERLAY LIVES HERE (and not in the console): the overlay must float over —
and take keyboard focus inside — whatever app is frontmost, including apps in native
fullscreen. macOS only lets a process do that when it is an Accessory (LSUIElement)
app. But the console process owns a real, fullscreen-capable window, and an Accessory
app cannot own a fullscreen Space — so if the console flipped its own policy to show
the overlay it would tear down its own fullscreen (menu bar slices off the top, the
traffic lights vanish). This agent is permanently Accessory and has no window of its
own to lose, so it can host the overlay over anything without ever touching the
console's window. (This is how Claude desktop / Raycast / Alfred do it: the global
panel is owned by a dedicated accessory helper, never by the main window's process.)

On the gesture:
  - ensure the console is up (launch it hidden if needed, so its Flask serves the
    overlay HTML and handles /quick-new + /raise)
  - show the overlay here, in this process

Runs as a LaunchAgent (RunAtLoad + KeepAlive), windowless, so it survives the console
being closed. Reuses quickaccess.py for the gesture detection.
"""
try:
    import setproctitle
    setproctitle.setproctitle("MIST Hotkey Agent")
except ImportError:
    pass  # cosmetic process name only; never block startup on it

import json
import os
import subprocess
import threading
import time
import urllib.request

import quickaccess  # shared gesture detection + config

MIST_APP = os.path.expanduser("~/Desktop/Apps/MIST Console.app")
PORT = 5014
BASE = "http://127.0.0.1:%d" % PORT
# marker that tells MIST to start with her main window hidden (quiet quick-access
# launch) — only the glowing overlay should appear, not the console window.
QUIET_MARKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".quiet-launch")

# overlay geometry — must match the box in static/quickbox.html (see desktop.py notes)
QW, QH = 1280, 460
QH_EXPANDED = QH + 340
Q_BOTTOM_MARGIN = 0
SHOT_PATH = "/tmp/mist-quickshot.png"

# how to ask each browser for its current page URL (for the link button)
BROWSER_SCRIPTS = {
    "Safari": 'tell application "Safari" to return URL of front document',
    "Google Chrome": 'tell application "Google Chrome" to return URL of active tab of front window',
    "Brave Browser": 'tell application "Brave Browser" to return URL of active tab of front window',
    "Microsoft Edge": 'tell application "Microsoft Edge" to return URL of active tab of front window',
    "Arc": 'tell application "Arc" to return URL of active tab of front window',
    "Vivaldi": 'tell application "Vivaldi" to return URL of active tab of front window',
    "Chromium": 'tell application "Chromium" to return URL of active tab of front window',
}

_panel = None
_webview = None
_handler = None
_handler_class = None
_panel_class = None
_summon_app = None   # frontmost app at the moment the hotkey fired (for URL attach)


def _mist_up():
    try:
        urllib.request.urlopen(BASE + "/", timeout=0.6)
        return True
    except Exception:
        return False


def _frontmost_app():
    """The app that's frontmost right now — for the URL attachment. NSWorkspace
    needs no permission, and at gesture time the browser is still frontmost."""
    try:
        from AppKit import NSWorkspace
        a = NSWorkspace.sharedWorkspace().frontmostApplication()
        return a.localizedName() if a is not None else None
    except Exception:
        return None


def _post(path, body=None):
    try:
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(
            BASE + path, data=data, method="POST",
            headers={"Content-Type": "application/json"})
        r = urllib.request.urlopen(req, timeout=3)
        return getattr(r, "status", 200) == 200
    except Exception:
        return False


def _run_main(fn):
    """Run fn on the main thread (AppKit is main-thread only)."""
    try:
        from Foundation import NSOperationQueue
        NSOperationQueue.mainQueue().addOperationWithBlock_(fn)
    except Exception:
        fn()


# ---- the glowing quick-entry overlay: a non-activating NSPanel + WKWebView --------
def _ensure_webview():
    """Build the WKWebView + message handler ONCE; it's reused across panels so we
    never reload quickbox.html on each summon."""
    from WebKit import WKWebView, WKWebViewConfiguration, WKUserContentController
    from Foundation import NSObject, NSURL, NSURLRequest, NSMakeRect
    global _webview, _handler, _handler_class
    if _webview is not None:
        return

    if _handler_class is None:
        class _MistHandler(NSObject):
            def userContentController_didReceiveScriptMessage_(self, ucc, message):
                try:
                    action = str(message.body())
                    print("quick-access: panel msg:", action, flush=True)
                    if action == "surface":
                        _hide_panel()
                        _post("/raise")            # console surfaces its own window
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
        NSURL.URLWithString_(BASE + "/quickbox.html")))
    _webview = wv


def _make_panel():
    """Create a FRESH NSPanel for this summon and attach the (reused) webview.

    A recycled panel goes stale across a Spaces reshuffle (e.g. restarting the console
    tears down/recreates its fullscreen Space), after which orderFront drops it on the
    desktop *behind* a fullscreen app instead of on the active Space (verified via
    isOnActiveSpace going True->False). A freshly created panel reliably joins whatever
    Space is active right now, so we rebuild it every time and reuse only the webview."""
    from AppKit import (NSPanel, NSColor, NSBackingStoreBuffered,
                        NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel,
                        NSScreenSaverWindowLevel,
                        NSWindowCollectionBehaviorCanJoinAllSpaces,
                        NSWindowCollectionBehaviorFullScreenAuxiliary,
                        NSWindowCollectionBehaviorStationary)
    from Foundation import NSMakeRect
    global _panel, _panel_class

    if _panel_class is None:
        # A borderless window returns canBecomeKeyWindow=False by default, so it
        # can't take keyboard focus — override it (and main) to True.
        class _MistPanel(NSPanel):
            def canBecomeKeyWindow(self):
                return True

            def canBecomeMainWindow(self):
                return True
        _panel_class = _MistPanel

    _ensure_webview()

    old = _panel
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
    panel.setReleasedWhenClosed_(False)
    panel.setContentView_(_webview)   # moves the webview off the old panel
    _panel = panel
    if old is not None:
        try:
            old.orderOut_(None)
        except Exception:
            pass


def _position_panel():
    from AppKit import NSScreen
    from Foundation import NSMakeRect
    vf = NSScreen.mainScreen().visibleFrame()
    _panel.setFrame_display_(NSMakeRect(
        vf.origin.x + (vf.size.width - QW) / 2, vf.origin.y + Q_BOTTOM_MARGIN,
        QW, QH), True)


def _set_panel_height(h):
    if not _panel:
        return
    from Foundation import NSMakeRect
    f = _panel.frame()
    _panel.setFrame_display_animate_(
        NSMakeRect(f.origin.x, f.origin.y, QW, h), True, False)


def _show_panel():
    try:
        _make_panel()          # FRESH panel each summon -> reliably joins the active Space
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


def _take_screenshot():
    """Hide the overlay, let the user drag a selection, then re-show with it attached."""
    def _work():
        _run_main(_hide_panel)
        time.sleep(0.2)
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
    """If the app frontmost when summoned is a browser, attach its current URL."""
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


def _handle():
    global _summon_app
    _summon_app = _frontmost_app()             # capture before anything steals focus
    if not _mist_up():
        try:
            open(QUIET_MARKER, "w").close()    # launch with the console window hidden
        except Exception:
            pass
        subprocess.Popen(["open", MIST_APP])   # console is down → open it
        for _ in range(50):                     # wait up to ~25s for its Flask to bind
            if _mist_up():
                break
            time.sleep(0.5)
    if _mist_up():
        _run_main(_show_panel)                  # draw the overlay here, in this process


def on_hotkey():
    threading.Thread(target=_handle, daemon=True).start()


def _reload_loop():
    # pick up enable/disable changes made in MIST's settings
    while True:
        time.sleep(10)
        quickaccess.load()


_status_item = None
_menu_target = None
LOGO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "mist-logo.png")


def _setup_status_item():
    """A menu bar icon (the MIST diamond) whose menu adjusts the quick-access
    settings. Lives in the agent so it's present even when MIST is closed."""
    from AppKit import (NSStatusBar, NSVariableStatusItemLength, NSImage,
                        NSMenu, NSMenuItem)
    from Foundation import NSObject, NSMakeSize
    global _status_item, _menu_target

    ON, OFF = 1, 0   # NSControlStateValueOn / Off

    class _MenuTarget(NSObject):
        def toggleEnabled_(self, sender):
            cfg = quickaccess.get()
            cfg["enabled"] = not cfg.get("enabled", True)
            quickaccess.save(cfg)
            sender.setState_(ON if cfg["enabled"] else OFF)

        def openMist_(self, sender):
            subprocess.Popen(["open", MIST_APP])

        def grantAx_(self, sender):
            quickaccess.request_permission()

    _menu_target = _MenuTarget.alloc().init()

    item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
    button = item.button()
    img = NSImage.alloc().initWithContentsOfFile_(LOGO)
    if img is not None:
        img.setSize_(NSMakeSize(20, 16))   # ~menu-bar height, keeps the diamond's aspect
        img.setTemplate_(True)             # render in the menu-bar tint (crisp in light+dark)
        button.setImage_(img)
    else:
        button.setTitle_("✦")          # ✦ fallback
    button.setToolTip_("MIST quick access")

    menu = NSMenu.alloc().init()

    def _item(title, sel, target=_menu_target, enabled=True, state=None):
        mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, sel, "")
        if sel:
            mi.setTarget_(target)
        mi.setEnabled_(enabled)
        if state is not None:
            mi.setState_(state)
        return mi

    menu.addItem_(_item("MIST Quick Access", None, enabled=False))
    menu.addItem_(_item("Enabled", "toggleEnabled:",
                        state=ON if quickaccess.get().get("enabled", True) else OFF))
    menu.addItem_(_item("Gesture:  Double-tap ⌥ Option", None, enabled=False))
    menu.addItem_(NSMenuItem.separatorItem())
    menu.addItem_(_item("Grant Accessibility…", "grantAx:"))
    menu.addItem_(_item("Open MIST Console", "openMist:"))
    item.setMenu_(menu)
    _status_item = item
    print("mist-hotkey-agent: status item installed", flush=True)


def main():
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
    from PyObjCTools import AppHelper

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # no dock icon

    quickaccess.load()
    if quickaccess.ax_trusted() is not True:
        quickaccess.request_permission()       # register + prompt for Accessibility
    quickaccess.install(on_hotkey)
    try:
        _setup_status_item()
    except Exception as e:
        print("mist-hotkey-agent: status item failed:", e, flush=True)
    threading.Thread(target=_reload_loop, daemon=True).start()

    print("mist-hotkey-agent: running, ax_trusted =", quickaccess.ax_trusted(), flush=True)
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
