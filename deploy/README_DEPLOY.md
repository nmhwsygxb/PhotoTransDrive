# PhotoTransDrive · 中继部署手册

从零部署完整中继链路（手机在外网访问家里网盘）的逐步指南。
配合 `README_RELAY.md`（协议与原理）阅读。

```
手机 ──► 公网服务器 relay.py ──隧道──► 家里电脑 relay_client.py ──► 本地 server.py:47810
```

---

## 第一步：准备公网服务器（VPS / 轻量云）

要求：有公网 IP，Linux 或 Windows，Python 3.8+。

**Linux (推荐)**：上传 relay.py 到服务器（如 `/opt/phototransdrive/relay.py`），用本目录
`relay-server.service` 配置 systemd 开机自启：

```bash
# 生成强密钥（保存好）
python3 -c "import secrets; print(secrets.token_hex(16))"

# 编辑 service 文件里的 --auth-key，然后：
sudo cp deploy/relay-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now relay-server
sudo systemctl status relay-server          # 应显示 active (running)
journalctl -u relay-server -f               # 实时日志
```

**Windows**：双击 `deploy/start-relay-server.bat`（改好 `AUTH_KEY`）。

### 防火墙必须放行
| 端口 | 用途 |
|---|---|
| 47820/TCP | 控制端口（电脑注册隧道） |
| 47830–47849/TCP | 数据端口（手机连接，20 个） |

```bash
# Ubuntu ufw 示例
sudo ufw allow 47820/tcp
sudo ufw allow 47830:47849/tcp
```

> 端口可在 `relay.py --port/--data-start/--data-count` 修改，**手机端填的端口会随之变化**。

## 第二步：家里电脑启动网盘 server.py

确保本机 server.py 已在运行（监听 47810）：

```bash
python server.py --pair-code 你的配对码
# Windows 也可用根目录 start-server.bat
```

## 第三步：家里电脑启动 relay_client（穿透 NAT）

**Windows**：双击 `deploy/start-relay-client.bat`（改好 `RELAY_ADDR` 和 `AUTH_KEY`）。

**Linux**：用 `deploy/relay-client.service` 配置 systemd。

验证隧道已注册：

```bash
# 在服务器上查（或本机执行 + --list-host 指向服务器）
python relay.py --list --auth-key 你的密钥 --list-host 你的服务器IP
# 输出应包含: 隧道名 电脑地址 数据端口 隧道就绪 是
```

## 第四步：手机 App 连接

App 网盘页 → 模式切换选 **中继** → 填写：

| 字段 | 值 |
|---|---|
| 中继地址 | 你的服务器 IP 或域名 |
| **中继端口** | **数据端口（默认 `47830`）** ← 注意 |
| 配对码 | 与家里 server.py `--pair-code` 一致 |

> ⚠️ **重要**：手机填的是**数据端口**（默认 47830），**不是控制端口 47820**！
> 控制端口只处理电脑注册指令，手机连控制端口会被拒绝（`PT-RELAY-FAIL bad-request`）。
>
> 单台电脑部署时，relay_client 拿到的数据端口固定为 `--data-start`（47830）。
> 若 `--list` 显示其它数据端口，以实际分配为准。

## 常用运维

```bash
# 服务器上查看实时连接
journalctl -u relay-server -f

# 查看已注册隧道（含数据端口、电脑地址）
python relay.py --list --auth-key 你的密钥 --list-host 你的服务器IP

# 重启中继
sudo systemctl restart relay-server

# 家里电脑查看 relay_client 日志
# Windows: %USERPROFILE%\.phototransrelay\logs\relay_client.log
# Linux:   journalctl -u relay-client -f
```

## 多电脑 / 多手机

- **一台电脑**：一个 relay_client 一条隧道，手机轮流使用；同一时刻一个手机一个操作
- **多台电脑**：每台电脑跑一个 relay_client（不同 `--name`），会分配到不同数据端口。
  每台对应一个数据端口，手机连哪台就填哪个端口。
- **端口分配**：按注册顺序取第一个空闲端口。重启电脑后重新分配，一般仍是 47830 起。

## 安全提示

- 中继**明文转发**（无 TLS）：公网链路上配对码/文件内容可被截获。家庭场景可用
  `--auth-key` 防止陌生人占用隧道；敏感数据建议配合 VPN/SSH 隧道使用。
- `--auth-key` 泄露 = 陌生人可注册隧道（数据端口仍需要手机正确配对码才能访问网盘）。
- 中继不落盘任何业务数据（纯内存字节转发）。
- **systemd 密钥权限**：`relay-server.service` 里的 `--auth-key` 是明文，默认权限 644 其他用户可读。
  建议：`sudo chmod 600 /etc/systemd/system/relay-server.service`，
  或改用 `EnvironmentFile=/etc/phototrans-relay.env`（chmod 600）+ `Environment=PT_RELAY_AUTH=...`
  （去掉 ExecStart 里的 `--auth-key`）。
- **DoS 面**：中继数据端口允许最多 256 个并发连接；公网暴露下攻击者用大量慢连接可占满名额
  导致正常手机暂不可用。家庭/小规模使用风险低，生产级建议前置防火墙/仅放行可信 IP。

## 故障排查

| 现象 | 检查 |
|---|---|
| 手机提示连接失败 | 服务器防火墙是否放行 47820 + 47830-47849；relay 是否运行 |
| 手机连接被拒 bad-request | **填错端口了**——应填数据端口 47830，不是控制端口 47820 |
| relay_client 一直重试 | `--relay` 地址/端口、`--auth-key` 是否与服务器一致 |
| relay_client "本地连接失败" | 本机 server.py 是否在 47810 运行 |
| 手机连接 no-tunnel | 隧道未就绪：服务器 `--list` 看 relay_client 是否注册成功 |
| 手机浏览时偶发失败 | 操作太快撞隧道重建窗口，中继已自动排队等待（≤3s），重试即可 |
| 中继日志 | 服务器 `journalctl -u relay-server`（Linux）或启动终端（Windows） |
| relay_client 日志 | `%USERPROFILE%\.phototransrelay\logs\relay_client.log` |

## 文件清单

```
relay.py                      公网中继服务器 (运行在 VPS)
relay_client.py               家里电脑中继客户端 (穿透 NAT)
deploy/relay-server.service   Linux 服务器 systemd 单元
deploy/relay-client.service   Linux 家里电脑 systemd 单元 (可选)
deploy/start-relay-server.bat Windows 服务器一键启动
deploy/start-relay-client.bat Windows 家里电脑一键启动
README_RELAY.md               原理与协议说明
```
