@echo off
setlocal EnableExtensions

rem ===========================================================================
rem  Launcher for the iCloud Browser Console (Windows).
rem
rem    run.bat            start the console server on :8000
rem    run.bat client     start the single-page client on :9002
rem    run.bat test       run the test suite
rem    run.bat setup      create venv + install dependencies
rem
rem  Creates .venv automatically on first run.
rem ===========================================================================

cd /d "%~dp0"

set "VENV=.venv"
set "VPY=.venv\Scripts\python.exe"

rem --- configuration (override by setting these before calling run.bat) -----
set "HOST=%ICLOUD_HOST%"
if not defined HOST set "HOST=0.0.0.0"
set "PORT=%ICLOUD_PORT%"
if not defined PORT set "PORT=8000"
set "CPORT=%ICLOUD_CLIENT_PORT%"
if not defined CPORT set "CPORT=9002"

set "CMD=%~1"
if not defined CMD set "CMD=serve"

rem --- first run: no virtualenv yet ----------------------------------------
if not exist "%VPY%" (
    echo No virtualenv found - running setup first.
    echo.
    call :do_setup
    if errorlevel 1 goto :fail
    echo Setup complete. Run "run.bat" again to start the server.
    goto :eof
)

if /i "%CMD%"=="setup" (
    call :do_setup
    goto :eof
)
if /i "%CMD%"=="test"   goto :test
if /i "%CMD%"=="tests"  goto :test
if /i "%CMD%"=="serve"  goto :serve
if /i "%CMD%"=="client" goto :client

echo usage: run.bat [serve ^| client ^| test ^| setup] 1>&2
exit /b 2

rem ===========================================================================
:serve
echo ==^> iCloud Browser Console on http://localhost:%PORT%
echo     Ctrl+C to stop.
echo.
"%VPY%" -m uvicorn app.main:app --host %HOST% --port %PORT%
goto :eof

:client
echo ==^> iCloud Client on http://localhost:%CPORT%
echo     One page, an iframe and a WebSocket. Visiting it starts the flow.
echo     Ctrl+C to stop.
echo.
"%VPY%" -m uvicorn client.server:app --host %HOST% --port %CPORT%
goto :eof

:test
rem Pass any extra arguments through to pytest: run.bat test -k targets
set "PYTEST_ARGS="
shift
:collect_args
if "%~1"=="" goto :run_tests
set "PYTEST_ARGS=%PYTEST_ARGS% %1"
shift
goto :collect_args
:run_tests
"%VPY%" -m pytest%PYTEST_ARGS%
goto :eof

rem ===========================================================================
:do_setup
call :find_python
if not defined BASEPY (
    echo ERROR: Python 3 was not found on PATH.
    echo.
    echo   Install it from https://www.python.org/downloads/ and tick
    echo   "Add python.exe to PATH" during setup, then run this file again.
    exit /b 1
)

echo ==^> Creating virtualenv in %VENV%
%BASEPY% -m venv "%VENV%"
if errorlevel 1 (
    echo ERROR: could not create the virtualenv.
    exit /b 1
)

echo ==^> Upgrading pip
"%VPY%" -m pip install --upgrade pip >nul 2>&1

echo ==^> Installing Python dependencies
"%VPY%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: dependency install failed.
    exit /b 1
)

echo.
echo ==^> Next: download a browser binary for Playwright
echo.
echo     "%VPY%" -m playwright install chromium
echo.
echo     Note: on Windows use the command above as-is. The --with-deps
echo     flag installs Linux system libraries and is not needed here.
echo.
echo ==^> Then start it:
echo       run.bat            console on :%PORT%
echo       run.bat client     client  on :%CPORT%
echo.
exit /b 0

rem ===========================================================================
:find_python
rem Prefer the Windows launcher ("py -3"), fall back to a bare "python".
set "BASEPY="
where py >nul 2>&1
if not errorlevel 1 (
    set "BASEPY=py -3"
    goto :eof
)
where python >nul 2>&1
if not errorlevel 1 (
    set "BASEPY=python"
    goto :eof
)
goto :eof

:fail
exit /b 1
