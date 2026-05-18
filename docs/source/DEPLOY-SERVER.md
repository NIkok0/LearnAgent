# 生产部署与配置（服务器）

本文档只描述 **生产环境部署步骤、路径约定与环境变量**，便于在目标机上按序执行。  
**技术框架、生产拓扑、与《选型》的差异说明**见同目录 [watermark-java-backend-tech-selection.md](./watermark-java-backend-tech-selection.md)。  
**上线检查表、已知问题、需求对照（R 表）、测试用例与 Maven 命令**见 [REQUIREMENTS-CHECKLIST-AND-TEST-CASES.md](./REQUIREMENTS-CHECKLIST-AND-TEST-CASES.md)。
**告警排障与回滚 SOP**见 [RUNBOOK.md](./RUNBOOK.md)。  
**安全策略基线**见 [SECURITY-BASELINE.md](./SECURITY-BASELINE.md)。  
**API 契约与错误模型**见 [API-CONTRACT.md](./API-CONTRACT.md)。

> 文档边界约定：本文件只保留“生产部署与配置命令”，不承载架构决策、API 字段细节与安全策略正文。

将 **`backend-java`** 发布到自有服务器，经 **HTTPS + 域名** 对外提供服务（示例域名 **`loadsadar.asia`**，可替换）。**无法替你登录服务器**，实操命令需在目标机上执行。

### 示例环境（维护者可改为占位符）

| 项 | 值 |
|----|-----|
| 服务器公网 IP | **`162.14.123.75`**（腾讯云 CVM，示例） |
| 站点域名 | **`www.loadsadar.asia`**（Nginx → **Spring Boot :8080**，Thymeleaf） |
| API 域名（可与上同源或分域名） | **`api.loadsadar.asia`** |
| MySQL / Redis | **本机 127.0.0.1**（安全组勿对公网开放 3306/6379） |
| 对象存储 | **腾讯云 COS**（`WM_STORAGE_BACKEND=cos`） |

> **隐私提示**：若仓库会公开，不建议长期把真实公网 IP 写死在文档里；可改为占位符或仅存私有 Wiki。

---

## 0. 公网部署实操流水（按顺序一条条执行）

以下假设你已用 **`ssh 用户名@162.14.123.75`** 登录 **Linux 服务器**（Ubuntu 系举例），且 **DNS** 已为 **`api.loadsadar.asia`** 指向本机 IP。
**Jar 包**需先在个人电脑上 **`mvn -pl web -am package -DskipTests`** 打好，再用 **`scp`** 传到服务器（见第 7 步）；也可在服务器上装 Maven 现打，此处按「上传 Jar」写法。

### 0）在云控制台（算服务器周边，必做）

1. **安全组 / 防火墙**：入站放行 **`22`**（SSH）、**`80`**（HTTP）、**`443`**（HTTPS）。
2. **不要**对公网放行 **3306（MySQL）**、**6379（Redis）**、**9000（MinIO）**，除非你很清楚风险。

### 1）登录后更新系统并装基础软件

```bash
sudo apt update
sudo apt install -y openjdk-17-jre-headless nginx curl
sudo apt install -y certbot python3-certbot-nginx
```

（若你用 **Docker** 跑 API，再装 **`docker.io`**；本文主流程用 **systemd + Jar**。）

### 2）安装并启动 MySQL

```bash
sudo apt install -y mysql-server
sudo systemctl enable --now mysql
```

然后进入 MySQL **创建库和用户**（示例，密码请改成自己的）：

```bash
sudo mysql
```

在 `mysql>` 里执行（示例）：

```sql
CREATE DATABASE IF NOT EXISTS watermark CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'watermark_app'@'localhost' IDENTIFIED BY '这里改成强密码';
GRANT ALL PRIVILEGES ON watermark.* TO 'watermark_app'@'localhost';
FLUSH PRIVILEGES;
EXIT;
```

### 3）安装并启动 Redis

```bash
sudo apt install -y redis-server
sudo systemctl enable --now redis-server
redis-cli ping
```

最后一行应返回 **`PONG`**。

### 4）（可选）本机 MinIO 或改用 COS

- **MinIO**：按官方文档或 Docker 安装，监听内网；**`WM_MINIO_*`** 指向它。
- **COS**：不设本机 MinIO，在环境变量里配 **`WM_STORAGE_BACKEND=cos`** 与 **`WM_COS_*`**。

### 5）创建运行目录与数据目录

```bash
sudo mkdir -p /opt/watermark-api
sudo mkdir -p /opt/watermark-data/instance
sudo chown -R $USER:$USER /opt/watermark-api /opt/watermark-data
```

