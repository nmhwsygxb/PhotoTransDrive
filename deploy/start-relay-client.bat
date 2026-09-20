@echo off
REM ============================================================
REM PhotoTrans 家里电脑中继客户端 - Windows 一键启动
REM 前置: 本机 server.py 已运行 (监听 47810)
REM 修改下面三行后保存, 双击运行即可
REM ============================================================
setlocal

REM 公网中继服务器地址+控制端口 (必改)
set RELAY_ADDR=你的服务器IP或域名:47820

REM 中继认证密钥, 与服务器 relay.py --auth-key 一致 (必改)
set AUTH_KEY=你的密钥

REM 隧道名 (手机端识别, 可改)
set NAME=my-home-pc

REM 本地网盘地址 (默认 127.0.0.1:47810, 一般不用改)
set LOCAL=127.0.0.1:47810

cd /d %~dp0..
echo 启动中继客户端: %RELAY_ADDR%  (隧道名 %NAME%)
echo 本机网盘: %LOCAL%
echo 日志: %USERPROFILE%\.phototransrelay\logs\relay_client.log
echo 按 Ctrl+C 退出
python relay_client.py --relay %RELAY_ADDR% --auth-key %AUTH_KEY% --local %LOCAL% --name %NAME% --retry 5
pause
