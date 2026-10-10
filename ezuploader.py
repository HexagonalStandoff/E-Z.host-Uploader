"""
EZ Screenshot Uploader
----------------------
Watches the Windows clipboard. When you take a screenshot (Win+Shift+S or
PrintScreen) it uploads the image to e-z.host and puts the resulting URL on
your clipboard, ready to paste.

Build:  see build.bat
"""
import sys
import os
import io
import json
import time
import queue
import ctypes
import datetime
import threading
import mimetypes
import webbrowser
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import requests
import pyperclip
import pystray
from PIL import Image, ImageGrab, ImageDraw, ImageTk

try:
    import keyring
except Exception:  # keyring is optional, we fall back to the config file
    keyring = None

try:
    import winreg
except ImportError:  # not on Windows
    winreg = None

APP_NAME = "EZ Screenshot Uploader"
APP_ID = "EZScreenshotUploader"
UPLOAD_URL = "https://api.e-z.host/files"          # uploadURL
MAX_UPLOAD_BYTES = 100 * 1024 * 1024               # 100 MB limit for file uploads
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_ID)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
KEYRING_SERVICE = APP_ID

DEFAULTS = {
    "enabled": True,
    "start_with_windows": False,
    "start_in_tray": False,
    "close_to_tray": True,
    "notifications": True,
    "open_in_browser": False,
    "api_key_fallback": "",   # only used if Windows Credential Manager fails
}

user32 = ctypes.windll.user32 if os.name == "nt" else None


# --------------------------------------------------------------------------
# Config + API key storage
# --------------------------------------------------------------------------
class Config:
    def __init__(self):
        self.data = dict(DEFAULTS)
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                self.data.update(json.load(f))
        except Exception:
            pass

    def save(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)

    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        self.data[k] = v

    # The key lives in Windows Credential Manager, not in plain text on disk.
    def get_key(self):
        if keyring:
            try:
                k = keyring.get_password(KEYRING_SERVICE, "api_key")
                if k:
                    return k
            except Exception:
                pass
        return self.data.get("api_key_fallback", "")

    def set_key(self, key):
        if keyring:
            try:
                if key:
                    keyring.set_password(KEYRING_SERVICE, "api_key", key)
                else:
                    try:
                        keyring.delete_password(KEYRING_SERVICE, "api_key")
                    except Exception:
                        pass
                self.data["api_key_fallback"] = ""
                self.save()
                return
            except Exception:
                pass
        self.data["api_key_fallback"] = key
        self.save()


# --------------------------------------------------------------------------
# Start with Windows
# --------------------------------------------------------------------------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _launch_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pyw = sys.executable.replace("python.exe", "pythonw.exe")
    return f'"{pyw}" "{os.path.abspath(__file__)}"'


def set_startup(enabled):
    if not winreg:
        return
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, APP_ID, 0, winreg.REG_SZ, _launch_command())
        else:
            try:
                winreg.DeleteValue(k, APP_ID)
            except FileNotFoundError:
                pass