（若用 **`www-data`** 跑服务，后面把 **`chown`** 改成 **`sudo chown -R www-data:www-data ...`**。）

### 6）准备环境变量文件

把仓库里的 **`deploy/watermark-api.env.example`** 拷到服务器（或用 `nano` 新建），保存为：

```bash
sudo nano /opt/watermark-api/watermark-api.env
```

至少保证包含（值按你实际修改）：

- **`WM_PROFILE=prod`**
- **`WM_DATASOURCE_URL`** / **`WM_DATASOURCE_USERNAME`** / **`WM_DATASOURCE_PASSWORD`**（与第 2 步 MySQL 一致）
- **`WM_REDIS_HOST=127.0.0.1`**、**`WM_REDIS_PORT=6379`**
- **`WM_INSTANCE_PATH=/opt/watermark-data/instance`**
- **`WM_STORAGE_BACKEND`**、**`WM_MINIO_*`** 或 COS 相关变量

然后：

```bash
sudo chmod 600 /opt/watermark-api/watermark-api.env
```

在已克隆本仓库的服务器上，可用一键脚本校验环境变量是否齐全、MySQL/Redis（及 MinIO）端口是否可达；API 上线后还可带上公网地址检查 **`/actuator/health`**：

```bash
cd /path/to/watermarking/backend-java
chmod +x scripts/verify-config.sh
./scripts/verify-config.sh --env-file /opt/watermark-api/watermark-api.env --strict
# 服务已跑通 Nginx 后，可加： --api-url https://api.loadsadar.asia
```

在 **Windows** 上可用同目录下的 **`scripts/verify-config.ps1`**（参数 **`-EnvFile`、`-ApiUrl`、`-Strict`**，与 bash 版语义一致）。

### 7）上传 Jar 到服务器

在**你的电脑**上（已打好 `web/target/web-*.jar`）：

```bash
scp web/target/web-*.jar 你的用户名@162.14.123.75:/opt/watermark-api/app.jar
```

在**服务器**上确认文件存在：

```bash
ls -la /opt/watermark-api/app.jar
```

### 8）安装 systemd 服务

把仓库 **`deploy/systemd/watermark-api.service.example`** 拷到服务器后：

```bash
sudo cp /path/to/watermark-api.service.example /etc/systemd/system/watermark-api.service
sudo nano /etc/systemd/system/watermark-api.service
```

确认 **`ExecStart`** 里是 **`/opt/watermark-api/app.jar`**，**`EnvironmentFile=`** 指向 **`/opt/watermark-api/watermark-api.env`**，**`User=`** 与目录权限一致。

启用并启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now watermark-api
sudo systemctl status watermark-api
```

若失败，看日志：

```bash
journalctl -u watermark-api -e --no-pager
```

### 9）在服务器本机确认 API 已监听

```bash
curl -sS http://127.0.0.1:8080/actuator/health
```

应返回含 **`"status":"UP"`** 的 JSON。

### 10）配置 Nginx 反代

把仓库 **`deploy/nginx-api.loadsadar.asia.conf.example`** 拷到服务器，例如：

```bash
sudo cp /path/to/nginx-api.loadsadar.asia.conf.example /etc/nginx/sites-available/api.loadsadar.asia.conf
sudo ln -sf /etc/nginx/sites-available/api.loadsadar.asia.conf /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

（若还没有 TLS 证书，可先按示例文件里说明 **只开 80** 调试，或让 **certbot** 自动改配置。）

### 11）申请 HTTPS 证书（Let's Encrypt）

```bash
sudo certbot --nginx -d api.loadsadar.asia
```

按提示完成；再执行：

```bash
sudo nginx -t && sudo systemctl reload nginx
```

### 12）从外网自测

在你自己电脑浏览器打开：

- **`https://api.loadsadar.asia/actuator/health`**
- **`https://api.loadsadar.asia/swagger-ui.html`**

或在服务器上：

```bash
curl -sS https://api.loadsadar.asia/actuator/health
```

> **`www.loadsadar.asia`**：由 **`30-deploy-nginx.sh`** 写入的站点与 `api` 一样 **`proxy_pass` 到 `127.0.0.1:8080`**。Thymeleaf 页面、**`/static/**`**、**`/api/v1/**`** 均由 Spring Boot 提供；**不再部署 Flask。** 生产环境建议在 **`watermark-api.env`** 中设置 **`SERVER_FORWARD_HEADERS_STRATEGY=framework`**（`20-deploy-api.sh` 已写入）；按是否跨子域直连 API 选择 **`WM_SESSION_COOKIE_SAME_SITE`**（同源反代常用 **`lax`**，跨子域见 `application-prod.yml` 默认 **`none`**）。

