@echo off
REM ============================================================
REM PhotoTrans 公网中继服务器 - Windows 一键启动
REM (用于 Windows 公网服务器/内网穿透场景)
REM ============================================================
setlocal

REM 认证密钥 (必改: 生成一个强密钥, 如 python -c "import secrets;print(secrets.token_hex(16))")
set AUTH_KEY=你的密钥

REM 控制端口 (电脑注册隧道)
set CTRL_PORT=47820

REM 数据端口段 (手机连接)
set DATA_START=47830
set DATA_COUNT=20

cd /d %~dp0..
echo 启动中继服务器: 控制 %CTRL_PORT%, 数据 %DATA_START% 起共 %DATA_COUNT% 个
echo 注意: 防火墙需放行 %CTRL_PORT% 和 %DATA_START%-%DATA_START% 段端口
echo 按 Ctrl+C 退出
python relay.py --port %CTRL_PORT% --data-start %DATA_START% --data-count %DATA_COUNT% --auth-key %AUTH_KEY%
pause
