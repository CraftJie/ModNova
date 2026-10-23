@echo off
rem Launch the classic Tkinter UI. Requires a Python build that includes tcl/tk.
rem If auto-detection picks the wrong one, set MODBUS_PYTHON to a suitable python.exe.

setlocal
cd /d "%~dp0"

set "PYEXE="
set "PYARGS="
if defined MODBUS_PYTHON if exist "%MODBUS_PYTHON%" set "PYEXE=%MODBUS_PYTHON%"
if defined PYEXE goto run

rem uv-managed interpreters usually ship with tcl/tk
if exist "%APPDATA%\uv\python" (
    for /d %%D in ("%APPDATA%\uv\python\cpython-*") do (
        if exist "%%D\python.exe" (
            "%%D\python.exe" -c "import tkinter" >nul 2>&1
            if not errorlevel 1 (
                set "PYEXE=%%D\python.exe"
                goto run
            )
        )
    )
)

rem PATH fallback, but skip the Microsoft Store stub (it just opens the Store)
for /f "delims=" %%P in ('where python 2^>nul') do (
    echo %%P| findstr /i /c:"WindowsApps" >nul
    if errorlevel 1 (
        "%%P" -c "import tkinter" >nul 2>&1
        if not errorlevel 1 (
            set "PYEXE=%%P"
            goto run
        )
    )
)

py -3 -c "import tkinter" >nul 2>&1
if not errorlevel 1 (
    set "PYEXE=py"
    set "PYARGS=-3"
    goto run
)

echo.
echo   [ERROR] No Python with tkinter was found.
echo.
echo   Install Python 3.9+ from https://www.python.org/downloads/
echo   (the official installer bundles tcl/tk), or set MODBUS_PYTHON
echo   to a python.exe that can run: import tkinter
echo.
pause
exit /b 1

:run
echo Using interpreter: %PYEXE% %PYARGS%
"%PYEXE%" %PYARGS% modbus.py --tk
if errorlevel 1 pause
exit /b 0
