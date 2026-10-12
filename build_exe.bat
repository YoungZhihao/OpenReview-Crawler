@echo off
rem Rebuild the two .exe launchers using the C# compiler built into Windows.
setlocal
cd /d "%~dp0"

where python.exe >nul 2>nul
if errorlevel 1 goto nopython

python.exe "%~dp0tools\build_exe.py" %*
pause
exit /b %ERRORLEVEL%

:nopython
echo.
echo   Python 3 was not found on PATH.
echo.
pause
exit /b 1