### 13）部署 Python Worker（消费水印任务，必需）

`POST /api/v1/jobs/watermark` 只是把任务写进 Redis Stream，实际嵌水印由 `watermark.worker.redis_stream_worker` 做。不部署 Worker 时，任务会卡在 `queued`。

```bash
# 环境变量（关键变量见 deploy/watermark-worker.env.example）
sudo cp deploy/watermark-worker.env.example /opt/watermark-api/watermark-worker.env
sudo chmod 600 /opt/watermark-api/watermark-worker.env
sudo nano /opt/watermark-api/watermark-worker.env   # 填 SQLALCHEMY_DATABASE_URI、COS、INSTANCE_PATH 等

# systemd
sudo cp deploy/systemd/watermark-worker.service.example /etc/systemd/system/watermark-worker.service
sudo nano /etc/systemd/system/watermark-worker.service
# 修改：
#   WorkingDirectory=/opt/watermark-app
#   ExecStart=/opt/miniconda3/envs/watermark/bin/python -m watermark.worker.redis_stream_worker
#   User=按实际

sudo systemctl daemon-reload
sudo systemctl enable --now watermark-worker
sudo systemctl status watermark-worker
journalctl -u watermark-worker -e --no-pager
```

### 14）首次创建超级管理员（空库一次性）

`/api/v1/admin/**` 需要 `ROLE_ADMIN`，但 Flyway 初始化后是空库。两种方式二选一：

**方式 A · 走 Bootstrap Runner（新部署推荐）**

在 `/opt/watermark-api/watermark-api.env` 里临时把 bootstrap 三件套填好并启用，然后重启服务：

```
WM_BOOTSTRAP_ADMIN_ENABLED=true
WM_BOOTSTRAP_ADMIN_USERNAME=admin
WM_BOOTSTRAP_ADMIN_EMAIL=admin@loadsadar.asia
WM_BOOTSTRAP_ADMIN_PASSWORD=强密码
```

```bash
sudo systemctl restart watermark-api
journalctl -u watermark-api -e --no-pager | grep "Admin bootstrap"
```

日志里看到 `created super_admin user 'admin'` 后：

1. 把 `WM_BOOTSTRAP_ADMIN_ENABLED` 改回 `false`；
2. 从 env 文件里删掉 `WM_BOOTSTRAP_ADMIN_PASSWORD`；
3. `sudo systemctl restart watermark-api`。

**方式 B · 手工 SQL 插入**

用 BCrypt 工具在本机预先算好哈希，再在服务器上：

```sql
INSERT INTO users (username, email, password, is_admin, role, is_active, is_embed, is_extract, created_at, updated_at)
VALUES ('admin', 'admin@loadsadar.asia', '$2a$10$....bcrypt hash....', 1, 'super_admin', 1, 1, 1, NOW(6), NOW(6));
```

---

## 腾讯云 CVM 补充说明

你用的是 **腾讯云服务器（CVM）** 时，和通用 Linux 相比，多关注下面几项即可（与上文 **§0** 步骤叠加，不重复装软件则跳过对应步）。

| 项目 | 在腾讯云哪里操作 | 说明 |
|------|------------------|------|
| **安全组** | 控制台 → **云服务器** → 实例 → **安全组** → 入站规则 | 放行 **TCP 22**（SSH）、**80**、**443**；**不要**对全网放行 **3306 / 6379 / 9000**（除非你有专线/VPN 管控） |
| **公网 IP** | 实例详情页 | 确认与 DNS **A 记录**（如 **`api` → 公网 IP**）一致；**弹性公网 IP** 若变更需同步改解析 |
| **域名解析** | **DNSPod**（或域名所在注册商） | **`api.loadsadar.asia`** 做 **A** 到 CVM 公网 IP；若前面套 **CDN**，注意回源与 HTTPS 模式（与源站证书匹配） |
| **MySQL** | **本机安装** 或 **TencentDB for MySQL** | 用云数据库时，**`WM_DATASOURCE_URL`** 填控制台给出的 **内网地址**（同地域 VPC 更安全），安全组放行 **CVM → 数据库 3306** |
| **Redis** | **本机安装** 或 **TencentDB for Redis** | 同上，**`WM_REDIS_HOST`** 用内网地址；安全组放行 **CVM → Redis 端口** |
| **对象存储** | **COS**（推荐与 Java 已支持的 COS 路径一致） | **`WM_STORAGE_BACKEND=cos`**，配置 **`WM_COS_*`**；**不必**在 CVM 上再装 MinIO（除非你想自建） |
| **HTTPS 证书** | 仍可用 **certbot + Nginx**（上文 §11） | 也可使用 **腾讯云 SSL 证书** 上传到 Nginx，二选一即可 |

