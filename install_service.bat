@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================================
REM  APEX — Install apex_trader.py as a Windows service (NSSM)
REM  --------------------------------------------------------------------------
REM  Deploy this file to:  C:\Apex\install_service.bat
REM  Run from an elevated (Administrator) Command Prompt.
REM
REM  Download NSSM (64-bit release zip) before first run, e.g.:
REM    curl.exe -L -o "%USERPROFILE%\Downloads\nssm-2.24.zip" https://nssm.cc/release/nssm-2.24.zip
REM  Then extract; you will need nssm.exe (win64) on PATH or set NSSM_EXE below.
REM ============================================================================

set "APEX_DIR=C:\Apex"
set "SCRIPT=%APEX_DIR%\apex_trader.py"
set "SERVICE_NAME=ApexTrader"
set "NSSM_EXE="

REM --- Optional: hard-code full path to nssm.exe if not on PATH --------------
REM set "NSSM_EXE=C:\Apex\tools\nssm-2.24\win64\nssm.exe"

REM --- Locate nssm.exe -------------------------------------------------------
if defined NSSM_EXE if exist "!NSSM_EXE!" goto have_nssm
for %%E in (nssm.exe) do (
  set "NSSM_EXE=%%~$PATH:E"
  if defined NSSM_EXE if exist "!NSSM_EXE!" goto have_nssm
)
if exist "%APEX_DIR%\tools\nssm-2.24\win64\nssm.exe" (
  set "NSSM_EXE=%APEX_DIR%\tools\nssm-2.24\win64\nssm.exe"
  goto have_nssm
)

echo ERROR: nssm.exe not found.
echo.
echo Download NSSM with curl, then extract win64\nssm.exe to e.g. %APEX_DIR%\tools\nssm-2.24\win64\
echo   curl.exe -L -o "%USERPROFILE%\Downloads\nssm-2.24.zip" https://nssm.cc/release/nssm-2.24.zip
echo.
exit /b 1

:have_nssm
echo Using NSSM: !NSSM_EXE!

REM --- Admin check ------------------------------------------------------------
net session >nul 2>&1
if errorlevel 1 (
  echo ERROR: Run this batch file as Administrator ^(right-click -^> Run as administrator^).
  exit /b 1
)

if not exist "%SCRIPT%" (
  echo ERROR: Script not found: %SCRIPT%
  exit /b 1
)

REM --- Resolve Python interpreter (prefer pythonw.exe for no console) -------
set "PYEXE="
for /f "delims=" %%P in ('where pythonw 2^>nul') do (
  set "PYEXE=%%P"
  goto gotpy
)
for /f "delims=" %%P in ('where python 2^>nul') do (
  set "PYEXE=%%P"
  goto gotpy
)
echo ERROR: Neither pythonw nor python found on PATH. Install Python 3 or add it to PATH.
exit /b 1

:gotpy
echo Using Python: !PYEXE!

REM --- Remove existing service if present -------------------------------------
sc query "%SERVICE_NAME%" >nul 2>&1
if not errorlevel 1 (
  echo Stopping and removing existing service %SERVICE_NAME% ...
  sc stop "%SERVICE_NAME%" >nul 2>&1
  ping -n 3 127.0.0.1 >nul
  "!NSSM_EXE!" remove "%SERVICE_NAME%" confirm
)

REM --- Install service --------------------------------------------------------
echo Installing service %SERVICE_NAME% ...
"!NSSM_EXE!" install "%SERVICE_NAME%" "!PYEXE!" "%SCRIPT%"

REM Working directory for relative paths / logs
"!NSSM_EXE!" set "%SERVICE_NAME%" AppDirectory "%APEX_DIR%"

REM Environment (NSSM AppEnvironmentExtra: space-separated KEY=value pairs)
REM Password contains * — keep entire value quoted for NSSM.
"!NSSM_EXE!" set "%SERVICE_NAME%" AppEnvironmentExtra "APEX_MT5_PASSWORD=PjSe*i1i PYTHONPATH=C:\Apex"

REM Restart on any non-zero exit / crash; small delay before respawn
"!NSSM_EXE!" set "%SERVICE_NAME%" AppExit Default Restart
"!NSSM_EXE!" set "%SERVICE_NAME%" AppThrottle 5000

REM Start automatically when Windows boots
"!NSSM_EXE!" set "%SERVICE_NAME%" Start SERVICE_AUTO_START

REM Optional: rotate logs under C:\Apex
if not exist "%APEX_DIR%\logs" mkdir "%APEX_DIR%\logs" 2>nul
"!NSSM_EXE!" set "%SERVICE_NAME%" AppStdout "%APEX_DIR%\logs\apex_service_stdout.log"
"!NSSM_EXE!" set "%SERVICE_NAME%" AppStderr "%APEX_DIR%\logs\apex_service_stderr.log"
"!NSSM_EXE!" set "%SERVICE_NAME%" AppRotateFiles 1
"!NSSM_EXE!" set "%SERVICE_NAME%" AppRotateOnline 1
"!NSSM_EXE!" set "%SERVICE_NAME%" AppRotateBytes 1048576

REM --- Start now --------------------------------------------------------------
echo Starting %SERVICE_NAME% ...
net start "%SERVICE_NAME%"
if errorlevel 1 (
  echo WARNING: net start returned an error. Check configuration with:
  echo   "!NSSM_EXE!" edit "%SERVICE_NAME%"
  exit /b 1
)

echo.
echo Done. Service %SERVICE_NAME% is installed and set to start automatically.
echo Edit service: "!NSSM_EXE!" edit "%SERVICE_NAME%"
endlocal
exit /b 0
