@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================================
REM  APEX v7.6 — Install apex_trader_v76.py as a Windows service (NSSM)
REM  --------------------------------------------------------------------------
REM  Deploy to:  C:\Apex\install_service.bat
REM  Run from an elevated (Administrator) Command Prompt.
REM
REM  Prerequisites:
REM    - Python 3 on PATH (python or pythonw)
REM    - APEX_MT5_PASSWORD set in System Environment Variables (recommended)
REM    - apex_trader_v76.py and dependencies in C:\Apex
REM ============================================================================

set "APEX_DIR=C:\Apex"
set "SCRIPT=%APEX_DIR%\apex_trader_v76.py"
set "SERVICE_NAME=APEX-LiveTrader"
set "NSSM_ROOT=C:\nssm"
set "NSSM_EXE=%NSSM_ROOT%\nssm-2.24\win64\nssm.exe"
set "NSSM_ZIP=%NSSM_ROOT%\nssm.zip"
set "NSSM_URL=https://nssm.cc/release/nssm-2.24.zip"

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

REM --- Download / extract NSSM if missing -------------------------------------
if not exist "!NSSM_EXE!" (
  echo NSSM not found at !NSSM_EXE!
  echo Downloading NSSM ...
  if not exist "%NSSM_ROOT%" mkdir "%NSSM_ROOT%"
  curl.exe -L "%NSSM_URL%" -o "%NSSM_ZIP%"
  if errorlevel 1 (
    echo ERROR: curl failed to download NSSM from %NSSM_URL%
    exit /b 1
  )
  powershell -NoProfile -Command "Expand-Archive -Path '%NSSM_ZIP%' -DestinationPath '%NSSM_ROOT%' -Force"
  if errorlevel 1 (
    echo ERROR: Failed to extract %NSSM_ZIP%
    exit /b 1
  )
)
if not exist "!NSSM_EXE!" (
  echo ERROR: nssm.exe still not found at !NSSM_EXE!
  exit /b 1
)
echo Using NSSM: !NSSM_EXE!

REM --- Resolve Python interpreter ---------------------------------------------
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

"!NSSM_EXE!" set "%SERVICE_NAME%" AppDirectory "%APEX_DIR%"

REM PYTHONPATH for local imports; APEX_MT5_PASSWORD should be in System env.
"!NSSM_EXE!" set "%SERVICE_NAME%" AppEnvironmentExtra "PYTHONPATH=%APEX_DIR%"

"!NSSM_EXE!" set "%SERVICE_NAME%" AppStdout "%APEX_DIR%\apex_v76_live.log"
"!NSSM_EXE!" set "%SERVICE_NAME%" AppStderr "%APEX_DIR%\apex_v76_errors.log"

"!NSSM_EXE!" set "%SERVICE_NAME%" AppExit Default Restart
"!NSSM_EXE!" set "%SERVICE_NAME%" AppThrottle 5000
"!NSSM_EXE!" set "%SERVICE_NAME%" Start SERVICE_AUTO_START

REM --- Start now --------------------------------------------------------------
echo Starting %SERVICE_NAME% ...
"!NSSM_EXE!" start "%SERVICE_NAME%"
if errorlevel 1 (
  echo WARNING: nssm start returned an error. Check:
  echo   sc query %SERVICE_NAME%
  echo   "!NSSM_EXE!" edit "%SERVICE_NAME%"
  exit /b 1
)

echo.
echo Done. Verify with:  sc query %SERVICE_NAME%
endlocal
exit /b 0
