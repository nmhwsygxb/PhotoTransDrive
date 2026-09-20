# PhotoTransDrive · 公网中继 (relay.py)

让手机在**任何网络**（4G/5G/异地 Wi-Fi）都能连回家里的网盘电脑，**无需路由器端口映射**。

```
手机 ──► 公网服务器 relay.py ──隧道──► 家里电脑 relay_client.py ──► 本地 server.py:47810
```

- **relay.py** — 运行在**公网服务器**上（中转站）
- **relay_client.py** — 运行在**家里/公司网盘电脑**上（穿透 NAT，主动连出）
- **手机 App 零改动** — 中继对手机完全透明，手机连中继的数据端口 = 直连家里 server.py

---

## 一、准备一台公网服务器

任意有公网 IP 的 Linux/Windows 机器即可（轻量云服务器、VPS 均可），需开放 TCP 端口：

| 端口 | 用途 |
|---|---|
| 47820 | 控制端口（电脑注册隧道） |
| 47830–47849 | 数据端口（手机连接，20 个） |

> 端口可用 `--port` / `--data-start` / `--data-count` 修改，但需与手机端 App 填的中继端口一致。

## 二、服务器上运行 relay.py

```bash
# 生成一个强密钥（自己保存好）
python3 -c "import secrets; print(secrets.token_hex(16))"

# 启动中继（前台）
python3 relay.py --auth-key 你的密钥

# 或用 nohup 后台运行
nohup python3 relay.py --auth-key 你的密钥 > relay.out 2>&1 &

# 查看已注册的隧道
python3 relay.py --list --auth-key 你的密钥
```

`--auth-key` 也可通过环境变量 `PT_RELAY_AUTH` 提供。

## 三、家里电脑运行 relay_client.py

```bash
python3 relay_client.py \
  --relay 你的服务器IP或域名:47820 \
  --auth-key 你的密钥 \
  --local 127.0.0.1:47810 \
  --name my-home-pc
```

- `--local` 指向本机 server.py 地址（默认 `127.0.0.1:47810`，一般不用改）
- `--name` 隧道名（手机端可识别，默认 `my-pc`）
- `--retry` 断线重试间隔秒（默认 5）
- 常驻运行；断线/隧道关闭自动重连，Ctrl+C 退出

> **前提**：本机 server.py 已在运行（`python server.py`，监听 47810）。
> 建议 relay_client 与 server.py 一起设为开机自启（见 `deploy/` 下的 systemd 单元 / Windows 一键脚本）。

## 四、手机端连接

App 网盘页 → 模式切换选 **中继** → 填写：

- **中继地址**：`你的服务器IP或域名`
- **中继端口**：**数据端口 `47830`**（不是控制端口！见下方说明）
- **配对码**：与家里 server.py 的 `--pair-code` 一致（配对校验在电脑端完成，中继不参与）

> ⚠️ **重要**：手机必须连**数据端口**（默认 `47830`，即 `--data-start`），
> **不是控制端口 `47820`**。控制端口只处理电脑注册指令（`PT-RELAY-REG`），
> 手机连控制端口会被拒绝：`PT-RELAY-FAIL bad-request`。
>
> 单台电脑部署时，relay_client 分配到的数据端口固定为 `--data-start`（47830）；
> 多台电脑时各自分配不同端口，以服务器 `--list` 显示为准。

## 五、安全说明

- **认证**：`--auth-key` 只用于电脑注册隧道，防陌生人占用；连续失败 5 次锁 IP 5 分钟
- **业务安全**：配对码/文件内容由家里 server.py 校验，中继**不落地任何业务数据**（纯内存转发）
- **数据不落盘**：中继不写文件、不记录文件内容，只转发字节流
- **明文转发**：公网链路为明文 TCP（无 TLS）。敏感场景建议配合 VPN/SSH 隧道，
  或用 `--auth-key` 限制注册、保持配对码强随机

## 六、部署与运维

完整从零部署（VPS + systemd / Windows 一键脚本 + 防火墙 + 多电脑 + 故障排查）见 **`deploy/README_DEPLOY.md`**。

常用命令：

```bash
# 查看已注册隧道（含数据端口）——在服务器上或加 --list-host 指定服务器
python3 relay.py --list --auth-key 你的密钥

# 服务器后台运行 (Linux)
nohup python3 relay.py --auth-key 你的密钥 > relay.out 2>&1 &
```

## 七、已知限制

- **单隧道单会话**：一台电脑一条隧道，同一时刻一个手机在操作；多手机排队（中继自动等待 ≤3s）
- **手机必须连数据端口**（默认 47830），填错成控制端口 47820 会被拒绝
- **动态端口**：多台电脑时各自分配数据端口，以 `--list` 为准
- **公网实测**：本地已用回环链路验证（下载文件 sha256 与直连一致），真实公网部署后建议
  用手机 App 中继模式实测一次

## 八、故障排查

| 现象 | 排查 |
|---|---|
| 手机提示无法连接 | 服务器防火墙是否开放 47820/47830-47849；relay.py 是否在运行 |
| **手机连接被拒 bad-request** | **端口填错**——应填数据端口 47830，不是控制端口 47820 |
| relay_client 一直重试连接 | 检查 `--relay` 地址端口、`--auth-key` 是否与服务器一致 |
| relay_client 日志"本地连接失败" | 本机 server.py 是否在运行（47810） |
| 手机连接被拒绝 no-tunnel | 家里电脑 relay_client 是否已注册隧道（`--list` 查看） |
| 手机浏览时偶发失败 | 操作太快撞隧道重建窗口，中继自动排队等待（≤3s），重试即可 |
| 日志位置 | 服务器 `~/.phototransrelay/logs/relay.log`；电脑 `~/.phototransrelay/logs/relay_client.log` |

零第三方依赖，仅 Python 3.8+ 标准库。
