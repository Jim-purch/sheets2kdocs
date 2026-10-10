# sheets2kdocs

**Google Sheets → 金山文档（WPS Kdocs）智能表格 定时同步连接器**

一个自托管的开源同步服务：定时（或手动）把 Google Sheets 工作表的数据同步到金山文档的智能表格（.ksheet 数据表），自带 Web 管理页面。

> [English README](README_EN.md)

## 功能特性

- **两种同步模式**
  - `upsert`（增量）：按唯一键列比对，自动新建 / 更新 / 删除目标记录
  - `replace`（全量）：清空目标数据表后重写
- **多种 Google 读取方式**：公开分享链接（零配置）· API Key · Service Account（可读私有表）
- **字段映射**：源表头 → 目标字段改名对应，同名自动匹配；目标缺字段可自动创建
- **定时计划**：固定间隔（分钟）或 5 段式 cron；也可仅手动触发
- **管理页面**：任务增删改查、连接测试、运行日志、同步统计（新建/更新/删除行数）
- **Kdocs OAuth**：引导式授权 + access_token 自动刷新（refresh_token 90 天有效）
- **轻量部署**：FastAPI + SQLite，单容器运行，约 60MB 内存

## 工作原理

```
Google Sheets ──① 拉取──▶ sheets2kdocs ──② 记录级写入──▶ 金山文档智能表格
                          （FastAPI + APScheduler + SQLite）      developer.kdocs.cn
浏览器 ──▶ Nginx ──▶ 管理页面（.env 账号密码登录）
```

