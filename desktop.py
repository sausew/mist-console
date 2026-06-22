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
import signal
import socket
import subprocess
import threading
import time

import webview

import app as appmod

PORT = 5014
QUIET_MARKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".quiet-launch")
# The overlay box is 600px wide and sits at the bottom of the panel. The panel is
# deliberately much larger than the box: its extra transparent margin is what the
# box's soft glow fades into (box-shadow is clipped at the panel's bounds, so a
# tight panel shows a hard rectangular edge). ~290px of slack on the sides/top
# swallows the glow's tail; the bottom runs off the screen edge.
QW, QH = 1280, 460   # quick-entry overlay size (collapsed)
QH_EXPANDED = QH + 340   # taller, to fit the conversation picker above the box
OFFSCREEN = -6000   # where the overlay is parked when not summoned
Q_BOTTOM_MARGIN = 0   # panel hugs the screen bottom so the glow runs off the edge

_main_window = None
# Set True once the main window's close button has been hit. A pywebview window can
# close while this process keeps running (it still owns port 5014), turning the
# instance into a zombie that answers /raise but has no window to surface. Tracking
# the close lets /raise report honestly so a relaunch can evict us instead of
# deferring to a window that no longer exists.
_window_closed = False
_quick_window = None
_quiet_launch = False   # True when summoned via hotkey with the console hidden


def _activate():
    try:
        from AppKit import NSApplication
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception:
        pass


def _set_dock_icon():
    """The .app launcher execs `uv run`, so the GUI actually lives in a python
    process uv spawns — not the bundle's own process. macOS therefore gives the
    Dock tile a generic Python icon (or none), so MIST never looks "open" as
    herself. Set the running app's Dock icon explicitly. Main thread only."""
    try:
        from AppKit import NSApplication, NSImage
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "static", "mist-logo.png")
        img = NSImage.alloc().initWithContentsOfFile_(path)
        if img is not None:
            NSApplication.sharedApplication().setApplicationIconImage_(img)
    except Exception as e:
        print("dock icon set skipped:", e, flush=True)


def _main_nswindow():
    """The main console's native NSWindow. pywebview stashes it on the Window object
    as `.native`, which is far more reliable than matching by title — a WKWebView
    window can report an empty or page-derived title, so the old title scan silently
    found nothing and _fit_main_window became a no-op (the menu bar then kept eating
    the title bar). Fall back to a title/role scan only if `.native` isn't up yet."""
    try:
        if _main_window is not None and getattr(_main_window, "native", None) is not None:
            return _main_window.native
    except Exception:
        pass
    try:
        from AppKit import NSApp
        for w in (NSApp().windows() or []):
            try:
                if w.title() == "MIST Console":
                    return w
            except Exception:
                pass
    except Exception:
        pass
    return None


def _fit_main_window():
    """Keep the main window inside the screen's visible frame (below the menu bar,
    above the Dock) so its title bar / traffic-light controls are never tucked under
    the macOS menu bar. Clamp only when the window is actually out of bounds, so a
    window the user placed stays put. Returns True once it found the window. Main
    thread only."""
    try:
        from AppKit import NSScreen
        from Foundation import NSMakeRect
        win = _main_nswindow()
        if win is None:
            return False
        # Don't touch the window mid fullscreen enter/exit animation — setFrame_ there
        # corrupts the transition (and can crash). We re-clamp on DidExitFullScreen.
        if _fs_busy:
            return True
        fs = False
        try:
            from AppKit import NSWindowStyleMaskFullScreen
            fs = bool(win.styleMask() & NSWindowStyleMaskFullScreen)
        except Exception:
            pass
        # NEVER touch the frame of a fullscreen window. setFrame_ on one throws an
        # NSException that crashes the app and corrupts it into a half-fullscreen state.
        # Native fullscreen is fully supported — macOS reveals the title bar + controls
        # on hover at the top; we just leave the window alone while it's fullscreen.
        if fs:
            return True
        scr = win.screen() or NSScreen.mainScreen()
        vf = scr.visibleFrame()           # bottom-left origin; excludes menu bar + Dock
        f = win.frame()
        w_ = min(f.size.width,  vf.size.width)
        h_ = min(f.size.height, vf.size.height)
        x, y = f.origin.x, f.origin.y
        if x < vf.origin.x:
            x = vf.origin.x
        if x + w_ > vf.origin.x + vf.size.width:
            x = vf.origin.x + vf.size.width - w_
        # the title bar must not cross above the visible-frame top (the menu bar)
        vf_top = vf.origin.y + vf.size.height
        if y + h_ > vf_top:
            y = vf_top - h_
        if y < vf.origin.y:
            y = vf.origin.y
        if (abs(w_ - f.size.width) > 1 or abs(h_ - f.size.height) > 1
                or abs(x - f.origin.x) > 1 or abs(y - f.origin.y) > 1):
            win.setFrame_display_(NSMakeRect(x, y, w_, h_), True)
        return True
    except Exception as e:
        print("fit window skipped:", e, flush=True)
        return False


