# Docker 部署与运维

## 前置条件

- Debian 12 / Ubuntu 22.04+，已安装 Docker Engine 与 Compose 插件：

```bash
docker --version
docker compose version
```

## 部署步骤

1. 上传项目目录到服务器（或 git clone）后进入项目根目录。
2. 创建配置：

```bash
cp .env.example .env
vi .env
```

必须修改：

| 变量 | 说明 |
|---|---|
| `API_KEY` | 入站 API 鉴权密钥，至少 16 位随机串 |
| `ADMIN_PASSWORD` | 管理员初始密码（首次启动写入数据库哈希） |
| `SECRET_KEY` | 敏感凭证加密密钥；不设置会自动生成到 `data/secret.key`（删除会导致已存凭证无法解密） |

3. 数据目录属主必须与容器内用户一致（容器以 uid 1000 运行）：

```bash
mkdir -p data logs
chown -R 1000:1000 data logs
```

4. 构建并启动（PAAS 使用 `network_mode: host`，直接监听宿主机 8000 端口，便于走宿主机透明代理访问 Telegram）：

```bash
docker compose up -d --build
docker compose logs -f paas
```

> 网页端「更新与卸载」一键模式默认开启：compose.yaml 会向容器挂载 Docker socket 与项目目录（`PAAS_HOST_PROJECT`，默认 `/opt/paas`）。这等价于把宿主机 Docker 控制权交给容器，仅建议个人自用服务器；如需关闭，在 `.env` 设置 `PAAS_MAINTENANCE=0` 并移除 compose.yaml 中对应挂载后重建。

5. 打开 `http://<服务器IP>:8000/admin` 配置机器人。

## Docker Hub 慢（国内服务器）

在 `/etc/docker/daemon.json` 配置镜像加速并重启 Docker（会短暂重启所有容器）：

```json
{
  "registry-mirrors": [
    "https://docker.1ms.run",
    "https://docker.m.daocloud.io",
    "https://docker.xuanyuan.me"
  ]
}
```

```bash
systemctl restart docker
docker pull alpine:3.20   # 验证加速源可用
```

若加速源全部失效，恢复 `daemon.json` 备份后直连 Docker Hub。

## 数据与备份

- 数据卷映射：`./data:/app/data`（SQLite + 备份 + secret.key），`./logs:/app/logs`。
- 每日 03:00 自动热备份到 `data/backups/account_*.db`，默认保留 30 天（管理界面可改 `backup_keep_days`，也可手动「立即热备份」）。
- 热备份使用 SQLite 官方 `backup()` API，绝不直接拷贝运行中的 `.db` 文件。
- 恢复：停容器后把备份文件替换为 `data/account.db`（WAL 模式请同时清理 `-wal/-shm`），再启动。

## 常见运维

```bash
docker compose restart paas     # 重启
docker compose logs -f --tail 200 paas
docker compose build --pull && docker compose up -d   # 升级
```

数据库使用 `PRAGMA user_version` 预留迁移位，未来结构变更无需清库。
