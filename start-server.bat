@echo off
rem PhotoTrans 网盘服务端 常驻启动脚本
rem 用法: start-server.bat [--root D:/path] [--port 47810]
rem 功能: 启动服务; 崩溃/被杀后 3 秒自动重启; 日志写 run-server.log

cd /d "%~dp0"

set PYTHON=C:\Users\86176\AppData\Local\Programs\Python\Python313\python.exe
if not exist "%PYTHON%" set PYTHON=python

:loop
echo [%date% %time%] 启动 PhotoTrans 服务端... >> run-server.log
"%PYTHON%" server.py %*
echo [%date% %time%] 服务端退出 (代码 %errorlevel%), 3 秒后重启... >> run-server.log
timeout /t 3 /nobreak > nul
goto loop
