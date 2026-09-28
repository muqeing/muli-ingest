# Web / API 合同

API 前缀为 `/api/v1`。错误响应使用 `{"detail": "..."}`。写请求必须使用 JSON，并带 `X-Ingest-Request: 1`；浏览器请求受 Host、Origin 和会话检查保护。

## 会话

- `GET /health`：无需登录的最小健康检查。
- `POST /session`：局域网模式登录，正文 `{"access_code": "..."}`。
- `DELETE /session`：退出并使当前会话失效。

局域网模式除健康检查和登录外均要求有效会话。本机回环模式不要求登录。

## 状态与来源

- `GET /status`：服务版本、运行模式、中转路径、工具状态、能力和设置版本。
- `GET /sources`：当前明确配置及自动发现的来源。
- `POST /sources/{source_id}/scans`：扫描并冻结范围，正文 `{"selected_roots": ["."]}`。

来源字段包含应用内介质身份、当前挂载会话、标签、连接状态和可用的卷 UUID。扫描或刷新来源不会开始复制。

## 批次

- `POST /batches`：手动创建复制批次，要求 `Idempotency-Key`。
- `GET /batches`、`GET /batches/{batch_uid}`：列表和详情。
- `GET /batches/{batch_uid}/files`：文件级状态。
- `POST /batches/{batch_uid}/interrupt`：请求安全中断。
- `POST /batches/{batch_uid}/resume`：手动恢复。
- `GET /batches/{batch_uid}/manifest?format=md|json`：下载清单。

批次进度包含阶段、当前路径、已处理字节、总字节、瞬时/平滑速度和预计剩余时间。批次回执记录精准或快速核验方式，不把两者表述为同等证据。

## 设置

- `GET /settings`：设置值、版本及字段 schema。
- `PATCH /settings`：以 `expected_version` 更新变更字段。

当前主要可编辑项包括默认留存时间、核验方式、并行来源数、增量复用、重试、带宽、Hash 线程预算、元数据级别和 JSON 报告。并行来源数范围为 1–4，默认 2；同一来源不能同时运行两个批次。

## 生命周期

- `PATCH /batches/{batch_uid}/retention`：延期、永久保留或保护锁。
- `POST /batches/{batch_uid}/handoffs`：人工确认已另行保存。
- `GET /retention/overview`：留存总览。
- `POST /cleanup/previews`：生成清理预览。
- `POST /cleanup/runs`：对明确的预览执行可逆回收，要求人工确认且服务器启用了清理能力。
- `POST /batches/{batch_uid}/restore`：从回收状态恢复。

0.1.0 的默认启动入口关闭真实清理执行；自动永久删除未实现。

## 界面约束

- 页面不可编造任务、速度或核验结果。
- 页面刷新和浏览器断开不改变后台任务。
- 明确区分复制完成、所选核验通过、已另行备份和允许格式化来源介质。
- 只通过同源静态资源运行，不依赖外部 CDN。
