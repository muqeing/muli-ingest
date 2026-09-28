# NAS / FNOS 部署指南

木梨 Ingest 以 Docker Compose 方式运行，当前不是 FNOS 应用中心的原生安装包。以下步骤适用于支持 Docker Engine 与 Compose v2 的 Linux NAS；不同 NAS 的 USB 自动挂载根目录和权限可能不同。

## 1. 准备目录

在 NAS 上分别创建状态、中转、来源和私密配置目录。状态和中转目录需要容器用户可写，来源目录由宿主机挂载并只读传给容器。

```sh
mkdir -p /path/to/muli-ingest/{state,staging,source-01,source-02,private}
cp example.env .env
```

`MULI_INGEST_STAGING` 应指向专用的拍摄项目素材中转目录。不要复用存有其他文件的旧临时目录。更换位置前停止容器，并确认没有复制或核验任务正在运行。

## 2. 创建固定登录密码

```sh
PRIVATE_DIR=/path/to/muli-ingest/private python3 - <<'PY'
import os
from pathlib import Path

path = Path(os.environ["PRIVATE_DIR"]) / "access-code"
if path.exists():
    raise SystemExit(f"refusing to replace existing file: {path}")
value = input("设置登录密码: ")
if not value or value != value.strip() or "\n" in value or "\r" in value:
    raise SystemExit("password must be non-empty and have no surrounding whitespace")
path.write_text(value, encoding="utf-8")
path.chmod(0o600)
print(path)
PY
```

密码文件保存在宿主机，容器只读加载。重新部署或重启不会改变密码；主动更换文件并重启后，旧会话会失效。

## 3. 配置来源

编辑 `.env`：

- `MULI_INGEST_SOURCE`、`MULI_INGEST_SOURCE_02`：两个明确的相机卷目录。
- `MULI_INGEST_SOURCE_01_UUID`、`MULI_INGEST_SOURCE_02_UUID`：宿主机报告的卷 UUID，用于界面卡 ID。
- `MULI_INGEST_EXTERNAL_ROOT`：Linux 可移动卷的共同挂载根目录，用于自动发现。
- `MULI_INGEST_STATE`：SQLite、事件和报告归档。
- `MULI_INGEST_STAGING`：每批素材、清单和回执。
- `MULI_INGEST_ACCESS_CODE_FILE`：上一步创建的密码文件。

若当前只有一个来源，可为第二个来源准备一个空目录和唯一的占位 UUID。自动发现只显示具有 `DCIM`、`PRIVATE` 或 `M4ROOT` 等相机目录特征的卷；仍应在首次复制前核对界面路径、卷名和卡 ID。

`compose.lan.yaml` 以只读 `rslave` 方式映射外接卷根目录，并只读读取 `/run/udev/data`。如果 NAS 不提供这些路径，删除对应两个挂载后仍可使用明确配置的来源，但不能在容器中自动发现后续插入的卷。

## 4. 启动

```sh
docker compose -f compose.lan.yaml config --quiet
docker compose -f compose.lan.yaml up -d --build
docker compose -f compose.lan.yaml ps
```

同一可信局域网访问 `http://NAS-IP:18765/`。首次使用时先刷新来源、核对卡 ID、执行小批量复制，并检查来源文件数、目标文件数、总字节数和核验结果。

## 5. 停止与升级

```sh
docker compose -f compose.lan.yaml down
```

停止容器不会删除宿主机目录。升级前备份状态数据库和报告，并保留中转素材；不要使用会删除数据卷的命令。更新代码后重新构建镜像，先用合成或可重复的小批量素材验收，再恢复正式工作。

## 网络边界

局域网直连使用 HTTP，只适合可信内网。外网、访客网络或跨网段使用时，应由 NAS 反向代理提供 HTTPS，并给应用启动参数增加 `--secure-cookie`。不要在路由器中直接开放服务端口。
