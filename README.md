# EZ Screenshot Uploader

Take a screenshot, get a link. A tiny Windows tray app that automatically uploads your screenshots to [e-z.host](https://e-z.host) and copies the URL to your clipboard.

No more clunky capture configs. Just use the screenshot shortcuts you already know:

- **Win + Shift + S** (Snipping Tool)
- **Print Screen**

A moment later, the link is on your clipboard, ready to paste.

> **Note:** This is an unofficial community tool and is not affiliated with e-z.host. You need your own e-z.host upload key.

---

## Features

- **Automatic uploads** from `Win+Shift+S` and `PrintScreen`, with no extra hotkeys to learn
- **Link copied to clipboard** as soon as the upload finishes
- **Simple GUI** for entering your upload key
- **Secure key storage** in Windows Credential Manager (not saved as plain text)
- **Start with Windows** option
- **Start minimized to tray** option
- **Tray icon** with quick controls (open, pause/resume, upload clipboard now, quit)
- **Toast notifications** when a link is ready or an upload fails
- **Upload history** for the session; double-click an entry to re-copy its link
- **Open in browser** option after upload
- **Single instance**, so you can't accidentally run two copies

---

## How it works

Both `Win+Shift+S` and `PrintScreen` place the screenshot on the clipboard instead of saving a file. The app watches the clipboard for new images and handles each one like this:

```
Screenshot lands on clipboard
        │
        ▼
Read the image from the clipboard (PNG, in memory)
        │
        ▼
POST https://api.e-z.host/files
   Header: key: <your upload key>
   Body:   the screenshot as a multipart file
        │
        ▼
Read "imageUrl" from the JSON response
        │
        ▼
Copy the URL to the clipboard (+ notification)
```

In pseudocode, this is the flow the app implements:

```
API_Key      = <upload key from the GUI>
uploadURL    = https://api.e-z.host/files
selectedFile = <the screenshot just taken>

response = POST uploadURL
           headers: { key: API_Key }
           body:    selectedFile

imageURL = response["imageUrl"]
copy imageURL to clipboard
```

The app ignores the clipboard change caused by its own copy, so it never re-uploads a link.

---

## Installation

### Option 1: Build the EXE (recommended)

1. Install [Python 3.9+](https://www.python.org/downloads/) and make sure it's on your `PATH`.
2. Put `ezuploader.py` and `build.bat` in the same folder.
3. Double-click `build.bat`.
4. Your executable will be at `dist\EZUploader.exe`.

Or run the build manually:

```bat
python -m pip install requests pyperclip pystray pillow keyring pyinstaller
python -m PyInstaller --onefile --noconsole --name EZUploader --hidden-import keyring.backends.Windows ezuploader.py
```

### Option 2: Run from source

```bat
python -m pip install requests pyperclip pystray pillow keyring
python ezuploader.py
```

---

## Usage

1. Launch `EZUploader.exe`.
2. Paste your e-z.host **upload key** into the key field and click **Save**.
3. Take a screenshot with `Win+Shift+S` or `PrintScreen`.
4. Wait for the "link copied" notification, then paste the URL wherever you like.

### Options

| Option | What it does |
| --- | --- |
| Auto-upload screenshots | Turns clipboard watching on or off. Also available from the tray menu. |
| Start with Windows | Adds the app to your user startup entries. |
| Start minimized to tray | Launches hidden in the tray (only once a key is saved). |
| Closing the window keeps it running in the tray | The X button hides the window instead of quitting. |
| Show a notification when the link is copied | Toast notifications for success and failure. |
| Also open the link in my browser | Opens the uploaded image after each upload. |

### Tray menu

- **Open**: show the main window (also the double-click action)
- **Auto-upload screenshots**: pause or resume; the icon turns grey while paused
- **Upload clipboard now**: manually upload whatever image is on the clipboard
- **Quit**: fully exit the app

---

## Where things are stored

| Item | Location |
| --- | --- |
| Upload key | Windows Credential Manager (service `EZScreenshotUploader`) |
| Settings | `%APPDATA%\EZScreenshotUploader\config.json` |
| Startup entry | `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` |

Screenshots are uploaded from memory, and nothing is written to disk. To fully remove the app, turn off **Start with Windows**, delete the EXE, delete the settings folder, and remove the credential from Credential Manager if you want.

---

## Troubleshooting

**Nothing happens after I take a screenshot**
- Check that **Auto-upload screenshots** is enabled (tray icon not grey).
- Make sure a key is saved. The status line in the window shows what's going on.

**"Upload key was rejected"**
- Double-check the key, and make sure there are no extra spaces.

**"Couldn't reach e-z.host"**
- Check your internet connection or whether the service is up.

**"No image URL in response"**
- The API response didn't include `imageUrl`. If the API changed, update the request and response handling in `upload_image()` in `ezuploader.py`.

**Random images I copied get uploaded**
- The app can't tell a screenshot from any other image on the clipboard (for example, an image copied from a browser). Pause auto-upload from the tray when you don't want that.

**Antivirus flags the EXE**
- This is a known false positive with some PyInstaller one-file builds. You can build it yourself from the source to verify what it does.

---

## Tech stack

- [Python](https://www.python.org/) with `tkinter` for the GUI
- [Pillow](https://python-pillow.org/) for reading clipboard images
- [requests](https://requests.readthedocs.io/) for uploads
- [pystray](https://github.com/moses-palmer/pystray) for the tray icon
- [pyperclip](https://github.com/asweigart/pyperclip) for clipboard writes
- [keyring](https://github.com/jaraco/keyring) for secure key storage
- [PyInstaller](https://pyinstaller.org/) for packaging

---

## Contributing

Issues and pull requests are welcome. Ideas:

- Support for video/GIF uploads
- Custom filename options
- Configurable upload domain
- Deleting an upload from the history
- A hotkey to toggle auto-upload

---

## Contributors

- Claude (Anthropic): initial implementation and documentation

---

## License

Add your license of choice here (e.g. MIT).
