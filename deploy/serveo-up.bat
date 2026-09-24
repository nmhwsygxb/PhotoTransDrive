@echo off
REM ============================================================
REM PhotoTransDrive · serveo 公网转发一键启动 (Windows)
REM 手机 App 直连模式: 地址 serveo.net  端口看窗口输出  配对码默认 131420
REM 修改配对码: serveo-up.bat 123456  (或改下面 set PAIR_CODE=)
REM ============================================================
setlocal
if "%~1"=="" (set PAIR_CODE=131420) else (set PAIR_CODE=%~1)
set PY=python
where py >nul 2>nul && set PY=py -3
cd /d %~dp0
echo 启动中: 本地 server.py + serveo 公网隧道 (配对码 %PAIR_CODE%)
echo 看到 "公网地址就绪: serveo.net:XXXXX" 即可在手机直连模式使用
echo 按 Ctrl+C 退出
%PY% serveo-up.py --pair-code %PAIR_CODE%
pause