_win_guard_token = None   # retained NSNotificationCenter observers (else they're freed)
_fs_busy = False          # True during a fullscreen enter/exit animation


def _install_window_guard():
    """Re-clamp the main window whenever it moves or resizes, so it can never come to
    rest with its title bar (the X / minimize / fullscreen controls) tucked under the
    macOS menu bar — e.g. after a maximize, a window-manager nudge, or dragging it up
    by its background. _fit_main_window only acts when the window is actually out of
    bounds, so a normal drag does nothing until the title bar hits the menu bar, then
    it pins to just below it. setFrame triggers another move notification, but by then
    we're in bounds so it's a no-op — no feedback loop.

    Fullscreen is fully supported: we track the enter/exit animation with a _fs_busy
    flag and _fit_main_window refuses to touch the window while it's busy OR actually
    fullscreen. (Calling setFrame_ mid-transition is what was corrupting fullscreen and
    crashing the app.) On DidExitFullScreen we re-clamp so the now-windowed window sits
    below the menu bar. Main thread only; idempotent."""
    global _win_guard_token
    if _win_guard_token is not None:
        return
    win = _main_nswindow()
    if win is None:
        return
    try:
        from Foundation import NSNotificationCenter
        nc = NSNotificationCenter.defaultCenter()

        def _on_change(note):
            _fit_main_window()

        def _fs_begin(note):
            global _fs_busy
            _fs_busy = True

        def _fs_enter_done(note):
            global _fs_busy
            _fs_busy = False   # now fully fullscreen; _fit_main_window bails on the fs bit

        def _fs_exit_done(note):
            global _fs_busy
            _fs_busy = False
            _fit_main_window()  # back to windowed — clamp below the menu bar

        toks = []
        for name, fn in (("NSWindowDidMoveNotification", _on_change),
                         ("NSWindowDidResizeNotification", _on_change),
                         ("NSWindowWillEnterFullScreenNotification", _fs_begin),
                         ("NSWindowDidEnterFullScreenNotification", _fs_enter_done),
                         ("NSWindowWillExitFullScreenNotification", _fs_begin),
                         ("NSWindowDidExitFullScreenNotification", _fs_exit_done)):
            toks.append(nc.addObserverForName_object_queue_usingBlock_(name, win, None, fn))
        _win_guard_token = toks
    except Exception as e:
        print("window guard skipped:", e, flush=True)


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
                        _dismiss_panel()
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
    global _overlay_went_accessory
    try:
        if _panel is None:
            _build_panel()
        # Go Accessory so the panel can float over (and take focus inside) another
        # app's fullscreen Space. No-op if already Accessory, so no Dock flicker.
        # BUT: an Accessory (LSUIElement) app cannot own a native-fullscreen Space, so
        # flipping to Accessory while the CONSOLE itself is fullscreen tears that Space
        # down and strands the window on the desktop with the fullscreen bit still set
        # (menu bar then cuts off its top). When the console is fullscreen, stay Regular
        # — the panel still floats via its CanJoinAllSpaces + high window level.
        _overlay_went_accessory = not _main_window_is_fullscreen()
        if _overlay_went_accessory:
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


_overlay_went_accessory = False   # did the last summon flip us to Accessory?


def _main_window_is_fullscreen():
    """True if the console window is in native fullscreen (style bit set)."""
    try:
        from AppKit import NSWindowStyleMaskFullScreen
        win = _main_nswindow()
        return win is not None and bool(win.styleMask() & NSWindowStyleMaskFullScreen)
    except Exception:
        return False


