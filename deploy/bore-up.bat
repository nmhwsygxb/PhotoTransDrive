@echo off
REM ============================================================
REM PhotoTransDrive · bore.pub 公网转发一键启动 (Windows)
REM 手机 App 直连模式: 地址 bore.pub  端口看窗口输出  配对码默认 131420
REM 首次运行自动下载 bore 客户端
REM ============================================================
setlocal
if "%~1"=="" (set PAIR_CODE=131420) else (set PAIR_CODE=%~1)
set PY=python
where py >nul 2>nul && set PY=py -3
cd /d %~dp0
echo 启动中: 本地 server.py + bore.pub 公网隧道 (配对码 %PAIR_CODE%)
echo 看到 "公网地址就绪: bore.pub:XXXXX" 即可在手机直连模式使用
echo 按 Ctrl+C 退出
%PY% bore-up.py --pair-code %PAIR_CODE%
pause