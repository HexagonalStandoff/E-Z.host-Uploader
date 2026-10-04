"""
EZ Screenshot Uploader
----------------------
Watches the Windows clipboard. When you take a screenshot (Win+Shift+S or
PrintScreen), it uploads the image to e-z.host and copies the resulting URL to
your clipboard, ready to paste.

Build: see build.bat
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
import webbrowser
import tkinter as tk
from tkinter import ttk, messagebox

import requests
import pyperclip
import pystray
from PIL import Image, ImageGrab, ImageDraw, ImageTk

try:
    import keyring
except Exception:  # keyring is optional; config file is the fallback.
    keyring = None

try:
    import winreg
except ImportError:  # not available on non-Windows systems.
    winreg = None

APP_NAME = "EZ Screenshot Uploader"
APP_ID = "EZScreenshotUploader"
UPLOAD_URL = "https://api.e-z.host/files"  # Upload endpoint for screenshots.
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
    "api_key_fallback": "",  # Used only when Windows Credential Manager is unavailable.
}

user32 = ctypes.windll.user32 if os.name == "nt" else None


# --------------------------------------------------------------------------
# Config + API key storage
# --------------------------------------------------------------------------
class Config:
    def __init__(self):
        self.data = dict(DEFAULTS)
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as config_file:
                self.data.update(json.load(config_file))
        except Exception:
            pass

    def save(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as config_file:
            json.dump(self.data, config_file, indent=2)

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    # The upload key is stored in Windows Credential Manager instead of plain text on disk.
    def get_key(self):
        if keyring:
            try:
                stored_key = keyring.get_password(KEYRING_SERVICE, "api_key")
                if stored_key:
                    return stored_key
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


# Start with Windows
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _launch_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pythonw_path = sys.executable.replace("python.exe", "pythonw.exe")
    return f'"{pythonw_path}" "{os.path.abspath(__file__)}"'


def set_startup(enabled):
    if not winreg:
        return
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as run_key:
        if enabled:
            winreg.SetValueEx(run_key, APP_ID, 0, winreg.REG_SZ, _launch_command())
        else:
            try:
                winreg.DeleteValue(run_key, APP_ID)
            except FileNotFoundError:
                pass



# Tray icon
def make_icon(size=64, paused=False):
    icon_image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas = ImageDraw.Draw(icon_image)
    background_color = (110, 110, 120, 255) if paused else (99, 102, 241, 255)
    canvas.rounded_rectangle((2, 2, size - 3, size - 3), radius=size // 4, fill=background_color)
    canvas.polygon([
        (size * 0.5, size * 0.2),
        (size * 0.24, size * 0.5),
        (size * 0.4, size * 0.5),
        (size * 0.4, size * 0.78),
        (size * 0.6, size * 0.78),
        (size * 0.6, size * 0.5),
        (size * 0.76, size * 0.5),
    ], fill=(255, 255, 255, 255))
    return icon_image


# --------------------------------------------------------------------------
# Main app
# --------------------------------------------------------------------------
class App:
    def __init__(self):
        self.cfg = Config()
        self.api_key = self.cfg.get_key()
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.history = []  # Each item: (timestamp, success, text)

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

        frame_padding = {"padx": 14, "pady": 6}
        main_frame = ttk.Frame(self.root, padding=6)
        main_frame.grid()

        # --- API key section ---
        api_key_frame = ttk.LabelFrame(main_frame, text="Upload key", padding=10)
        api_key_frame.grid(row=0, column=0, sticky="ew", **frame_padding)
        self.key_var = tk.StringVar(value=self.api_key)
        self.key_entry = ttk.Entry(api_key_frame, textvariable=self.key_var, show="•", width=44)
        self.key_entry.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.key_entry.bind("<Return>", lambda event: self.save_key())
        self.show_key_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            api_key_frame,
            text="Show key",
            variable=self.show_key_var,
            command=lambda: self.key_entry.config(show="" if self.show_key_var.get() else "•"),
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))

        key_buttons = ttk.Frame(api_key_frame)
        key_buttons.grid(row=1, column=1, sticky="e", pady=(6, 0))
        ttk.Button(key_buttons, text="Paste", width=7, command=self.paste_key).pack(side="left", padx=2)
        ttk.Button(key_buttons, text="Save", width=7, command=self.save_key).pack(side="left", padx=2)
        ttk.Label(
            api_key_frame,
            text="Stored securely in Windows Credential Manager.",
            foreground="#777",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        api_key_frame.columnconfigure(0, weight=1)

        # --- Options section ---
        options_frame = ttk.LabelFrame(main_frame, text="Options", padding=10)
        options_frame.grid(row=1, column=0, sticky="ew", **frame_padding)
        self.option_vars = {}
        option_settings = [
            ("enabled", "Auto-upload screenshots"),
            ("start_with_windows", "Start with Windows"),
            ("start_in_tray", "Start minimized to tray"),
            ("close_to_tray", "Closing the window keeps it running in the tray"),
            ("notifications", "Show a notification when the link is copied"),
            ("open_in_browser", "Also open the link in my browser"),
        ]
        for index, (setting_name, label) in enumerate(option_settings):
            option_var = tk.BooleanVar(value=self.cfg[setting_name])
            self.option_vars[setting_name] = option_var
            ttk.Checkbutton(options_frame, text=label, variable=option_var, command=self.options_changed).grid(
                row=index,
                column=0,
                sticky="w",
                pady=1,
            )

        # --- Status + history ---
        status_frame = ttk.LabelFrame(main_frame, text="Status", padding=10)
        status_frame.grid(row=2, column=0, sticky="ew", **frame_padding)
        self.status_lbl = ttk.Label(status_frame, text="", wraplength=380, justify="left")
        self.status_lbl.grid(row=0, column=0, sticky="w")

        self.listbox = tk.Listbox(status_frame, height=6, activestyle="none", width=58)
        self.listbox.grid(row=1, column=0, pady=(8, 4), sticky="ew")
        self.listbox.bind("<Double-Button-1>", lambda event: self.copy_selected())
        action_row = ttk.Frame(status_frame)
        action_row.grid(row=2, column=0, sticky="ew")
        ttk.Button(action_row, text="Copy selected link", command=self.copy_selected).pack(side="left")
        ttk.Button(
            action_row,
            text="Upload clipboard now",
            command=lambda: threading.Thread(target=self.upload_clipboard_now, daemon=True).start(),
        ).pack(side="left", padx=6)
        ttk.Button(action_row, text="Hide to tray", command=self.root.withdraw).pack(side="right")

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
        for option_name, option_var in self.option_vars.items():
            self.cfg[option_name] = bool(option_var.get())
        self.cfg.save()
        try:
            set_startup(self.cfg["start_with_windows"])
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Couldn't update startup setting:\n{exc}")
        self.refresh_tray()

    def set_status(self, text, kind="info"):
        status_colors = {"ok": "#15803d", "warn": "#b45309", "err": "#b91c1c", "info": "#333"}
        self.status_lbl.config(text=text, foreground=status_colors.get(kind, "#333"))

    def add_history(self, ok, text):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.history.insert(0, (timestamp, ok, text))
        self.history = self.history[:25]
        self.listbox.delete(0, "end")
        for entry_time, success, entry_text in self.history:
            self.listbox.insert("end", f"{entry_time}  {'✔' if success else '✘'}  {entry_text}")

    def copy_selected(self):
        selection = self.listbox.curselection()
        selected_index = selection[0] if selection else 0
        if selected_index < len(self.history) and self.history[selected_index][1]:
            pyperclip.copy(self.history[selected_index][2])
            self.set_status("Link copied to clipboard.", "ok")
            # Ignore our own copied URL so the clipboard watcher does not treat it as a new screenshot.
            self.swallow_clipboard_change()

    # Tray
    def build_tray(self):
        self.tray = pystray.Icon(
            APP_ID,
            make_icon(64),
            APP_NAME,
            menu=pystray.Menu(
                pystray.MenuItem("Open", lambda _, __: self.root.after(0, self.show_window), default=True),
                pystray.MenuItem(
                    "Auto-upload screenshots",
                    lambda _, __: self.root.after(0, self.toggle_enabled),
                    checked=lambda _: self.cfg["enabled"],
                ),
                pystray.MenuItem(
                    "Upload clipboard now",
                    lambda _, __: threading.Thread(target=self.upload_clipboard_now, daemon=True).start(),
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", lambda _, __: self.root.after(0, self.quit)),
            ),
        )
        self.tray.run_detached()

    def refresh_tray(self):
        try:
            self.tray.icon = make_icon(64, paused=not self.cfg["enabled"])
            self.tray.title = APP_NAME + ("" if self.cfg["enabled"] else " (paused)")
            self.tray.update_menu()
        except Exception:
            pass

    def toggle_enabled(self):
        self.option_vars["enabled"].set(not self.cfg["enabled"])
        self.options_changed()
        self.set_status("Auto-upload is " + ("ON." if self.cfg["enabled"] else "paused."), "ok" if self.cfg["enabled"] else "warn")

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
        self.stop_event.set()
        try:
            self.tray.stop()
        except Exception:
            pass
        self.root.destroy()

    def notify(self, message):
        if self.cfg["notifications"]:
            try:
                self.tray.notify(message, APP_NAME)
            except Exception:
                pass

    # Event pump (thread -> UI)
    def pump_events(self):
        try:
            while True:
                kind, success, text = self.events.get_nowait()
                if kind == "status":
                    self.set_status(text, "ok" if success else "err")
                elif kind == "history":
                    self.add_history(success, text)
        except queue.Empty:
            pass
        if not self.stop_event.is_set():
            self.root.after(200, self.pump_events)

    # Windows exposes a clipboard sequence number that changes whenever the clipboard content changes.
    def clip_seq(self):
        return user32.GetClipboardSequenceNumber() if user32 else 0

    def swallow_clipboard_change(self):
        self._ignore_sequence = self.clip_seq()

    def read_clipboard_image(self):
        for _ in range(8):  # The snipping tool can briefly hold the clipboard lock.
            try:
                clipboard_data = ImageGrab.grabclipboard()
                if isinstance(clipboard_data, Image.Image):
                    return clipboard_data
                return None
            except Exception:
                time.sleep(0.1)
        return None

    def watch_clipboard(self):
        self._ignore_sequence = None
        last_sequence = self.clip_seq()
        while not self.stop_event.is_set():
            time.sleep(0.25)
            current_sequence = self.clip_seq()
            if current_sequence == last_sequence:
                continue
            last_sequence = current_sequence
            if not self.cfg["enabled"] or current_sequence == self._ignore_sequence:
                continue
            time.sleep(0.15)  # Give the snipping tool time to finish placing the screenshot on the clipboard.
            screenshot = self.read_clipboard_image()
            if screenshot is not None:
                self.upload_image(screenshot)
                last_sequence = self.clip_seq()  # Skip the sequence change caused by our own copied URL.

    def upload_clipboard_now(self):
        screenshot = self.read_clipboard_image()
        if screenshot is None:
            self.events.put(("status", False, "There's no image on the clipboard."))
            return
        self.upload_image(screenshot)

    # ---------------- Upload ----------------
    def upload_image(self, image):
        if not self.api_key:
            self.events.put(("status", False, "No upload key set. Open the app and add it."))
            self.notify("No upload key set.")
            return

        self.events.put(("status", True, "Uploading screenshot…"))
        try:
            image_buffer = io.BytesIO()
            image.save(image_buffer, format="PNG")
            image_buffer.seek(0)
            uploaded_file_name = datetime.datetime.now().strftime("screenshot_%Y%m%d_%H%M%S.png")

            # POST the image file to e-z.host using the configured API key.
            response = requests.post(
                UPLOAD_URL,
                headers={"key": self.api_key},
                files={"file": (uploaded_file_name, image_buffer, "image/png")},
                timeout=60,
            )
            if response.status_code in (401, 403):
                raise RuntimeError("Upload key was rejected (check it's correct).")
            response.raise_for_status()
            payload = response.json()

            # e-z.host returns the file URL in one of several possible response fields.
            uploaded_url = (
                payload.get("imageUrl")
                or payload.get("imageURL")
                or payload.get("url")
                or payload.get("rawUrl")
            )
            if not uploaded_url:
                raise RuntimeError(f"No image URL in response: {str(payload)[:150]}")

            pyperclip.copy(uploaded_url)
            self._ignore_sequence = self.clip_seq()
            self.events.put(("history", True, uploaded_url))
            self.events.put(("status", True, "Uploaded! Link copied to clipboard."))
            self.notify("Link copied to clipboard.")
            if self.cfg["open_in_browser"]:
                webbrowser.open(uploaded_url)
        except requests.exceptions.ConnectionError:
            self._fail("Couldn't reach e-z.host. Check your internet connection.")
        except requests.exceptions.Timeout:
            self._fail("Upload timed out.")
        except Exception as exc:
            self._fail(str(exc))

    def _fail(self, message):
        self.events.put(("history", False, message))
        self.events.put(("status", False, message))
        self.notify("Upload failed: " + message)

    def run(self):
        self.root.mainloop()

# Single-instance guard
def ensure_single_instance():
    if os.name != "nt":
        return True
    ctypes.windll.kernel32.CreateMutexW(None, False, f"Local\\{APP_ID}Mutex")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS

if __name__ == "__main__":
    if not ensure_single_instance():
        root_window = tk.Tk()
        root_window.withdraw()
        messagebox.showinfo(APP_NAME, "Already running. Look for it in the system tray.")
        sys.exit(0)
    App().run()