**轻量应用服务器**与 **CVM** 控制台入口不同，但思路相同：**防火墙/安全组放行 80/443 + 本机或云数据库 + Nginx 反代**。

**Windows Server 镜像与 SSH**：安全组里勾选 **「Linux 登录 (22)」** 只表示**入站流量可以到达实例网卡**；若系统内**未安装或未启动 OpenSSH Server**，本机仍**不会在 22 端口监听**，此时客户端常见现象为 **`ssh: ... port 22: Connection refused`**（与密码错误不同）。处理方式二选一：**（1）** 在服务器上启用 **OpenSSH 服务器**（可选功能，服务一般为 `sshd` / `OpenSSH SSH Server`）；**（2）** 用 **远程桌面连接 3389** 登录（你的规则里已放行），再按需安装 OpenSSH 或改用 **WSL / 虚拟机里的 Linux** 跑本文档中的 **Ubuntu / Nginx / Jar** 流程。

### Windows：安装 OpenSSH 服务器、启动服务、放行本机防火墙

以下在 **已用远程桌面登录到服务器** 的前提下操作；涉及服务的步骤需 **管理员** 权限（PowerShell「以管理员身份运行」）。

**方式 A — 图形界面（Windows 10 / 11 / Server 2019+ 常见）**

1. 打开 **设置** → **应用** → **可选功能**（或「已安装的功能」旁的 **查看功能**）。
2. 搜索 **OpenSSH 服务器**（英文界面为 **OpenSSH Server**），勾选并 **安装**。
3. 安装完成后打开 **服务**（`Win + R` → 输入 `services.msc`）：
   - 找到 **OpenSSH SSH Server**；
   - 右键 → **启动**；再右键 → **属性** → **启动类型** 选 **自动**。

**方式 B — PowerShell（管理员）**

先查看精确包名（不同版本后缀可能略有差异）：

```powershell
Get-WindowsCapability -Online | Where-Object Name -like 'OpenSSH.Server*'
```

安装（将 `Name` 换成上一条输出里 **State** 为 **NotPresent** 的完整名称，常见为 `OpenSSH.Server~~~~0.0.1.0`）：

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
```

启动并设为开机自启：

```powershell
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic
```

**Windows 防火墙放行入站 TCP 22**

安装 OpenSSH 后，系统有时会**自动**添加放行规则。若没有或你改过端口，在 **管理员 PowerShell** 中执行（端口改为非 22 时，把 `LocalPort` 与云安全组一并改成一致）：

```powershell
Get-NetFirewallRule -Name *ssh* -ErrorAction SilentlyContinue | Format-Table Name, Enabled, Direction, Action
New-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -DisplayName "OpenSSH SSH Server (sshd)" `
  -Enabled True -Direction Inbound -Action Allow -Protocol TCP -LocalPort 22
```

若提示规则名已存在，说明已有入站放行，可不再新建。

**验证本机是否在监听**

```powershell
Get-NetTCPConnection -LocalPort 22 -State Listen -ErrorAction SilentlyContinue
```

有输出则表示本机已监听；此时再配合腾讯云安全组放行 **22**，从外网执行 `ssh 用户名@公网IP` 应不再出现 **Connection refused**（若仍失败，再查密码、密钥、`sshd_config` 是否禁止密码登录等）。

**改 SSH 端口（可选）**

编辑 **`C:\ProgramData\ssh\sshd_config`**，取消注释并设置 `Port 2222`（示例），在 **Windows 防火墙** 与 **腾讯云安全组** 中同样放行 **2222**，然后 `Restart-Service sshd`，客户端使用 `ssh -p 2222 用户@IP`。

---

## 文档修订备忘

| 日期 | 摘要 |
|------|------|
| 2026-05-12 | 明确文档角色：本文为**技术框架与拓扑说明** + **公网部署实操流水**；**生产检查 / 问题 / 测试**迁至 [REQUIREMENTS-CHECKLIST-AND-TEST-CASES.md](./REQUIREMENTS-CHECKLIST-AND-TEST-CASES.md)。 |
| 2026-05-12 | 收窄为**生产部署与配置**：架构与拓扑迁至 [watermark-java-backend-tech-selection.md](./watermark-java-backend-tech-selection.md)。 |
