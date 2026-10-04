@echo off
REM Builds EZUploader.exe (needs Python 3.9+ installed and on PATH)
python -m pip install --upgrade requests pyperclip pystray pillow keyring pyinstaller
python -m PyInstaller --onefile --noconsole --name EZUploader ^
  --hidden-import keyring.backends.Windows ^
  ezuploader.py
echo.
echo Done! Your EXE is at dist\EZUploader.exe
pause