# --------------------------------------------------------------------------
# Icon
# --------------------------------------------------------------------------
def make_icon(size=64, paused=False):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bg = (110, 110, 120, 255) if paused else (99, 102, 241, 255)
    d.rounded_rectangle((2, 2, size - 3, size - 3), radius=size // 4, fill=bg)
    s = size
    d.polygon([(s * .5, s * .2), (s * .24, s * .5), (s * .4, s * .5),
               (s * .4, s * .78), (s * .6, s * .78), (s * .6, s * .5),
               (s * .76, s * .5)], fill=(255, 255, 255, 255))
    return img


# --------------------------------------------------------------------------
# The app
# --------------------------------------------------------------------------
class App:
    def __init__(self):
        self.cfg = Config()
        self.api_key = self.cfg.get_key()
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.history = []  # (time, ok, text)

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.resizable(False, False)
        self._icon_photo = ImageTk.PhotoImage(make_icon(64))
        self.root.iconphoto(True, self._icon_photo)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.build_ui()
        self.build_tray()

        threading.Thread(target=self.watch_clipboard, daemon=True).start()
        self.root.after(200, self.pump_events)

        if not self.api_key:
            self.set_status("Enter your e-z.host upload key to get started.", "warn")
        else:
            self.set_status("Ready. Take a screenshot with Win+Shift+S or PrintScreen.", "ok")

        if self.cfg["start_in_tray"] and self.api_key:
            self.root.withdraw()

    # ---------------- UI ----------------
    def build_ui(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            style.theme_use("clam")

        pad = {"padx": 14, "pady": 6}
        main = ttk.Frame(self.root, padding=6)
        main.grid()

        # --- Key section
        key_box = ttk.LabelFrame(main, text="Upload key", padding=10)
        key_box.grid(row=0, column=0, sticky="ew", **pad)
        self.key_var = tk.StringVar(value=self.api_key)
        self.key_entry = ttk.Entry(key_box, textvariable=self.key_var, show="•", width=44)
        self.key_entry.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.key_entry.bind("<Return>", lambda e: self.save_key())
        self.show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(key_box, text="Show key", variable=self.show_var,
                        command=lambda: self.key_entry.config(
                            show="" if self.show_var.get() else "•")
                        ).grid(row=1, column=0, sticky="w", pady=(6, 0))
        btns = ttk.Frame(key_box)
        btns.grid(row=1, column=1, sticky="e", pady=(6, 0))
        ttk.Button(btns, text="Paste", width=7, command=self.paste_key).pack(side="left", padx=2)
        ttk.Button(btns, text="Save", width=7, command=self.save_key).pack(side="left", padx=2)
        ttk.Label(key_box, text="Stored securely in Windows Credential Manager.",
                  foreground="#777").grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        key_box.columnconfigure(0, weight=1)

        # --- Options
        opt = ttk.LabelFrame(main, text="Options", padding=10)
        opt.grid(row=1, column=0, sticky="ew", **pad)
        self.vars = {}
        options = [
            ("enabled", "Auto-upload screenshots"),
            ("start_with_windows", "Start with Windows"),
            ("start_in_tray", "Start minimized to tray"),
            ("close_to_tray", "Closing the window keeps it running in the tray"),
            ("notifications", "Show a notification when the link is copied"),
            ("open_in_browser", "Also open the link in my browser"),
        ]
        for i, (key, label) in enumerate(options):
            v = tk.BooleanVar(value=self.cfg[key])
            self.vars[key] = v
            ttk.Checkbutton(opt, text=label, variable=v,
                            command=self.options_changed).grid(row=i, column=0, sticky="w", pady=1)

        # --- Status + history
        st = ttk.LabelFrame(main, text="Status", padding=10)
        st.grid(row=2, column=0, sticky="ew", **pad)
        self.status_lbl = ttk.Label(st, text="", wraplength=380, justify="left")
        self.status_lbl.grid(row=0, column=0, sticky="w")

        self.listbox = tk.Listbox(st, height=6, activestyle="none", width=58)
        self.listbox.grid(row=1, column=0, pady=(8, 4), sticky="ew")
        self.listbox.bind("<Double-Button-1>", lambda e: self.copy_selected())
        row = ttk.Frame(st)
        row.grid(row=2, column=0, sticky="ew")
        ttk.Button(row, text="Copy selected link", command=self.copy_selected).pack(side="left")
        ttk.Button(row, text="Upload clipboard now",
                   command=lambda: threading.Thread(
                       target=self.upload_clipboard_now, daemon=True).start()
                   ).pack(side="left", padx=6)
        ttk.Button(row, text="Hide to tray", command=self.root.withdraw).pack(side="right")
        row2 = ttk.Frame(st)
        row2.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(row2, text="📁  Browse files to upload…  (max 100 MB each)",
                   command=self.browse_files).pack(fill="x")

    def paste_key(self):
        try:
            self.key_var.set(self.root.clipboard_get().strip())
        except tk.TclError:
            pass

    def save_key(self):
        key = self.key_var.get().strip()
        self.api_key = key
        self.cfg.set_key(key)
        if key:
            self.set_status("Key saved. You're good to go!", "ok")
        else:
            self.set_status("Key cleared.", "warn")

    def options_changed(self):
        for k, v in self.vars.items():
            self.cfg[k] = bool(v.get())
        self.cfg.save()
        try:
            set_startup(self.cfg["start_with_windows"])
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Couldn't update startup setting:\n{e}")
        self.refresh_tray()

    def set_status(self, text, kind="info"):
        colors = {"ok": "#15803d", "warn": "#b45309", "err": "#b91c1c", "info": "#333"}
        self.status_lbl.config(text=text, foreground=colors.get(kind, "#333"))

    def add_history(self, ok, text):
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.history.insert(0, (stamp, ok, text))
        self.history = self.history[:25]
        self.listbox.delete(0, "end")
        for stamp, ok, text in self.history:
            self.listbox.insert("end", f"{stamp}  {'✔' if ok else '✘'}  {text}")

    def copy_selected(self):
        sel = self.listbox.curselection()
        idx = sel[0] if sel else 0
        if idx < len(self.history) and self.history[idx][1]:
            pyperclip.copy(self.history[idx][2])
            self.set_status("Link copied to clipboard.", "ok")
            # don't let the watcher treat our own copy as a screenshot
            self.swallow_clipboard_change()

    # ---------------- Tray ----------------
    def build_tray(self):
        self.tray = pystray.Icon(
            APP_ID, make_icon(64), APP_NAME,
            menu=pystray.Menu(
                pystray.MenuItem("Open", lambda i, it: self.root.after(0, self.show_window), default=True),
                pystray.MenuItem("Auto-upload screenshots",
                                 lambda i, it: self.root.after(0, self.toggle_enabled),
                                 checked=lambda it: self.cfg["enabled"]),
                pystray.MenuItem("Upload files…",
                                 lambda i, it: self.root.after(0, self.browse_files)),
                pystray.MenuItem("Upload clipboard now",
                                 lambda i, it: threading.Thread(
                                     target=self.upload_clipboard_now, daemon=True).start()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", lambda i, it: self.root.after(0, self.quit)),
            ))
        self.tray.run_detached()

    def refresh_tray(self):
        try:
            self.tray.icon = make_icon(64, paused=not self.cfg["enabled"])
            self.tray.title = APP_NAME + ("" if self.cfg["enabled"] else " (paused)")
            self.tray.update_menu()
        except Exception:
            pass

    def toggle_enabled(self):
        self.vars["enabled"].set(not self.cfg["enabled"])
        self.options_changed()
        self.set_status("Auto-upload is " + ("ON." if self.cfg["enabled"] else "paused."),
                        "ok" if self.cfg["enabled"] else "warn")

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def on_close(self):
        if self.cfg["close_to_tray"]:
            self.root.withdraw()
        else:
            self.quit()

    def quit(self):
        self.stop.set()
        try:
            self.tray.stop()
        except Exception:
            pass
        self.root.destroy()

    def notify(self, msg):
        if self.cfg["notifications"]:
            try:
                self.tray.notify(msg, APP_NAME)
            except Exception:
                pass

    # ---------------- Event pump (thread -> UI) ----------------
    def pump_events(self):
        try:
            while True:
                kind, ok, text = self.events.get_nowait()
                if kind == "status":
                    self.set_status(text, "ok" if ok else "err")
                elif kind == "history":
                    self.add_history(ok, text)
        except queue.Empty:
            pass
        if not self.stop.is_set():
            self.root.after(200, self.pump_events)

    # ---------------- Clipboard watching ----------------
    def clip_seq(self):
        return user32.GetClipboardSequenceNumber() if user32 else 0

    def swallow_clipboard_change(self):
        self._ignore_seq = self.clip_seq()

    def read_clipboard_image(self):
        for _ in range(8):  # the clipboard can be briefly locked by the snipping tool
            try:
                data = ImageGrab.grabclipboard()
                if isinstance(data, Image.Image):
                    return data
                return None
            except Exception:
                time.sleep(0.1)
        return None

    def watch_clipboard(self):
        self._ignore_seq = None
        last = self.clip_seq()
        while not self.stop.is_set():
            time.sleep(0.25)
            seq = self.clip_seq()
            if seq == last:
                continue
            last = seq
            if not self.cfg["enabled"] or seq == self._ignore_seq:
                continue
            time.sleep(0.15)  # let the snipping tool finish writing
            img = self.read_clipboard_image()
            if img is not None:
                self.upload_image(img)
                last = self.clip_seq()  # skip the change caused by our own copy

    def upload_clipboard_now(self):
        img = self.read_clipboard_image()
        if img is None:
            self.events.put(("status", False, "There's no image on the clipboard."))
            return
        self.upload_image(img)

    # ---------------- Upload ----------------
    def browse_files(self):
        if not self.api_key:
            self.show_window()
            messagebox.showwarning(APP_NAME, "Add your upload key first.")
            return
        self.show_window()
        paths = filedialog.askopenfilenames(
            parent=self.root,
            title="Choose images or videos to upload",
            filetypes=[
                ("Images and videos",
                 "*.png *.jpg *.jpeg *.gif *.webp *.bmp *.mp4 *.webm *.mov *.mkv *.avi"),
                ("Images", "*.png *.jpg *.jpeg *.gif *.webp *.bmp"),
                ("Videos", "*.mp4 *.webm *.mov *.mkv *.avi"),
                ("All files", "*.*"),
            ],
        )
        if paths:
            threading.Thread(target=self.upload_paths, args=(list(paths),),
                             daemon=True).start()

    def upload_paths(self, paths):
        urls = []
        for i, path in enumerate(paths, 1):
            label = f"({i}/{len(paths)}) " if len(paths) > 1 else ""
            url = self.upload_path(path, label)
            if url:
                urls.append(url)
        if urls:
            pyperclip.copy("\n".join(urls))
            self._ignore_seq = self.clip_seq()
            msg = ("Link copied to clipboard." if len(urls) == 1
                   else f"{len(urls)} links copied to clipboard.")
            self.events.put(("status", True, "Done! " + msg))
            self.notify(msg)
            if self.cfg["open_in_browser"]:
                for u in urls:
                    webbrowser.open(u)

    def upload_path(self, path, label=""):
        name = os.path.basename(path)
        try:
            size = os.path.getsize(path)
        except OSError as e:
            self._fail(f"{name}: can't read file ({e})")
            return None
        if size > MAX_UPLOAD_BYTES:
            self._fail(f"{name}: {size / 1048576:.1f} MB is over the 100 MB limit.")
            return None
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        try:
            with open(path, "rb") as f:
                return self.send(name, f, mime, timeout=900,
                                 label=f"{label}Uploading {name} ({size / 1048576:.1f} MB)…")
        except OSError as e:
            self._fail(f"{name}: can't read file ({e})")
            return None

    def upload_image(self, img):
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        name = datetime.datetime.now().strftime("screenshot_%Y%m%d_%H%M%S.png")
        url = self.send(name, buf, "image/png", timeout=60, label="Uploading screenshot…")
        if url:
            pyperclip.copy(url)
            self._ignore_seq = self.clip_seq()
            self.events.put(("status", True, "Uploaded! Link copied to clipboard."))
            self.notify("Link copied to clipboard.")
            if self.cfg["open_in_browser"]:
                webbrowser.open(url)

    def send(self, name, fileobj, mime, timeout, label="Uploading…"):
        """POST one file to e-z.host. Returns the URL, or None on failure."""
        if not self.api_key:
            self._fail("No upload key set. Open the app and add it.")
            return None
        self.events.put(("status", True, label))
        try:
            # POST to uploadURL, header key: API_Key, body = the file
            resp = requests.post(
                UPLOAD_URL,
                headers={"key": self.api_key},
                files={"file": (name, fileobj, mime)},
                timeout=timeout,
            )
            if resp.status_code in (401, 403):
                raise RuntimeError("Upload key was rejected (check it's correct).")
            if resp.status_code == 413:
                raise RuntimeError("File too large for the server.")
            resp.raise_for_status()
            payload = resp.json()

            # get value for imageURL in contents of URL
            url = (payload.get("imageUrl") or payload.get("imageURL")
                   or payload.get("url") or payload.get("rawUrl"))
            if not url:
                raise RuntimeError(f"No URL in response: {str(payload)[:150]}")
            self.events.put(("history", True, url))
            return url
        except requests.exceptions.ConnectionError:
            self._fail("Couldn't reach e-z.host. Check your internet connection.")
        except requests.exceptions.Timeout:
            self._fail("Upload timed out.")
        except Exception as e:
            self._fail(f"{name}: {e}")
        return None

    def _fail(self, msg):
        self.events.put(("history", False, msg))
        self.events.put(("status", False, msg))
        self.notify("Upload failed: " + msg)

    def run(self):
        self.root.mainloop()


# --------------------------------------------------------------------------
# Single instance
# --------------------------------------------------------------------------
def ensure_single_instance():
    if os.name != "nt":
        return True
    ctypes.windll.kernel32.CreateMutexW(None, False, f"Local\\{APP_ID}Mutex")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


if __name__ == "__main__":
    if not ensure_single_instance():
        r = tk.Tk()
        r.withdraw()
        messagebox.showinfo(APP_NAME, "Already running. Look for it in the system tray.")
        sys.exit(0)
    App().run()
