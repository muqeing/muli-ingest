from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    retention_days: int | None = Field(14, ge=1, le=3650)
    cleanup_enabled: bool = True
    expiry_action: Literal["trash", "notify"] = "trash"
    trash_retention_days: int = Field(7, ge=1, le=90)
    trash_auto_purge: bool = True
    require_handoff_confirmation: bool = True
    auto_cleanup_hour: int = Field(3, ge=0, le=23)
    cleanup_paused: bool = False
    max_retries: int = Field(3, ge=0, le=10)
    retry_delay_seconds: float = Field(1, ge=0, le=300)
    incremental: bool = True
    exclude_os_junk: bool = True
    verification_mode: Literal["exact", "size_only"] = "exact"
    metadata_level: Literal["minimal", "standard"] = "standard"
    metadata_timeout_seconds: int = Field(30, ge=5, le=300)
    max_parallel_sources: int = Field(2, ge=1, le=4)
    bandwidth_mib: int = Field(0, ge=0, le=10000)
    json_enabled: bool = True
    hash_threads: int = Field(2, ge=1, le=8)
    notification_days: int = Field(3, ge=0, le=30)


LABELS = {
    "retention_days": ("中转与留存", "保留天数", "从整批核验完成计时；永久保留可不设天数"),
    "cleanup_enabled": ("中转与留存", "启用到期清理", "本地试用区有效；真实目录需部署时单独启用"),
    "expiry_action": ("中转与留存", "到期动作", "回收站仍占磁盘，只有永久清理才释放空间"),
    "trash_retention_days": ("中转与留存", "回收站保留天数", "从实际入站时间计时"),
    "trash_auto_purge": ("中转与留存", "到期自动清空回收站", "仅清理符合依赖、转存及保护条件的批次"),
    "require_handoff_confirmation": (
        "中转与留存",
        "清理前要求转存确认",
        "人工确认与Hash回执是不同证据；此版支持人工确认",
    ),
    "auto_cleanup_hour": ("中转与留存", "每日清理时刻（北京时间）", "小时，0–23；未启用真实清理时只做预览"),
    "cleanup_paused": ("中转与留存", "暂停全部自动清理", "不停止复制或改变单批次保护锁"),
    "incremental": ("来源与复制", "增量导入", "每次重新核验源和历史副本；关闭则完整再复制一份"),
    "exclude_os_junk": ("来源与复制", "过滤已知系统垃圾", "只过滤.DS_Store及根目录的三种系统垃圾目录"),
    "verification_mode": (
        "核验与失败处理",
        "核验方式",
        "精准核验会先连续复制整批，再集中执行BLAKE3回读；快速核验只检查文件数量和大小",
    ),
    "max_retries": ("失败处理", "额外重试次数", "不含首次尝试；断卡、冲突和空间不足不盲重试"),
    "retry_delay_seconds": ("失败处理", "重试间隔（秒）", "应用级预算；不与复制工具的重试相乘"),
    "metadata_timeout_seconds": ("失败处理", "元数据超时（秒）", "超时显示警告，字节核验独立判断"),
    "max_parallel_sources": (
        "性能与队列",
        "并行来源数量",
        "允许1–4个不同来源同时复制；默认2，系统压力过高时新批次会继续排队",
    ),
    "bandwidth_mib": ("性能与队列", "复制速度上限 MiB/s", "0代表不限；上限分别作用于每个活动来源"),
    "hash_threads": ("性能与队列", "Hash线程预算", "每个活动核验任务的线程预算；不代表磁盘速度"),
    "metadata_level": ("报告与元数据", "元数据提取", "最小模式仅保留文件与复制证据"),
    "json_enabled": ("报告与元数据", "附带JSON报告", "Markdown内始终保留完整JSON"),
    "notification_days": ("提醒与显示", "提前提醒天数", "在留存页面提示即将到期的批次，不外发"),
}

UNSUPPORTED_SETTINGS = {"trash_retention_days", "trash_auto_purge", "auto_cleanup_hour"}


def settings_schema():
    schema = Settings.model_json_schema()["properties"]
    result = []
    for key, (group, label, description) in LABELS.items():
        s = schema[key]
        kind = "select" if "enum" in s else s.get("type", "integer")
        if kind == "number":
            kind = "number"
        options = None
        if "enum" in s:
            labels = {
                "trash": "移入回收站",
                "notify": "只提醒",
                "minimal": "最小证据",
                "standard": "标准元数据",
                "exact": "精准核验（整批复制后集中 Hash）",
                "size_only": "快速核验（仅文件数与大小）",
            }
            options = [{"value": v, "label": labels.get(v, str(v))} for v in s["enum"]]
        bounds = next((x for x in s.get("anyOf", []) if x.get("type") == "integer"), s)
        if key in UNSUPPORTED_SETTINGS:
            description = "预留规格默认值；当前开发版尚不执行自动调度或永久删除，不能编辑。"
        result.append(
            {
                "key": key,
                "group": group,
                "label": label,
                "description": description,
                "type": kind,
                "default": s.get("default"),
                "min": bounds.get("minimum"),
                "max": bounds.get("maximum"),
                "options": options,
                "editable": key not in UNSUPPORTED_SETTINGS,
            }
        )
    return result
