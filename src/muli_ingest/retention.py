"""Conservative retention: reviewed plans, reversible trash, immutable evidence archive.

Permanent purge is intentionally unavailable in this development release.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

from .safeio import Root, SafetyError, atomic_bytes, atomic_json, digest_file, move_directory
from .sources import scan_source, source_record

ACTIVE = {"QUEUED", "PREPARING", "COPYING", "VERIFYING", "FINALIZING", "RECOVERING"}


def now():
    return datetime.now(UTC)


class Retention:
    def __init__(self, engine):
        self.e = engine

    def reasons(self, b, clock=None):
        clock = clock or now()
        life = b["lifecycle"]
        reasons = []
        cfg = self.e.settings()["values"]
        if not cfg["cleanup_enabled"] or cfg["cleanup_paused"]:
            reasons.append("全局清理已关闭或暂停")
        policy = b.get("retention_policy_at_completion", {})
        if b["state"] != "COMPLETED":
            reasons.append("批次未全部核验完成")
        if life["storage_state"] != "ACTIVE":
            reasons.append("批次不在中转区")
        if life["hold"]:
            reasons.append("已设置保护锁")
        expiry = life.get("expires_at")
        if not expiry:
            reasons.append("永久保留或未开始计时")
        elif datetime.fromisoformat(expiry) > clock:
            reasons.append("尚未到期")
        if not policy.get("enabled") or policy.get("expiry_action") != "trash":
            reasons.append("批次策略仅提醒或不清理")
        if policy.get("require_handoff_confirmation", True) and not life["handoff_confirmed"]:
            reasons.append("尚未确认全部素材已经另行保存")
        for other in self.e.batches():
            if other["batch_uid"] == b["batch_uid"]:
                continue
            if other["state"] in ACTIVE:
                reasons.append("存在活动复制任务")
                break
        for other in self.e.batches():
            if other["batch_uid"] == b["batch_uid"] or other["lifecycle"]["storage_state"] == "PURGED":
                continue
            references = {
                f["existing_copy"]["batch_uid"]
                for f in self.e.store.files(other["batch_uid"])
                if f.get("existing_copy")
            }
            references.update(other.get("borrowed_batches", []))
            if b["batch_uid"] in references:
                reasons.append("仍被批次 " + other["batch_id"] + " 引用")
        return list(dict.fromkeys(reasons))

    def overview(self):
        with self.e.lock:
            batches = self.e.batches()
            eligible = trash = 0
            warning_at = now() + timedelta(days=self.e.settings()["values"]["notification_days"])
            for b in batches:
                b["lifecycle"]["blocked_reasons"] = self.reasons(b)
                expiry = b["lifecycle"].get("expires_at")
                b["lifecycle"]["expiring_soon"] = bool(
                    expiry and datetime.fromisoformat(expiry) <= warning_at
                )
                size = b["summary"].get("copied_bytes", 0)
                if not b["lifecycle"]["blocked_reasons"]:
                    eligible += size
                if b["lifecycle"]["storage_state"] == "TRASHED":
                    trash += size
            return {
                "batches": batches,
                "eligible_bytes": eligible,
                "trash_bytes": trash,
                "cleanup_enabled": self.e.allow_cleanup,
                "purge_supported": False,
            }

    def update(self, uid, values):
        with self.e.lock:
            if set(values) - {"retention_days", "hold"}:
                raise SafetyError("unknown_retention_setting")
            b = self.e.batch(uid)
            life = b["lifecycle"]
            if life["storage_state"] != "ACTIVE":
                raise SafetyError("batch_not_active")
            if "hold" in values and values["hold"] is not None:
                if type(values["hold"]) is not bool:
                    raise SafetyError("invalid_hold")
                life["hold"] = values["hold"]
            if "retention_days" in values:
                days = values["retention_days"]
                if days is not None and (type(days) is not int or not 1 <= days <= 3650):
                    raise SafetyError("invalid_retention_days")
                if not b.get("completed_at"):
                    raise SafetyError("batch_not_completed")
                proposed = (
                    (datetime.fromisoformat(b["completed_at"]) + timedelta(days=days)).isoformat()
                    if days is not None
                    else None
                )
                # Existing batches may be extended, never silently brought nearer to deletion.
                old = life.get("expires_at")
                if proposed and (not old or datetime.fromisoformat(proposed) < datetime.fromisoformat(old)):
                    raise SafetyError(
                        "retention_shortening_requires_future_review", "此版本只允许延长或永久保留"
                    )
                life["expires_at"] = proposed
            self.e._save(b)
            self.e.store.event(
                uid, {"type": "retention_updated", "time": now().isoformat(), "values": values}
            )
            return life

    def handoff(self, uid, confirmed):
        if confirmed is not True:
            raise SafetyError("confirmation_required")
        with self.e.lock:
            b = self.e.batch(uid)
            if b["state"] != "COMPLETED":
                raise SafetyError("batch_not_completed")
            b["lifecycle"].update(
                handoff_confirmed=True, handoff_evidence="manual_confirmation", handoff_at=now().isoformat()
            )
            self.e._save(b)
            self.e.store.event(uid, {"type": "handoff_manually_confirmed", "time": now().isoformat()})
            return b["lifecycle"]

    def preview(self):
        with self.e.lock:
            view = self.overview()
            eligible = []
            blocked = []
            for b in view["batches"]:
                if b["lifecycle"]["blocked_reasons"]:
                    blocked.append({"batch_id": b["batch_id"], "reasons": b["lifecycle"]["blocked_reasons"]})
                else:
                    eligible.append(
                        {
                            "batch_uid": b["batch_uid"],
                            "batch_id": b["batch_id"],
                            "bytes": b["summary"].get("copied_bytes", 0),
                        }
                    )
            plan = {
                "plan_id": uuid.uuid4().hex,
                "action": "trash",
                "eligible": eligible,
                "blocked": blocked,
                "total_bytes": sum(x["bytes"] for x in eligible),
                "expires_at": (now() + timedelta(minutes=5)).isoformat(),
                "cleanup_enabled": self.e.allow_cleanup,
                "used": False,
            }
            self.e.store.put("cleanup_plan", plan["plan_id"], plan)
            return plan

    def _verify(self, b, root):
        """Refuse cleanup/restore when unknown entries or corrupt bytes are present."""
        with Root(root):
            pass
        owner = json.loads((root / ".ingest/owner.json").read_text())
        if owner != {"batch_uid": b["batch_uid"], "batch_id": b["batch_id"]}:
            raise SafetyError("owner_mismatch")
        receipt = json.loads((root / "ingest_complete.json").read_text())
        if receipt["batch_uid"] != b["batch_uid"]:
            raise SafetyError("receipt_mismatch")
        for name, key in [("ingest_manifest.md", "md_blake3"), ("ingest_manifest.json", "json_blake3")]:
            if receipt.get(key) and digest_file(root / name) != receipt[key]:
                raise SafetyError("report_hash_mismatch")
        data = root / "SOURCE_DATA"
        scanned = scan_source(source_record(data), ["."], False)
        files = [
            f
            for f in self.e.store.files(b["batch_uid"])
            if f["copy_status"] in {"verified", "size_verified"}
        ]
        if not scanned["complete"] or {f["relative_path"] for f in scanned["files"]} != {
            f["relative_path"] for f in files
        }:
            raise SafetyError("data_inventory_changed")
        expected = self.e.store.get("scan", b["scope"]["scan_id"])["directories"]
        if set(scanned["directories"]) != set(expected):
            raise SafetyError("directory_inventory_changed")
        for f in files:
            target = data / f["relative_path"]
            if target.stat().st_size != f["size_bytes"]:
                raise SafetyError("data_size_mismatch")
            if f["copy_status"] == "verified" and digest_file(target) != f["source_hash"]:
                raise SafetyError("data_hash_mismatch")
        return receipt

    def execute(self, plan_id, confirmed):
        if confirmed is not True:
            raise SafetyError("confirmation_required")
        if not self.e.allow_cleanup:
            raise SafetyError("cleanup_runtime_disabled", "本地清理执行尚未启用；可查看预览")
        with self.e.lock:
            plan = self.e.store.get("cleanup_plan", plan_id)
            if not plan or plan["used"] or datetime.fromisoformat(plan["expires_at"]) < now():
                raise SafetyError("stale_cleanup_plan")
            batches = [self.e.batch(x["batch_uid"]) for x in plan["eligible"]]
            if any(self.reasons(b) for b in batches):
                raise SafetyError("cleanup_conditions_changed")
            # Claim the exact reviewed list before doing any filesystem mutation.
            plan["used"] = True
            self.e.store.put("cleanup_plan", plan_id, plan)
            trash = self.e.staging / ".ingest-trash"
            trash.mkdir(exist_ok=True, mode=0o700)
            with Root(trash):
                pass
            results = []
            for b in batches:
                try:
                    src = self.e.staging / b["batch_id"]
                    dst = trash / b["batch_uid"]
                    self._verify(b, src)
                    archive = self.e.archive / b["batch_uid"]
                    archive.mkdir(exist_ok=True, mode=0o700)
                    with Root(archive):
                        pass
                    for name in [
                        "ingest_manifest.md",
                        "ingest_manifest.json",
                        "ingest_complete.json",
                        "ingest.log",
                    ]:
                        if not (src / name).exists():
                            continue
                        with Root(src) as r, r.file(name) as fd:
                            import os

                            with os.fdopen(os.dup(fd), "rb") as stream:
                                payload = stream.read()
                        target = archive / name
                        if target.exists():
                            if target.read_bytes() != payload:
                                raise SafetyError("archive_conflict")
                        else:
                            atomic_bytes(target, payload)
                    b["lifecycle"].update(storage_state="CLEANUP_PENDING", cleanup_plan_id=plan_id)
                    self.e._save(b)
                    move_directory(src, dst)
                    b["lifecycle"].update(
                        storage_state="TRASHED", trashed_at=now().isoformat(), purge_supported=False
                    )
                    atomic_json(archive / "lifecycle.json", b["lifecycle"])
                    self.e._save(b)
                    results.append({"batch_uid": b["batch_uid"], "state": "TRASHED"})
                # Each batch needs a durable failure state even for unexpected I/O errors.
                except Exception as exc:  # noqa: BLE001
                    b["lifecycle"]["storage_state"] = "CLEANUP_FAILED"
                    self.e._save(b)
                    results.append(
                        {"batch_uid": b["batch_uid"], "state": "CLEANUP_FAILED", "error": str(exc)}
                    )
            return {"results": results}

    def restore(self, uid):
        if not self.e.allow_cleanup:
            raise SafetyError("cleanup_runtime_disabled")
        with self.e.lock:
            b = self.e.batch(uid)
            if b["lifecycle"]["storage_state"] != "TRASHED":
                raise SafetyError("batch_not_trashed")
            for f in self.e.store.files(uid):
                ref = f.get("existing_copy")
                if ref and self.e.batch(ref["batch_uid"])["lifecycle"]["storage_state"] != "ACTIVE":
                    raise SafetyError("restore_dependencies_first", "请先还原引用的历史批次")
            src = self.e.staging / ".ingest-trash" / uid
            self._verify(b, src)
            b["lifecycle"]["storage_state"] = "RESTORE_PENDING"
            self.e._save(b)
            try:
                move_directory(src, self.e.staging / b["batch_id"])
            except Exception:
                b["lifecycle"]["storage_state"] = "CLEANUP_FAILED"
                self.e._save(b)
                raise
            b["lifecycle"].update(storage_state="ACTIVE", hold=True, restored_at=now().isoformat())
            self.e._save(b)
            atomic_json(self.e.archive / uid / "lifecycle.json", b["lifecycle"])
            return b["lifecycle"]
