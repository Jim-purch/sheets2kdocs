# 部署指南

## 方式一：Docker Compose（推荐）

### 1. 准备

```bash
git clone https://github.com/<you>/sheets2kdocs.git
cd sheets2kdocs
cp .env.example .env
```

编辑 `.env`：

```ini
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<强密码>
SECRET_KEY=<python3 -c "import secrets;print(secrets.token_hex(32))">
BASE_URL=http://<服务器IP或域名>:8300
KDOCS_APP_ID=<APPID>
KDOCS_APP_KEY=<APPKEY>
```

### 2. 启动

```bash
docker compose up -d --build
docker compose logs -f          # 查看日志
curl http://127.0.0.1:8300/healthz
```

### 3. 升级

```bash
git pull
docker compose up -d --build
```

数据（SQLite + 令牌）都在 `./data` 目录，升级不丢失。

### 4. 卸载

```bash
docker compose down            # 加 -v 不需要（volume 是 bind mount）
rm -rf data .env               # 如确认不再需要数据
```

## 方式二：systemd + venv（无 Docker 环境）

```bash
cd /opt
git clone https://github.com/<you>/sheets2kdocs.git
cd sheets2kdocs
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env && vim .env
```

`/etc/systemd/system/sheets2kdocs.service`：

```ini
[Unit]
Description=sheets2kdocs sync connector
After=network.target

[Service]
WorkingDirectory=/opt/sheets2kdocs
ExecStart=/opt/sheets2kdocs/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8300
Restart=always
RestartSec=5
User=www-data

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now sheets2kdocs
```

> 注意：`BASE_URL` 要填对外地址（Nginx 域名），应用本身监听 127.0.0.1 由 Nginx 转发。

## Nginx 反向代理（生产建议）

```nginx
server {
    listen 443 ssl http2;
    server_name sync.example.com;

    # ssl_certificate     /etc/nginx/certs/sync.example.com.pem;
    # ssl_certificate_key /etc/nginx/certs/sync.example.com.key;

    location / {
        proxy_pass http://127.0.0.1:8300;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

对应 `.env`：`BASE_URL=https://sync.example.com`（不带末尾斜杠）。

## 安全清单

- [ ] `ADMIN_PASSWORD` 使用强密码
- [ ] `SECRET_KEY` 随机且不外泄（泄露 = 会话可被伪造）
- [ ] 生产环境走 HTTPS
- [ ] `.env` 权限 `chmod 600`，绝不提交仓库
- [ ] 云服务器安全组只放行必要端口；不需要外网访问时端口映射绑 `127.0.0.1`
- [ ] 定期备份 `data/`（含任务配置、日志、OAuth 令牌）

## Google 侧准备

| 模式 | 要求 |
|---|---|
| public（默认） | 表格「分享 → 知道链接的任何人 → 查看者」，任务里粘贴链接即可 |
| api_key | `.env` 配 `GOOGLE_API_KEY`（Google Cloud 控制台启用 Sheets API）；表格仍需链接共享 |
| sa | Google Cloud 创建 Service Account → 下载 JSON → `.env` 配 `GOOGLE_SERVICE_ACCOUNT_JSON`（内联 JSON 或路径）；把表格共享给 SA 的邮箱（查看者） |

## 故障排查

```bash
# 看服务日志
docker compose logs -f --tail 100

# 手动触发一次同步并观察日志输出
curl -s http://127.0.0.1:8300/healthz

# 直接运行同步引擎（排错用）
cd /opt/sheets2kdocs && .venv/bin/python -c "
from app import db, sync_engine
db.init_db('./data/sheets2kdocs.db')
print(sync_engine.run_task(1))
"
```

常见问题：管理页「运行日志」中的 error 字段会带出 Google / Kdocs 的原始错误信息，按提示处理（共享设置、令牌过期、字段缺失等）。
