@echo off
rem SignalX scheduled tick wrapper: sets working dir, runs crawl+monitor, logs output.
rem Scheduled Task points here so relative paths (config\, data\) always resolve correctly.
cd /d F:\monitor\crawler_tool
echo ===== tick started %date% %time% ===== >> data\monitoring\tick_log.txt
F:\py311\python.exe -m crawler_tool.monitoring.scheduled_tick --config-dir config\monitoring --data-dir data\monitoring >> data\monitoring\tick_log.txt 2>&1
echo ===== tick finished, exit=%errorlevel% ===== >> data\monitoring\tick_log.txt
