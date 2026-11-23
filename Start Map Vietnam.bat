@echo off
rem Double-click this file in Windows to start Map Vietnam and open it in its own window.
rem It finds a Python 3.12 or newer and runs start.py from this folder. The window that opens
rem must stay open while the map is in use; "Quit Map Vietnam" in the map (or closing the
rem window, or Control-C) stops it.
rem Keep this file's line endings as CRLF (.gitattributes does): labels and goto can misbehave with LF.

cd /d "%~dp0"
title Map Vietnam

rem Prefer the "py" launcher that the python.org installer adds; "python" may be the Microsoft Store stub.
set "PYCMD="
py -3 -c "import sys; sys.exit(sys.version_info < (3, 12))" >nul 2>nul
if not errorlevel 1 set "PYCMD=py -3"
if defined PYCMD goto run
python -c "import sys; sys.exit(sys.version_info < (3, 12))" >nul 2>nul
if not errorlevel 1 set "PYCMD=python"
if defined PYCMD goto run

echo Map Vietnam needs Python 3.12 or newer, and none was found on this computer.
echo Download Python from https://www.python.org/downloads/ , install it
echo (tick "Add python.exe to PATH" on the first screen),
echo then double-click "Start Map Vietnam.bat" again.
echo.
pause
exit /b 1

:run
%PYCMD% start.py %*
set "CODE=%errorlevel%"
rem Status 0 is a normal quit from the map; 130 is Control-C. Anything else: keep the window so the message can be read.
if "%CODE%"=="0" exit /b 0
if "%CODE%"=="130" exit /b 0
echo.
echo Map Vietnam stopped with a problem (see the messages above).
pause
exit /b %CODE%
