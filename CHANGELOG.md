## 0.2.0 — 2026-10-18

- Accept explicit permanent completion facts transactionally without granting current source-cleanup permission.
- Poll only unresolved markers and use an indexed due-lease query; release consumed marker hints. Keep legacy exchange the default and never auto-import old leases as facts.
- Add read-only completion status and matching source/client versions. Preserve the independently tested desktop client settings read command and optional settings-version guard, including a server-capability check before a guarded start. NAS exact role-file readback and public health passed; 105 server-role isolated checks and 115 maintained-source checks passed without skips. Authenticated feedback-worker status and a real newly completed archive remain separate acceptance gaps.

# 更新记录

本项目遵循语义化版本。日期使用 `YYYY-MM-DD`。

## 未发布

- 增加带版本化 JSON 信封的 `muli-ingest-client` 本地 API 客户端；远程写操作默认 dry-run，并提供有限等待与回读状态。
- CLI 发布检查使用 Dockerfile 声明的 rclone 版本，与正式服务一致；不使用发行版仓库中的旧测试版本。
- 来源页面区分当前连接与默认收起的历史设备，并按最近时间倒序显示。
- 来源设备中途断开时保留批次；同一卷 UUID 重新连接后可继续，已完成文件不会重复复制。
- 中断时正在写入的文件会丢弃未完成临时副本并从头安全重拷，完成前重新执行来源终检。

## 0.1.0 - 2026-09-28

- 提供本机与 NAS Docker 运行方式。
- 支持多来源扫描和最多四个来源任务并行。
- 提供精准 BLAKE3 核验及文件数量/大小快速核验。
- 提供实时速度、进度、剩余时间、中断恢复和增量复用。
- 提供 14 天默认留存、延期、保护锁、转存确认和清理预览。
- 提供局域网固定口令登录、会话保护和登录限速。
- 提供 Linux/FNOS 相机卷自动发现入口。
## 0.2.1 — 2026-10-18

- Add opt-in `MULI_METADATA_FAIRNESS=1`: bounded regular/short coordinators share one fair media-I/O turn, yielding between existing readers and 32-file metadata groups. At most four short turns precede a waiting regular batch.
- Choose the short lane with a bounded indexed key count; queued work retains identifiers rather than file lists. Keep copy priority, source release, destination checks and final receipt requirements.
- Route synchronous metadata through the same coordinators, release the engine lock before waiting on resume, and reject recursive synchronous waits. Add cancellation, ownership, backlog, small-before-large and destination-change regressions. Default behavior remains single-threaded; this change does not retry or reprocess historical batches.
