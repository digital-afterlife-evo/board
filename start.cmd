@echo off
setlocal
title Typewriter Host - COM4
pushd "%~dp0" || exit /b 1
echo Starting typewriter host: COM4, HTTP 8765, WebSocket 8766
echo Keep this window open. Extra UART logs: start.cmd --debug
".venv\Scripts\python.exe" -u -m host.app --port COM4 --headless --char-interval-ms 80 --return-delay-ms 500 %*
set "START_EXIT=%ERRORLEVEL%"
echo.
echo Typewriter host stopped. Exit code: %START_EXIT%
pause
popd
exit /b %START_EXIT%
