#!/usr/bin/env python3
"""System-tray companion for mini-everything.

Lives in the Windows notification area and drives the local search service:

  * double-click / 打开搜索窗口 -> opens the web UI in the default browser
  * 重建索引 C:/D:              -> re-enumerates that volume via USN/MFT
  * 暂停/恢复实时监听            -> stops or restarts the USN journal watcher
  * 开机自启                     -> HKCU\\...\\Run entry (uses pythonw, no console)
  * 全局热键 Ctrl+Alt+E          -> summons the search window from anywhere
  * 退出                         -> stops the owned server and the icon

If a mini-everything server is already listening on the port, the tray attaches
to it instead of starting a second index. Run with pythonw.exe for a clean
no-console experience.
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

try:
    import winreg
except ImportError:                                  # non-Windows: tray still loads
    winreg = None

import pystray
from PIL import Image, ImageDraw
from pystray import Menu, MenuItem

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "tray.log")


def log(msg):
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "MiniEverythingTray"
HOTKEY_LABEL = "Ctrl+Alt+E"
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
HOTKEY_ID = 0xB37

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


# ------------------------------------------------------------------ helpers
def http_json(base, path, payload=None, timeout=30):
    url = base + path
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as fh:
        return json.load(fh)


def make_icon_image(accent=(76, 154, 255)):
    """A folder with a magnifier, drawn at 64x64 so the tray can scale it."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 18, 42, 50), radius=7, fill=(245, 248, 252, 255))
    d.polygon([(4, 20), (18, 20), (23, 27), (4, 27)], fill=(245, 248, 252, 255))
    d.rounded_rectangle((10, 30, 36, 34), radius=2, fill=(150, 165, 185, 255))
    d.rounded_rectangle((10, 38, 30, 42), radius=2, fill=(150, 165, 185, 255))
    d.ellipse((30, 24, 58, 52), outline=accent, width=7)
    d.line((51, 45, 61, 55), fill=accent, width=7)
    return img


# -------------------------------------------------------------- autostart
def autostart_command():
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    return '"%s" "%s"' % (exe, os.path.abspath(__file__))


def is_autostart():
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, RUN_NAME)
            return value == autostart_command()
    except OSError:
        return False


def set_autostart(on):
    if winreg is None:
        return False
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if on:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass
    return is_autostart()


# ---------------------------------------------------------------- hotkey
class Hotkey:
    """RegisterHotKey on its own message-loop thread."""

    def __init__(self):
        self.enabled = False
        self.thread = None

    def toggle(self, on, callback):
        if on == self.enabled:
            return self.enabled
        if on:
            self.thread = threading.Thread(target=self._loop, args=(callback,), name="hotkey", daemon=True)
            self.enabled = True
            self.thread.start()
        else:
            tid = getattr(self, "tid", None)
            self.enabled = False
            if tid:
                user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)
            self.thread = None
        return self.enabled

    def _loop(self, callback):
        self.tid = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord("E")):
            self.enabled = False
            return
        try:
            msg = wt.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    try:
                        callback()
                    except Exception:
                        pass
        finally:
            user32.UnregisterHotKey(None, HOTKEY_ID)


