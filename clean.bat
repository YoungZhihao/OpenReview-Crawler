@echo off
rem Delete crawl results and intermediate files (keeps the crawler itself).
setlocal
cd /d "%~dp0"

where pythonw.exe >nul 2>nul
if errorlevel 1 goto nopython

start "" pythonw.exe "%~dp0tools\cleanup_gui.py"
exit /b 0

:nopython
echo.
echo   Python 3 was not found on PATH.
echo.
pause
exit /b 1
