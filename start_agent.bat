@echo off
setlocal

set "PROJECT_ROOT=%~dp0"
if "%PROJECT_ROOT:~-1%"=="\" set "PROJECT_ROOT=%PROJECT_ROOT:~0,-1%"

powershell -ExecutionPolicy Bypass -File "%PROJECT_ROOT%\scripts\start_all.ps1" -ProjectRoot "%PROJECT_ROOT%" -TunnelName "weixin-agent" -PublicHost "wxbot.haoyusun.me"

echo.
echo Done. Press any key to close...
pause >nul
