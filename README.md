# PhotoTrans 网盘服务端 使用手册

电脑端 Python 网盘服务，配合 PhotoTrans App 实现局域网 / 异地文件传输。

---

## 目录

1. [快速开始](#快速开始)
2. [启动参数](#启动参数)
3. [配置说明](#配置说明)
4. [权限分级](#权限分级)
5. [配对设备](#配对设备)
6. [协议参考](#协议参考)
7. [故障排除](#故障排除)
8. [安全特性](#安全特性)
9. [已知限制](#已知限制)
10. [版本历史](#版本历史)

---

## 快速开始

### 1. 环境要求

- Python 3.10+（或直接使用打包好的 `PhotoTransDrive.exe`）
- Windows / Linux / macOS

### 2. 启动服务

```bash
# 最简启动（默认端口 47810，配对码自动生成并持久化）
python server.py

# 指定网盘根目录
python server.py --root D:\MyDrive

# 指定普通配对码（只读权限）
python server.py --pair-code 123456

# 指定管理员配对码（完整权限）
python server.py --admin-code 888888

# 启动并自动打开网盘目录
python server.py --open
```

### 3. 启动成功标志

```
==============================================================
  PhotoTrans 网盘已就绪
==============================================================
  网盘目录   : D:\MyDrive
  已用空间   : 11 B
  局域网地址 : 192.168.1.3:47810
               ↑ 手机填这个地址（需与电脑同一 WiFi）
  普通配对码 : 935200（只读：仅浏览/下载）
  管理员配对码: 487213（完整权限：上传/删除）
  IPv6 地址  : [2409:...]:47810（异地连接用）
--------------------------------------------------------------
  手机端操作：打开 PhotoTrans → 添加电脑
            → 输入上方「局域网地址」和「配对码」
  只读设备只能下载，上传/删除请用管理员配对码
  停止服务  ：按 Ctrl+C
==============================================================
```

### 4. 安卓 App 连接

1. 打开 PhotoTrans App
2. 进入"网盘"功能
3. 输入启动面板上的「局域网地址」和「配对码」（或等待自动发现）
4. 完成绑定后即可传输文件

---

## 启动参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--config` | 配置文件路径 | `~/.phototransdrive/config.json` |
| `--root` | 网盘根目录 | `~/PhotoTransDrive` |
| `--port` | TCP 监听端口 | `47810` |
| `--pair-code` | 普通配对码（只读权限） | 自动生成并持久化，重启不变 |
| `--admin-code` | 管理员配对码（完整权限） | 自动生成并持久化，重启不变 |
| `--open` | 启动后自动打开网盘目录 | - |
| `--status` | 显示状态摘要（配置/设备/占用空间）后退出 | - |
| `--list-devices` | 列出所有已绑定设备后退出 | - |
| `--remove-device` | 解绑指定设备后退出（需设备 ID） | - |

### 常用命令示例

```bash
python server.py                              # 启动服务
python server.py --root D:\MyDrive            # 指定网盘目录
python server.py --pair-code 123456           # 指定普通配对码（只读）
python server.py --admin-code 888888          # 指定管理员配对码（完整权限）
python server.py --open                       # 启动并打开网盘目录
python server.py --status                     # 查看状态摘要
python server.py --list-devices               # 查看已绑定设备
python server.py --remove-device <设备ID>     # 解绑设备
```

---

## 配置说明

### 配置文件位置

```
Windows: %USERPROFILE%\.phototransdrive\config.json
Linux:   ~/.phototransdrive/config.json
macOS:   ~/.phototransdrive/config.json
```

### 配置文件格式

```json
{
  "root": "D:\\MyDrive",
  "port": 47810,
  "pair_code": "123456",
  "admin_code": "888888"
}
```

### 配置优先级

命令行参数 > 配置文件 > 环境变量 > 默认值

### 配对码持久化

普通配对码和管理员配对码首次启动时自动生成并保存到配置文件，**重启后保持不变**，手机配对一次永久有效。

- 想换普通配对码：`python server.py --pair-code 新码`
- 想换管理员配对码：`python server.py --admin-code 新码`
- 查看当前配对码：`python server.py --status`

> 两个配对码不能相同，否则无法区分权限，启动时会报错。

---

## 权限分级

服务端用两套配对码区分设备权限：

| 配对码 | 权限 | 允许操作 | 拒绝操作 |
|--------|------|----------|----------|
| 普通配对码（`--pair-code`） | 只读 | `LIST`（浏览）、`DOWNLOAD`（下载文件/文件夹） | `MKDIR`、`UPLOAD`、`DELETE` |
| 管理员配对码（`--admin-code`） | 完整 | 所有操作 | 无 |

- 用普通配对码绑定的设备，只能**浏览和下载**，任何修改/删除都会收到 `PT-ERR 只读权限，不能修改或删除`。
- 用管理员配对码绑定的设备，拥有**完整权限**（上传、建目录、删除、改动）。
- 权限在配对时确定，绑定后固定（换配对码需重新配对）。

---

## 配对设备

### 方式一：手动配对码

```bash
# 只读设备（只能下载）
python server.py --pair-code 123456

# 管理员设备（完整权限）
python server.py --admin-code 888888
```

App 输入「局域网地址」+「配对码」即可绑定，权限按配对码类型自动确定。

### 方式二：自动发现

手机端广播 `PT-DISCOVER` 到 UDP 47811，服务端回复 `PT-DRIVE|<port>`。

### 管理已绑定设备

```bash
python server.py --list-devices               # 查看已绑定设备（含权限）
python server.py --remove-device <设备ID>     # 解绑设备
```

---

## 协议参考

### 端口

- TCP 47810 - 网盘服务
- UDP 47811 - 设备发现

### 认证流程

```
# 配对（普通配对码=只读，管理员配对码=完整权限）
客户端: PT-PAIR <deviceId> <pairCode> <deviceName>
服务端: PT-PAIR-OK <deviceToken>

# 设备令牌认证
客户端: PT-AUTH <deviceId> <deviceToken>
服务端: PT-AUTH-OK <deviceName> | PT-AUTH-FAIL
```

### 文件操作

```
# 列出目录
客户端: LIST /path
服务端: PT-JSON <len>\n<json_array>

# 新建目录（需完整权限）
客户端: MKDIR /path
服务端: PT-OK | PT-ERR <reason>

# 上传文件（需完整权限）
客户端: UPLOAD /path/name Content-Length <n>
服务端: PT-OK (就绪)
客户端: <file_bytes>
服务端: PT-OK | PT-ERR <reason>

# 下载文件
客户端: DOWNLOAD /path/name
服务端: PT-OK Content-Length <n>\n<file_bytes>

# 删除（需完整权限）
客户端: DELETE /path/name
服务端: PT-OK | PT-ERR <reason>
```

> 只读设备执行 MKDIR/UPLOAD/DELETE 会收到 `PT-ERR 只读权限，不能修改或删除`。

### 目录列表 JSON 格式

```json
[
  {
    "name": "folder",
    "isDir": true,
    "size": 0,
    "mtime": 1700000000
  },
  {
    "name": "file.txt",
    "isDir": false,
    "size": 1234,
    "mtime": 1700000000
  }
]
```

---

## 故障排除

### 端口被占用

服务端启动时会自动检测并提示，或手动排查：

```bash
# Windows
netstat -ano | findstr 47810

# Linux/macOS
lsof -i :47810
```

解决：换一个端口，如 `--port 47812`（注意 47811 是 UDP 发现端口，别占用）。

### 配对码相同报错

如果看到"普通配对码与管理员配对码相同"，请分别指定两个不同的配对码：

```bash
python server.py --pair-code 123456 --admin-code 888888
```

### 只读设备无法上传/删除

这是权限限制。用管理员配对码重新绑定即可获得完整权限。

### 文件上传失败

- 检查文件大小限制（默认 100GB）
- 检查路径是否合法
- 查看日志 `~/.phototransdrive/logs/phototrans_drive.log`

### 日志位置

```
Windows: %USERPROFILE%\.phototransdrive\logs\phototrans_drive.log
Linux:   ~/.phototransdrive/logs/phototrans_drive.log
macOS:   ~/.phototransdrive/logs/phototrans_drive.log
```

---

## 安全特性

| 特性 | 说明 |
|------|------|
| 路径安全 | 防穿越、符号链接、junction |
| 文件名安全 | 过滤非法字符、BIDI 控制字符、超长名 |
| 令牌安全 | 256-bit 随机令牌，常量时间比较 |
| 权限分级 | 普通配对码只读，管理员配对码完整权限 |
| 认证锁定 | 5 次失败/5 分钟/IP |
| 并发限制 | 16 全局，4/IP |
| 原子写入 | 防崩溃损坏配置文件 |
| 日志轮转 | 10MB，保留 5 个备份 |

---

## 已知限制

| 限制 | 说明 |
|------|------|
| 配对码长度 | 6 位数字（暴力破解空间 100 万） |
| 权限粒度 | 只读/完整两级，不支持按目录细分权限 |

---

## 版本历史

| 版本 | 日期 | 主要变更 |
|------|------|----------|
| v1.2.0 | 2026-09 | 移除邮件授权，改为双配对码权限分级（普通只读 / 管理员完整） |
| v1.1.0 | 2026-09 | 安全加固（路径/文件名/认证）+ 使用顺手优化（配对码持久化、设备管理、状态查询、启动面板） |
| v1.0.0 | 2026-09 | 初始版本：核心传输 + 邮件授权 + Web UI |

---

## 许可证

MIT License
