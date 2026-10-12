# `muli-ingest-client`

## 安装与随版本更新

需要 Python 3.12 及以上。CLI 和服务同仓库、同软件包、同版本发布，现有服务端 API 可直接使用：

```sh
uv tool install --from git+https://github.com/muqeing/muli-ingest.git muli-ingest
uv tool upgrade muli-ingest
muli-ingest-client --version
muli-ingest-client schema
```

固定 Git tag 或 commit 可锁定版本。每次 GitHub push 都运行客户端和完整回归检查，构建
安装包，并验证实际安装后的命令入口与包版本一致；产物可在 GitHub Actions 中下载。
客户端升级与 NAS 镜像部署分别进行；发布源码不会自动重启 NAS。原 `muli-ingest` 服务
启动命令保留，新增客户端无需启动另一份服务。

`muli-ingest-client` 是面向脚本和 AI 调用的本地 HTTP API 客户端。默认连接
`http://127.0.0.1:8765`，也可以设置 `MULI_INGEST_URL` 或传入 `--url`。URL 只能是
没有路径、查询、片段或 userinfo 的 `http(s)` origin；客户端禁用代理并拒绝重定向。

每次调用只输出一个 JSON 信封：

```json
{"schema_version":1,"ok":true,"service":"muli-ingest","command":"health","cli_version":"0.1.1","data":{"status":"ok"}}
```

`--version` 输出本地客户端版本；`version --remote` 通过现有 `status` API 回读服务端版本：

```sh
muli-ingest-client --version
muli-ingest-client version --remote
```

读命令包括 `health`、`status`、`session`、`sources`、`batches`、`batch`、`files` 和
`manifest`。例如：

```sh
muli-ingest-client health
muli-ingest-client --url http://127.0.0.1:8765 batches
muli-ingest-client batch BATCH_20261013_000001
muli-ingest-client manifest BATCH_20261013_000001 --format json
```

`scan`、`start`、`interrupt` 和 `resume` 默认是 dry-run，返回拟发送的 HTTP method、path
和 JSON body。确认边界后再加 `--execute`。`scan` 的根目录可重复指定：

```sh
muli-ingest-client scan --source-id SOURCE_ID --root . --root DCIM --execute
```

`start` 必须提供幂等键和只含 `source_id`、`scan_id`、`scan_revision` 的 JSON 文件（用 `-`
表示 stdin）：

```sh
muli-ingest-client start --idempotency-key request-20261013-01 --request batch.json --execute
```

当服务启用局域网会话时，使用 `--access-code-env NAME` 指定环境变量名。客户端只在服务
返回需要登录且尚未认证时 POST 一次会话请求；口令仅存在于内存中，不会写入 JSON、日志或
Cookie 输出。不要把真实口令写入命令行或文件。

`wait` 只轮询 GET，不会重试写入：

```sh
muli-ingest-client wait BATCH_ID --deadline 300 --interval 1
```

它只根据当前服务返回的 `state` 判断 `COMPLETED`、`FAILED`、`INTERRUPTED` 或
`CANCELLED`；`COMPLETED` 返回 0，失败或中断返回 4，截止时间返回 5，未知状态返回 4。
轮询间隔必须大于 0，单次 GET 超时不会超过剩余 deadline。完成状态仅表示服务端当前批次
状态，不能单凭它判断独立内容核验完成或来源卡可以格式化；需要回读相应校验和备份证据。

POST 的网络超时、响应读取失败、非法响应和 HTTP 5xx 会返回 `result_unknown=true`、
`automatic_retry=false`，同时带上 method/path；`start` 还会带幂等键。客户端不会自动重试
任何写请求。

退出码为：0 成功，2 参数/输入错误，3 网络或 HTTP 错误，4 操作/协议失败，5 等待超时。