def _recover_if_corrupt_fullscreen():
    """Recover the 'half-fullscreen' corruption: the window carries the fullscreen
    style bit but is sitting on the normal desktop Space (so the menu bar/Dock are
    reserved and visibleFrame is shorter than the frame). In GENUINE fullscreen the
    frame equals the visible frame; the mismatch is the tell. Exit fullscreen cleanly
    so the controls come back (then the DidExitFullScreen observer re-clamps it).
    Returns True if it kicked off a recovery."""
    win = _main_nswindow()
    if win is None:
        return False
    try:
        from AppKit import NSWindowStyleMaskFullScreen, NSScreen
        if not (win.styleMask() & NSWindowStyleMaskFullScreen):
            return False
        scr = win.screen() or NSScreen.mainScreen()
        vf = scr.visibleFrame()
        f = win.frame()
        if f.size.height > vf.size.height + 2:   # fs bit but desktop Space -> corrupt
            print("recover: half-fullscreen detected — exiting fullscreen cleanly", flush=True)
            win.toggleFullScreen_(None)
            return True
    except Exception as e:
        print("recover skipped:", e, flush=True)
    return False


def _console_visible():
    """True if the main console window is live and actually on screen. After a quiet
    (overlay-only) launch the window exists but is hidden, so we check AppKit's
    isVisible. Use the real NSWindow handle (title matching is unreliable for a
    WKWebView window — it was silently failing, so the dismiss path skipped the
    re-fit and left the window stuck under the menu bar)."""
    if _main_window is None or _window_closed:
        return False
    try:
        win = _main_nswindow()
        if win is not None:
            return bool(win.isVisible())
    except Exception:
        pass
    return False


def _fit_burst():
    """Re-clamp the main window several times over ~0.8s. A single fit races two async
    things in the overlay flow: pywebview's show() (which can reposition the window a
    tick later) and the Accessory->Regular policy flip (until it lands, visibleFrame
    can still report the menu-bar area as usable, so a too-early fit sees the window as
    'in bounds' and leaves its title bar under the menu bar). Retrying covers both."""
    _fit_main_window()
    try:
        from PyObjCTools import AppHelper
        for d in (0.05, 0.2, 0.45, 0.8):
            AppHelper.callLater(d, _fit_main_window)
    except Exception:
        pass


def _dismiss_panel():
    """Hide the overlay WITHOUT surfacing the console (Esc / click-away). Summoning
    the overlay flips us to Accessory so the panel can float over fullscreen — which
    also strips MIST's Dock tile. If the console window is up, we must flip back to
    Regular here or the open window is left as an Accessory app: no Dock 'open' dot,
    and its title bar can end up tucked under the menu bar. If only the overlay was
    ever showing (quiet session), stay Accessory — there's no window to represent."""
    _hide_panel()
    # Safety net: if anything still left the window half-fullscreen, exit cleanly.
    _recover_if_corrupt_fullscreen()
    if not _overlay_went_accessory:
        return   # we stayed Regular (console was fullscreen) — nothing to restore
    if _console_visible():
        _set_activation_policy(False)   # Regular -> Dock tile + 'open' dot return
        _set_dock_icon()                # re-assert MIST's icon after the flip
        # The policy flip + focus churn can leave the window crossing the menu bar;
        # re-clamp it once the run loop has applied the policy change.
        _fit_burst()
    else:
        _set_activation_policy(True)     # overlay-only session stays Accessory


def _surface():
    """Enter: hide the overlay, bring MIST's main window up. The main window polls
    /pending-open (~1.5s) to jump to the new chat — don't evaluate_js on the main
    thread (it deadlocks)."""
    # Always a normal Dock app once its window is up. (A cold launch from the overlay
    # starts Accessory + hidden so no Dock icon shows for an overlay-only summon; this
    # flip restores the icon when the window actually surfaces.) The overlay now lives
    # in the always-accessory hotkey agent, so the console never goes Accessory while
    # its window is shown and can't corrupt its own fullscreen.
    _set_activation_policy(False)

    def _raise():
        # Flipping Accessory->Regular doesn't take effect until the run loop spins,
        # so activating + ordering the window front on the SAME tick is a no-op and
        # the console never surfaces. Defer the raise by one tick.
        _activate()
        try:
            _main_window.restore()
            _main_window.show()
        except Exception:
            pass
        _set_dock_icon()      # (re)assert MIST's icon
        _fit_burst()          # keep the surfaced window clear of the menu bar
                              # (show() repositions a tick later, so retry)

    try:
        from PyObjCTools import AppHelper
        AppHelper.callLater(0.08, _raise)
    except Exception:
        _raise()