- Google 侧：公开表走 `gviz` CSV 导出；API Key / Service Account 走 Sheets API v4
- Kdocs 侧：走[金山文档开放平台](https://developer.kdocs.cn)官方 OpenAPI（记录级 CRUD），非逆向、非爬虫
- 全程只做单向同步：Google Sheets 是数据源，Kdocs 智能表格是目标

## 快速开始（Docker）

```bash
git clone https://github.com/<you>/sheets2kdocs.git
cd sheets2kdocs

cp .env.example .env
# 编辑 .env：设置管理账密、SECRET_KEY、KDOCS_BACKEND=cli（免企业认证推荐）

docker compose up -d --build
# 访问 http://<服务器IP>:8300
```

### 前置条件

1. **Kdocs 侧**（二选一）：
   - **cli 模式（推荐）**：无需企业认证。部署后在管理页「Kdocs 连接」粘贴个人 Token（获取方式：登录 [kdocs.cn](https://www.kdocs.cn/latest) → 右上角主菜单 → 金山文档Skill → 复制 Token）
   - **openapi 模式**：金山文档开放平台企业认证 + 创建应用，详见 [docs/KDOCS_SETUP.md](docs/KDOCS_SETUP.md)
2. **Google 表格**：公开同步只需把表格设为「知道链接的任何人 → 查看者」
3. 目标文件：多维表格（.dbt）或智能表格（.ksheet）内的**数据表** sheet

### 两种 Kdocs 接入模式

| 模式 | 认证要求 | 适用 |
|---|---|---|
| **cli（推荐，免企业认证）** | WPS 个人账号 Token（`kdocs.cn` → 菜单 → 金山文档Skill → 复制，有效期约 1 年） | 个人/小团队，目标为多维表格或智能表格数据表 |
| openapi | 金山文档开放平台企业认证 + 创建应用 | 已有企业开发者资质，需要正式应用配额 |

两种模式功能一致（记录级增删改查、字段自动创建、定时同步）。`.env` 里 `KDOCS_BACKEND=cli` 或 `openapi` 切换；cli 模式的 Token 在管理页「Kdocs 连接」里粘贴保存、热更新。

## 使用

1. 打开管理页面，用 `.env` 里的账密登录
2. 进入 **Kdocs 连接 / 授权** 页：
   - cli 模式：粘贴个人 Token → 保存并测试
   - openapi 模式：点击「打开 Kdocs 授权页面」完成授权（或手动粘贴 code / refresh_token）
3. **新建同步任务**：
   - 粘贴 Google 表格链接 → 点「读取源表头」测试连接
   - 选择目标文件和数据表（也可一键「用源表头新建数据表」）
   - 检查字段映射，选择 upsert 键列
   - 设置定时计划（间隔分钟数或 cron），保存
4. 点「▶ 运行」立即同步一次，到 **运行日志** 查看结果

## 配置参考

| 环境变量 | 必填 | 说明 |
|---|---|---|
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | ✅ | 管理页面登录凭据 |
| `SECRET_KEY` | ✅ | 会话签名密钥（`python3 -c "import secrets;print(secrets.token_hex(32))"`） |
| `BASE_URL` | ✅* | 对外访问地址，用于 OAuth 回调（*手动粘贴 code 方式可不填） |
| `KDOCS_APP_ID` / `KDOCS_APP_KEY` | ✅ | 开放平台应用凭据 |
| `GOOGLE_API_KEY` | 可选 | API Key 读取模式 |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | 可选 | Service Account JSON（内联或文件路径），读取私有表 |
| `PORT` / `DATA_DIR` / `TZ` | 可选 | 默认 `8300` / `./data` / `Asia/Shanghai` |

## 同步语义

| 模式 | 行为 | 适用 |
|---|---|---|
| `upsert` | 源表有而目标无 → 新建；键相同但内容变 → 更新；目标有而源表无 → **删除** | 持续双向维护的主数据 |
| `replace` | 先清空目标数据表全部记录，再整体写入 | 纯展示型快照 |

注意事项：

- upsert 模式的「删除」指的是**删除目标表中的记录**（键值不在最新源表中的行），任务表单里有醒目提示
- 值默认以文本写入；勾选「纯数字转数值」后，形如 `123`、`1.5` 的值会转为数字，但**前导零会被保留**（如编号 `007` 仍是文本）
- **单元格字符上限实测为 30000**（Kdocs 硬限制，写超会报 `Cell char count limit exceeded`）。任务默认截断阈值 20000，可在表单调整（0–30000）；配合「HTML 清理」（剥除 class/注释/空白）通常能在上限内保留完整内容
- 目标表缺少字段时会自动创建 `SingleLineText` 字段（可关闭）
- Google 侧首行为表头；空表头列自动命名 `col_N`

## 生产部署建议

- 用 Nginx 反向代理 + HTTPS（示例见 [docs/DEPLOY.md](docs/DEPLOY.md)），不要裸 HTTP 暴露公网
- `docker-compose.yml` 中如仅需本机访问，把端口映射改为 `127.0.0.1:8300:8300`
- 定期备份 `data/` 目录（SQLite，含任务配置、日志、OAuth 令牌）
- `.env` 权限设为 `600`，切勿提交到代码仓库

## 常见问题

**Q: 提示「gviz 返回了 HTML」？**
表格没有公开分享。Google Sheets → 分享 → 一般访问权限 → 「知道链接的任何人」→ 查看者。

**Q: Kdocs 令牌/Token 过期了？**
cli 模式的个人 Token 有效期约 1 年，到期到「Kdocs 连接」页重新粘贴即可（保存即生效，无需重启）；openapi 模式 refresh_token 90 天，过期需重新授权。另检查服务器时间是否准确（NTP）。

**Q: 文件列表里看不到目标表格？**
文件列表只展示授权账号「我的文档」下的文件；共享给我的文件或企业盘文件请手动填 file_token（智能表格内右键标签 → 文档信息 → 文件ID）。

**Q: 同步后数值变成了文本？**
勾选任务的「纯数字字符串转数值」；或把目标字段在 Kdocs 里设为数字类型后重跑。

**Q: 限流是多少？**
开放平台测试应用 10,000 次/天（正式应用 10,000,000 次/天），普通定时同步远用不完。

## 开发

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
uvicorn app.main:app --reload --port 8300
pytest
```

## License

[MIT](LICENSE)
