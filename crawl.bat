@echo off
rem Prompt for a paper, crawl it, then open the output folder.
setlocal
cd /d "%~dp0"

where python.exe >nul 2>nul
if errorlevel 1 goto nopython

python.exe "%~dp0tools\quick_crawl.py"
set CODE=%ERRORLEVEL%

echo.
echo   Output folder: %~dp0output
echo.
if exist "%~dp0output" start "" explorer.exe "%~dp0output"
pause
exit /b %CODE%

:nopython
echo.
echo   Python 3 was not found on PATH.
echo.
echo   Install Python 3 and tick "Add python.exe to PATH" during setup,
echo   or create python-path.txt in this folder containing the full path
echo   to python.exe.
echo.
pause
exit /b 1