def _raise_main():
    """Surface the main console window from a Flask worker thread (POST /raise from
    a second .app launch). AppKit work must run on the main thread.

    Returns True only if there is a live window to surface. False means this is a
    windowless zombie (window closed, or never created) — the caller uses that to
    evict us and start a fresh console rather than deferring to a window that is
    gone."""
    if _main_window is None or _window_closed:
        return False
    try:
        from Foundation import NSOperationQueue
        NSOperationQueue.mainQueue().addOperationWithBlock_(_surface)
    except Exception:
        _surface()
    return True


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

    def pick_folder(self):
        """Native folder chooser — used by the repo card to pick a working dir."""
        try:
            win = webview.windows[0]
            res = win.create_file_dialog(webview.FOLDER_DIALOG)
            if not res:
                return ""
            return res[0] if isinstance(res, (list, tuple)) else res
        except Exception:
            return ""

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
    _set_dock_icon()   # show MIST's own icon in the Dock (not a generic Python tile)
    # The pywebview NSWindow may not exist the instant _setup runs; retry briefly,
    # then clamp it inside the visible frame so its top isn't hidden by the menu bar.
    def _try_fit(n=0):
        if _fit_main_window():
            _install_window_guard()   # keep it clamped on every later move/resize
            return
        if n > 20:
            return
        try:
            from PyObjCTools import AppHelper
            AppHelper.callLater(0.15, lambda: _try_fit(n + 1))
        except Exception:
            pass
    _try_fit()
    # The double-tap-Option hotkey is owned by the always-on mist-hotkey-agent (so
    # it works even when MIST is closed). Expose a main-thread summon hook that the
    # agent calls via POST /show-quick.
    # pywebview window methods are thread-safe (they marshal to the UI thread),
    # so the Flask worker thread can call this directly.
    appmod.show_quick = _quick_show
    # A second launch of the .app posts /raise so this instance surfaces instead
    # of a duplicate process starting (see the single-instance guard in main()).
    appmod.surface_main = _raise_main


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


def _instance_already_running(port):
    """True if a MIST console is already serving on `port`. Probing /repo (a cheap
    GET that only our app answers) avoids treating an unrelated listener as us."""
    import urllib.request
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/repo", timeout=0.6)
        return True
    except Exception:
        return False


def _raise_existing(port):
    """POST /raise to the running instance. Returns True only if it confirms a live
    window was surfaced; False if it is a windowless zombie or the call fails — the
    caller then evicts it instead of exiting into a dead 'is not open anymore' state."""
    import urllib.request
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
    """Kill whatever process is holding `port` (a zombie console with no window) so
    this launch can bind it and start a fresh console. Skips our own PID. Returns
    True once the port is free."""
    try:
        out = subprocess.run(
            ["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=3).stdout.split()
    except Exception:
        out = []
    me = os.getpid()
    pids = [int(p) for p in out if p.isdigit() and int(p) != me]
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in pids:
            try:
                os.kill(pid, sig)
            except Exception:
                pass
        # Give the port up to ~2s to free before escalating to SIGKILL.
        for _ in range(20):
            try:
                with socket.create_connection(("127.0.0.1", port), 0.1):
                    pass
            except OSError:
                return True  # connection refused → port is free
            time.sleep(0.1)
    return False


def main():
    global _main_window, _quiet_launch
    # Single instance: if a console is already up, surface it and exit instead of
    # starting a second process that can't bind the port and leaves two windows
    # fighting over /show-quick and /pending-open. A quiet (overlay) launch only
    # happens when the agent already found MIST down, so it never lands here.
    #
    # But only defer if the running instance confirms it actually surfaced a live
    # window. A windowless zombie (window closed, process still holding 5014) answers
    # /repo but can't raise anything — deferring to it would exit into a dead "MIST
    # Console is not open anymore" with no window. In that case, evict it and start
    # fresh so a relaunch always yields a real window.
    if _instance_already_running(PORT):
        if _raise_existing(PORT):
            return
        if not _evict_port(PORT):
            return  # couldn't free the port; bail rather than crash on bind
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
    # Mark the instance windowless when the user closes the window, so a later /raise
    # reports honestly (see _raise_main / the single-instance guard in main()).
    def _on_closed():
        global _window_closed
        _window_closed = True
    try:
        _main_window.events.closed += _on_closed
    except Exception:
        pass
    # The quick-entry overlay is a native NSPanel (built lazily in _show_panel),
    # not a pywebview window — only that can float over fullscreen apps.
    webview.start(_on_start)


if __name__ == "__main__":
    main()
