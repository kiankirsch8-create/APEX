@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================================
REM  APEX v7.6 PRIVATE — Install apex_trader_v76_private.py as NSSM service
REM  --------------------------------------------------------------------------
REM  Deploy to:  C:\Apex\install_service_private.bat
REM  Run from an elevated (Administrator) Command Prompt.
REM
REM  Crash survival (acceptance):
REM    AppExit Default Restart  — auto-restart on any exit / kill
REM    AppThrottle 5000         — restart within ~5s (SLA: <60s)
REM  On restart: reloads circuit_halt_until + _adopt_open_positions.
REM ============================================================================

set "APEX_DIR=C:\Apex"
set "SCRIPT=%APEX_DIR%\apex_trader_v76_private.py"
set "SERVICE_NAME=APEX-LiveTraderPrivate"
set "NSSM_ROOT=C:\nssm"
set "NSSM_EXE=%NSSM_ROOT%\nssm-2.24\win64\nssm.exe"
set "NSSM_ZIP=%NSSM_ROOT%\nssm.zip"
set "NSSM_URL=https://nssm.cc/release/nssm-2.24.zip"
set "RESTART_THROTTLE_MS=5000"

net session >nul 2>&1
if errorlevel 1 (
  echo ERROR: Run this batch file as Administrator.
  exit /b 1
)

if not exist "%SCRIPT%" (
  echo ERROR: Script not found: %SCRIPT%
  exit /b 1
)

if not exist "!NSSM_EXE!" (
  echo NSSM not found at !NSSM_EXE!
  echo Downloading NSSM ...
  if not exist "%NSSM_ROOT%" mkdir "%NSSM_ROOT%"
  curl.exe -L "%NSSM_URL%" -o "%NSSM_ZIP%"
  if errorlevel 1 (
    echo ERROR: curl failed to download NSSM
    exit /b 1
  )
  powershell -NoProfile -Command "Expand-Archive -Path '%NSSM_ZIP%' -DestinationPath '%NSSM_ROOT%' -Force"
)
if not exist "!NSSM_EXE!" (
  echo ERROR: nssm.exe still not found at !NSSM_EXE!
  exit /b 1
)

set "PYEXE="
for /f "delims=" %%P in ('where python 2^>nul') do (
  set "PYEXE=%%P"
  goto gotpy
)
for /f "delims=" %%P in ('where pythonw 2^>nul') do (
  set "PYEXE=%%P"
  goto gotpy
)
echo ERROR: Neither python nor pythonw found on PATH.
exit /b 1

:gotpy
echo Using Python: !PYEXE!

sc query "%SERVICE_NAME%" >nul 2>&1
if not errorlevel 1 (
  echo Stopping and removing existing service %SERVICE_NAME% ...
  sc stop "%SERVICE_NAME%" >nul 2>&1
  ping -n 3 127.0.0.1 >nul
  "!NSSM_EXE!" remove "%SERVICE_NAME%" confirm
)

echo Installing service %SERVICE_NAME% ...
"!NSSM_EXE!" install "%SERVICE_NAME%" "!PYEXE!" "%SCRIPT%"
"!NSSM_EXE!" set "%SERVICE_NAME%" AppDirectory "%APEX_DIR%"
"!NSSM_EXE!" set "%SERVICE_NAME%" AppEnvironmentExtra "PYTHONPATH=%APEX_DIR%"
"!NSSM_EXE!" set "%SERVICE_NAME%" AppStdout "%APEX_DIR%\apex_v76_private.log"
"!NSSM_EXE!" set "%SERVICE_NAME%" AppStderr "%APEX_DIR%\apex_v76_private_errors.log"
"!NSSM_EXE!" set "%SERVICE_NAME%" AppExit Default Restart
"!NSSM_EXE!" set "%SERVICE_NAME%" AppThrottle %RESTART_THROTTLE_MS%
"!NSSM_EXE!" set "%SERVICE_NAME%" Start SERVICE_AUTO_START

echo Starting %SERVICE_NAME% ...
"!NSSM_EXE!" start "%SERVICE_NAME%"
if errorlevel 1 (
  echo WARNING: nssm start returned an error. Check sc query %SERVICE_NAME%
  exit /b 1
)

echo.
echo Done. Crash-restart is ON ^(AppExit Default Restart, throttle %RESTART_THROTTLE_MS%ms^).
echo Verify with:  sc query %SERVICE_NAME%
endlocal
exit /b 0
