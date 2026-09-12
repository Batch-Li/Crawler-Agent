@echo off
setlocal
rem One crawl+monitor tick only; no Agent runner or scheduler registration.
pushd "%~dp0.." || exit /b 1
if not exist "data\monitoring" mkdir "data\monitoring"
if not exist "data\monitoring" (
    popd
    exit /b 1
)
set "SIGNALX_PYTHON=python"
if exist "..\signalx-agent\.venv\Scripts\python.exe" set "SIGNALX_PYTHON=..\signalx-agent\.venv\Scripts\python.exe"
echo ===== tick started %date% %time% ===== >> data\monitoring\tick_log.txt
"%SIGNALX_PYTHON%" -m crawler_tool.monitoring.scheduled_tick --config-dir config\monitoring --data-dir data\monitoring >> data\monitoring\tick_log.txt 2>&1
set "SIGNALX_EXIT_CODE=%ERRORLEVEL%"
echo ===== tick finished, exit=%SIGNALX_EXIT_CODE% ===== >> data\monitoring\tick_log.txt
popd
endlocal & exit /b %SIGNALX_EXIT_CODE%
