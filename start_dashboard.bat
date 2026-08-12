@echo off
chcp 65001 >nul
setlocal
cd /d D:\F_Data_Sys

rem ============================================================
rem  看板启动器: 自动探测 8765 端口
rem    - 桌面端/服务已在跑 -> 直接打开浏览器
rem    - 未在跑            -> 后台拉起无GUI只读服务, 再打开浏览器
rem ============================================================

set "URL=http://127.0.0.1:8765/dashboard"

rem 1) 探测服务是否已活(桌面端在跑也算活)
curl -s -m 2 -o nul http://127.0.0.1:8765/api/v1/health
if %errorlevel%==0 (
    echo [看板] 服务已在运行, 直接打开浏览器...
    start "" "%URL%"
    exit /b 0
)

rem 2) 未运行 -> 后台拉起独立只读服务(最小化窗口, 保持运行)
echo [看板] 服务未运行, 正在启动独立只读服务(窗口可最小化, 关闭即停止服务)...
start "Bern 看板服务" /min python scripts_gen\serve_api.py --port 8765

rem 3) 等待服务就绪(最多 ~15 秒)
set n=0
:waitloop
curl -s -m 1 -o nul http://127.0.0.1:8765/api/v1/health
if %errorlevel%==0 goto up
set /a n+=1
if %n% geq 15 goto timeout
ping -n 2 127.0.0.1 >nul
goto waitloop

:up
echo [看板] 服务就绪, 打开浏览器...
start "" "%URL%"
exit /b 0

:timeout
echo [看板] 服务启动超时, 请确认 Python 环境正常后重试。
pause
exit /b 1
