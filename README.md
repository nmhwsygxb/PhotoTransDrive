<div align="center">

# 💻 PhotoTransDrive

### 家庭网盘 · 电脑端（Python）

人在外面，手机也能打开家里电脑的网盘 —— 直连 / 邮件寻址 / 自建中继 / 免注册转发，任选一种

[![License](https://img.shields.io/github/license/nmhwsygxb/PhotoTransDrive)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-2f81f7)](#)
[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)](#)

**同项目的另外三端：** [Android](https://github.com/nmhwsygxb/PhotoTransApp) · [iOS](https://github.com/nmhwsygxb/PhotoTrans-iOS) · [HarmonyOS](https://github.com/nmhwsygxb/PhotoTrans-HarmonyOS)

</div>

---

## 为什么做这个

家里一台电脑常年开着，装着照片、文档和整季的剧集。局域网内手机连过去传文件很顺手——但人一出门，这台电脑就锁死在 NAT 后面了。更麻烦的是运营商的 IPv6 前缀说变就变，昨天抄下来的地址今天就是死路。

PhotoTransDrive 把"外面怎么连进来"做成了**可选的路径**：地址会变，就让电脑发邮件告诉你；NAT 太深，就自己搭一座透明的桥；连服务器都不想要，就借一把别人现成的梯子。协议层完全一致，手机 App 侧零改动。

## 功能总览

| 能力 | 说明 |
|---|---|
| 🔐 权限分级 | 普通配对码只读（浏览/下载），管理员配对码完整权限（上传/删除），配对即确定 |
| 🛡 安全加固 | 路径防穿越、符号链接防护、256-bit 随机令牌、常量时间比较、认证锁定、并发限制 |
| 🚀 零配置起步 | 首次启动自动生成并持久化配对码，重启不变，手机配对一次永久有效 |
| 📡 自动发现 | UDP 广播 `PT-DISCOVER`，App 在同一网络自动找到电脑（端口 47811） |
| ✉️ 邮件寻址 | 地址变化时自动把当前地址/配对码发到你的邮箱，App 自动解析配对 |
| 🌉 异地访问 | 自建中继隧道（NAT 穿透）或免注册第三方转发，人在外面也能连 |

## 快速上手

```bash
# 最简启动：默认端口 47810，配对码自动生成并持久化
python server.py

# 指定网盘目录与配对码
python server.py --root D:\MyDrive --pair-code 123456 --admin-code 888888

# 想在外面连、又不想租服务器：一键打通公网（自动打印地址）
python bore-up.py --pair-code 123456
```

启动后会看到一张启动面板：**局域网地址、IPv6 地址、两个配对码**都在上面。手机打开 **PhotoTrans → 网盘**，输入地址和配对码即可绑定。

## 人在外面，怎么连进来

**① 邮件寻址 —— 地址会变，就让电脑自己汇报。** server.py 启动时自动把当前地址、端口和配对码发到你的邮箱；手机开邮件模式，App 自己收信、解析、配对，你甚至不用看那封邮件。`--email-to <邮箱> --email-auth <授权码>` 即可启用。

**② 自建中继 —— NAT 深处的桥。** 在公网放一个 `relay.py`，家里电脑用 `relay_client.py` **主动连出**建反向隧道，手机从桥的另一端进来。桥只转发字节、不落盘文件，带认证、心跳保活、断线自动重建。适合长期稳定、多设备访问——部署步骤见 [deploy/README_DEPLOY.md](deploy/README_DEPLOY.md)。

**③ 免注册转发 —— 连服务器都不想租？** `bore-up.py` 借助公开的免注册 TCP 转发服务，一条命令打通公网，端口秒开。实测稳定可用，代价是端口随启动变化、免费额度有限——适合个人轻量使用。

## 刚才说的，是真的

- **下载完整性**：经公网中继拿到的文件，sha256 与局域网直连**逐字节一致**；
- **断线自愈**：中继进程被杀后，客户端主动断开隧道并自动重建，全程日志可查；
- **踩过的坑**：serveo.net 免费隧道每约 4.6 分钟被服务端强制断开、端口漂移（记录在案），因此默认推荐实测稳定、免注册的 bore.pub——“免费”不等于“能用”，测过才算数。

## 目录速览

```
server.py               电脑端网盘服务（协议 / 鉴权 / 文件管理 / 邮件通知）
relay.py                公网中继服务器（反向隧道，NAT 穿透）
relay_client.py         家里电脑的中继客户端（主动连出 + 心跳 + 自动重建）
bore-up.py              免注册转发一键脚本（自动下载客户端 + 打印端口 + 断线重连）
deploy/                 systemd 单元 / Windows 一键脚本 / 部署手册
docs/SERVER_MANUAL.md   服务端完整手册（启动参数 / 配置 / 协议 / 故障排除）
README_RELAY.md         中继协议与部署细节
```

## 安全

出门在外访问家里的电脑，**配对码就是门槛**：日常用普通配对码（只读），需要改动文件时才输入管理员配对码。文件内容只在你的设备与 server 之间流动——中继与第三方桥都不落盘、不校验密码、不接触业务数据。

## 开源协议

[MIT License](LICENSE)