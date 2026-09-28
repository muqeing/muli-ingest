# Docker 本地验证包

这份 Compose 只用于在本机 Docker 中验证木梨 Ingest 的容器合同。它没有 NAS/fnOS 部署配置，不开放局域网，也不启用真实清理。浏览器入口只通过宿主 `127.0.0.1` 发布；容器内部虽然监听 `0.0.0.0`，CLI 仍要求 `--container-loopback` 和 `MULI_INGEST_CONTAINER_LOOPBACK=1` 两道门。

## 目录与权限

先准备三个明确的宿主目录，并让 `PUID` 对它们有相应权限。来源只读挂载，状态和中转目录需要可写：

```sh
mkdir -p "$PWD/docker-local/state" "$PWD/docker-local/staging" "$PWD/docker-local/source-01"
printf 'synthetic local Docker fixture\n' > "$PWD/docker-local/source-01/fixture.txt"
chmod u+rwx "$PWD/docker-local/state" "$PWD/docker-local/staging" "$PWD/docker-local/source-01"

export PUID="$(id -u)"
export PGID="$(id -g)"
export MULI_INGEST_STATE="$PWD/docker-local/state"
export MULI_INGEST_STAGING="$PWD/docker-local/staging"
export MULI_INGEST_SOURCE="$PWD/docker-local/source-01"
export MULI_INGEST_PORT=8765
```

Compose 不会替这些路径猜测宿主 `HOME` 或其他默认目录；三个路径变量缺失时配置会直接失败。来源目录不能在容器内写入。

## 构建与启动

需要 Docker Engine、Compose v2 和可用的镜像仓库访问。Apple Silicon 本机可按目标架构构建：

```sh
docker compose config
docker build --platform linux/arm64 -t muli-ingest:local .
docker compose up -d
docker compose ps
curl --fail http://127.0.0.1:8765/api/v1/status
```

打开 `http://127.0.0.1:8765/` 后，只做来源扫描和合成文件的手动验证；不要把这个过程当成真实客户素材或 NAS 验收。确认状态响应中的 `production_ready` 仍为 `false`，并检查 `tools` 中的 `rclone`、`exiftool`、`ffprobe` 路径。

Compose 启动时会把 `/state`、`/staging` 和 `/sources/source-01` 分别绑定到上面的宿主目录。容器不会自动发现宿主的 USB 设备；先由宿主系统挂载存储卡，再把卡的挂载目录填写到 `MULI_INGEST_SOURCE`。`SOURCE_DATA` 的发布与同文件系统回收依赖中转目录：`/staging` 必须是同一个可写挂载，不能再把它的子目录拆成其他挂载。回收站位于 `/staging/.ingest-trash`，仍占用磁盘。

## 停止与回滚

停止容器时保留状态和中转证据：

```sh
docker compose down
```

回到 Mac 版本时停止 Compose，然后使用 README 中的 `uv run muli-ingest ...` 命令，并继续指向同一份状态/中转目录。不要使用 `docker compose down -v`，也不要删除 `state`、`staging` 或来源目录；这些目录是可回读的回滚证据。重新启动前先检查 `docker compose config` 和路径权限。

## 已验证、未验证与上线门槛

本地交付包的自动合同测试检查了 Compose 的来源只读、状态/中转可写、宿主回环端口、非 root、只读根文件系统、`tmpfs /tmp`、能力收缩、健康检查、私有网络和无 Docker socket 等约束。若执行 Docker 构建，还应记录实际镜像架构、镜像大小以及容器内 `rclone --version`、`exiftool -ver`、`ffprobe -version` 输出。

本机容器流程仍未验证真实相机数据；每一种相机、Linux USB/挂载变化、断电恢复、大文件压力和 NAS/fnOS 权限都需要单独验收。进入 NAS 部署前必须确认目标架构、挂载权限、备份与回滚路径、浏览器入口范围及日志；本文件不构成生产就绪声明。