# ------------------------------------------------------------------ app
class TrayApp:
    def __init__(self, port, volumes, no_serve):
        self.base = "http://127.0.0.1:%d" % port
        self.port = port
        self.volumes = volumes
        self.no_serve = no_serve
        self.owns_server = False
        self.server = None
        self.hotkey = Hotkey()
        self.hotkey_on = False
        self.last = {"count": 0, "deltas": 0, "volumes": [], "building": False}
        self.icon = pystray.Icon("mini-everything", make_icon_image(),
                                 "Mini Everything", Menu(self.menu_items))

    # ---- server
    def probe(self, timeout=1.2):
        try:
            http_json(self.base, "/api/stats", timeout=timeout)
            return True
        except Exception:
            return False

    def ensure_server(self):
        if self.probe():
            log("attached to an already running server on %s" % self.base)
            return
        if self.no_serve:
            return
        from http.server import ThreadingHTTPServer
        from webapp import Service, make_handler
        svc = Service(list(self.volumes))
        svc.build_all()
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), make_handler(svc))
        self.server.daemon_threads = True
        self.owns_server = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        log("built the index and started the server on %s" % self.base)

    # ---- actions
    def open_search(self, *_):
        webbrowser.open(self.base + "/")

    def rebuild(self, letter):
        def run(*_):
            try:
                http_json(self.base, "/api/rebuild", {"drive": letter})
            except Exception:
                pass
        return run

    def toggle_listen(self, *_):
        try:
            st = http_json(self.base, "/api/stats")
            paused = any(v.get("paused") for v in st.get("volumes", []))
            http_json(self.base, "/api/listen", {"enabled": paused})
            log("live listening -> %s" % ("resumed" if paused else "paused"))
        except Exception:
            pass

    def toggle_autostart(self, *_):
        try:
            log("autostart -> %s" % set_autostart(not is_autostart()))
        except Exception as exc:
            log("autostart failed: %s" % exc)

    def toggle_hotkey(self, *_):
        self.hotkey_on = self.hotkey.toggle(not self.hotkey_on, self.open_search)

    def quit(self, *_):
        log("quitting (owned_server=%s)" % self.owns_server)
        try:
            self.hotkey.toggle(False, None)
        except Exception:
            pass
        if self.owns_server and self.server is not None:
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        self.icon.stop()

    # ---- menu
    def status_text(self, item=None):
        st = self.last
        if st.get("building"):
            return "索引构建中…"
        return "%d 项 · 增量 %d · %s" % (
            st.get("count", 0), st.get("deltas", 0),
            "监听中" if not any(v.get("paused") for v in st.get("volumes", [])) else "已暂停")

    def menu_items(self):
        st = self.last
        paused = any(v.get("paused") for v in st.get("volumes", []))
        letters = [v["letter"] for v in st.get("volumes", [])] or list(self.volumes)
        items = [
            MenuItem("打开搜索窗口", self.open_search, default=True),
        ]
        for letter in letters:
            items.append(MenuItem("重建索引 %s: 的索引" % letter, self.rebuild(letter)))
        items += [
            Menu.SEPARATOR,
            MenuItem("恢复实时监听" if paused else "暂停实时监听", self.toggle_listen),
            Menu.SEPARATOR,
            MenuItem("开机自启", self.toggle_autostart, checked=lambda i: is_autostart()),
            MenuItem("全局热键 %s" % HOTKEY_LABEL, self.toggle_hotkey,
                     checked=lambda i: self.hotkey_on),
            Menu.SEPARATOR,
            MenuItem(self.status_text, None, enabled=False),
            Menu.SEPARATOR,
            MenuItem("退出", self.quit),
        ]
        return items

    # ---- background refresh
    def refresh(self):
        while True:
            try:
                st = http_json(self.base, "/api/stats", timeout=5)
                self.last = st
                vol = " ".join("%s:%d%s" % (v["letter"], v["count"],
                                            " (暂停)" if v.get("paused") else "")
                               for v in st.get("volumes", []))
                self.icon.title = "Mini Everything — %s\n增量 %d" % (vol, st.get("deltas", 0))
            except Exception:
                pass
            time.sleep(4)

    def setup(self, icon):
        icon.visible = True
        log("tray icon visible=%s  (shell accepted Shell_NotifyIcon)  pid=%d"
            % (icon.visible, os.getpid()))
        threading.Thread(target=self.refresh, daemon=True).start()

    def run(self):
        self.ensure_server()
        self.hotkey_on = self.hotkey.toggle(True, self.open_search)
        log("global hotkey %s registered=%s" % (HOTKEY_LABEL, self.hotkey_on))
        log("open the search window at %s/  (double-click the tray icon)" % self.base)
        self.icon.run(setup=self.setup)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--volumes", default="CD")
    ap.add_argument("--no-serve", action="store_true",
                    help="never start a server; only drive one that is already running")
    ap.add_argument("--open", action="store_true", help="open the search window on start")
    args = ap.parse_args()
    app = TrayApp(args.port, [c for c in args.volumes.upper() if c.isalpha()], args.no_serve)
    if args.open:
        threading.Timer(1.5, app.open_search).start()
    app.run()


if __name__ == "__main__":
    main()
