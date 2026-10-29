@echo off
cd /d "%~dp0"
echo [1/3] decrypt images...
".venv\Scripts\python.exe" decrypt_media.py
echo [2/3] make 700px previews...
".venv\Scripts\python.exe" make_previews.py
echo [3/3] export html...
".venv\Scripts\python.exe" export_chats.py
echo.
echo Done. Open export\index.html
pause
