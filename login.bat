@echo off
rem Open the login window (asks for the password only when the token has run out).
setlocal
cd /d "%~dp0"

where pythonw.exe >nul 2>nul
if errorlevel 1 goto nopython

start "" pythonw.exe "%~dp0tools\app_gui.py" --login-only
exit /b 0

:nopython
echo.
echo   Python 3 was not found on PATH.
echo.
echo   Install Python 3 and tick "Add python.exe to PATH" during setup,
echo   or create python-path.txt in this folder containing the full path
echo   to pythonw.exe.
echo.
pause
exit /b 1
