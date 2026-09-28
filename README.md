# 木梨 Ingest

木梨 Ingest 是一套面向摄影与视频工作流的 NAS 素材安全中转服务。相机、存储卡或移动介质由宿主机只读挂载，服务扫描素材、执行受控复制、记录进度，并生成可回读的核验结果。

> 当前版本为 **0.1.0 Alpha**。它已经可以运行和完成素材中转，但仍需要操作者保留源卡，并在另一份独立备份完成前避免格式化介质。

## 功能

- 同时接入多个来源，默认最多并行处理 2 个来源，可在设置中选择 1–4 个。
- 来源只读挂载；刷新或扫描不会自动开始复制。
- 两种核验方式：
  - **精准核验**：连续复制整批素材，同时计算来源 BLAKE3；复制完成后集中回读 NAS 副本并逐文件比对。
  - **快速核验**：核对文件数量与大小，速度更高，证据等级较低。
- 显示实时速度、进度、当前文件和预计剩余时间。
- 保留未知文件、原目录结构和空目录，不覆盖已存在的已发布文件。
- 支持中断恢复、失败重试和已核验文件的增量复用。
- 每批生成 Markdown/JSON 清单、核验结果和最终回执。
- 默认保留 14 天，支持按批次延期、永久保留和保护锁。
- 提供本机回环模式及带固定登录密码的 NAS 局域网模式。
- 可在 Linux/FNOS 的 Docker 环境中发现符合相机目录特征的新挂载卷。

## Docker 快速开始

需要 Docker Engine、Compose v2，以及两个已由宿主机挂载的来源目录。先复制示例配置：

```sh
cp example.env .env
mkdir -p runtime/state runtime/staging runtime/source-01 runtime/source-02 runtime/private
python3 - <<'PY'
import secrets
from pathlib import Path

path = Path("runtime/private/access-code")
path.write_text(secrets.token_urlsafe(32), encoding="utf-8")
path.chmod(0o600)
print(f"访问口令已保存到 {path}")
PY
```

编辑 `.env`，把状态、中转、来源和口令文件改成宿主机的绝对路径。来源目录必须只作为读取来源使用：

```sh
docker compose -f compose.lan.yaml config --quiet
docker compose -f compose.lan.yaml up -d --build
```

同一局域网打开 `http://NAS-IP:18765/`。不要把 18765 端口直接映射到公网；远程访问应先配置 HTTPS 反向代理，并启用 `--secure-cookie`。

FNOS/Linux 的挂载、权限和自动发现说明见 [NAS 部署指南](docs/nas-deployment.md)。只在本机验证时使用 [本地 Docker 指南](docs/docker-local.md)。

## 本地开发

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)：

```sh
uv sync --all-extras
uv run pytest
uv run ruff check .
uv run python -m build
```

启动隔离演示服务：

```sh
uv run muli-ingest --demo --state ./runtime/demo-state --staging ./runtime/demo-staging
```

打开 `http://127.0.0.1:8765/`。演示模式只创建少量合成文本文件，不代表真实相机、NAS、断电或大容量素材验收。

## 安全边界

- 来源卷必须由宿主机只读挂载；应用不会格式化或删除来源介质。
- 中转副本不是备份。完成另一份独立存储并人工确认后，才应释放源卡。
- 自动到期永久删除尚未开放；到期管理保留人工确认门。
- 局域网模式使用固定口令文件和短期 HttpOnly 会话。口令、Cookie、状态数据库和真实素材都不应提交到仓库。
- 发现安全问题请按 [安全策略](SECURITY.md) 私下报告。

更多当前边界见 [发布状态](docs/release-status.md)，接口说明见 [API 合同](docs/frontend-contract.md)。

## 参与贡献

提交问题或代码前请阅读 [贡献指南](CONTRIBUTING.md)。本项目采用 [Apache License 2.0](LICENSE)。
