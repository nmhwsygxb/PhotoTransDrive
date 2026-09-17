@echo off
rem PhotoTrans Drive Server launcher (auto-restart)
rem Usage: start-server.bat [--root D:/path] [--port 47810]
rem Feature: start server; auto-restart after crash (3s delay); logs to run-server.log

cd /d "%~dp0"

rem Switch console to UTF-8 so Chinese banner/logs display correctly
chcp 65001 >nul

set PYTHON=C:\Users\86176\AppData\Local\Programs\Python\Python313\python.exe
if not exist "%PYTHON%" set PYTHON=python

:loop
echo [%date% %time%] Starting PhotoTrans server... >> run-server.log
"%PYTHON%" server.py %*
echo [%date% %time%] Server exited (code %errorlevel%), restarting in 3s... >> run-server.log
timeout /t 3 /nobreak > nul
goto loop
