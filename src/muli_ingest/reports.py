import json
import uuid
from datetime import datetime, timedelta
from importlib.metadata import version
from pathlib import Path

from .safeio import atomic_bytes, atomic_json, digest_file, publish_file, sync_dir


def retention_snapshot(settings, completed_at, revision):
    return {
        "policy_id": "global",
        "policy_revision": revision,
        "enabled": settings["cleanup_enabled"],
        "keep_forever": settings["retention_days"] is None,
        "retention_days": settings["retention_days"],
        "anchor": "completed_at",
        "completed_at": completed_at,
        "expires_at": (
            datetime.fromisoformat(completed_at) + timedelta(days=settings["retention_days"])
        ).isoformat()
        if completed_at and settings["retention_days"] is not None
        else None,
        "expiry_action": settings["expiry_action"],
        "trash_retention_days": settings["trash_retention_days"],
        "trash_auto_purge": settings["trash_auto_purge"],
        "require_handoff_confirmation": settings["require_handoff_confirmation"],
    }


def build_manifest(batch, files, scan, now):
    records = []
    for f in files:
        st = f["signature"]
        records.append(
            {
                "file_id": f["file_id"],
                "filename": f["filename"],
                "relative_path": f["relative_path"],
                "extension": f["extension"],
                "media_type": f["media_type"],
                "size_bytes": f["size_bytes"],
                "filesystem": {
                    "mtime_ns_decimal": str(st[3]),
                    "birthtime": None,
                    "ctime_ns_decimal": str(st[4]),
                    "ctime_semantics": "metadata_change_time",
                },
                "copy_status": f["copy_status"],
                "destination_relative_path": f.get("destination_relative_path"),
                "existing_copy": f.get("existing_copy"),
                "hash": {
                    "algorithm": "blake3",
                    "source": f.get("source_hash"),
                    "destination": f.get("destination_hash"),
                    "existing_destination": f.get("existing_hash"),
                    "readback_verified_at": f.get("verified_at"),
                },
                "hash_match": f.get("hash_match"),
                "attempt_count": f.get("attempt_count", 0),
                "retry_count": max(0, f.get("attempt_count", 0) - 1),
                "resumed": f.get("resumed", False),
                "error": f.get("error"),
                "metadata": f.get("metadata"),
            }
        )
    dependencies = {}
    for f in records:
        if f["existing_copy"]:
            previous = f["existing_copy"]
            uid = previous["batch_uid"]
            dependencies.setdefault(
                uid, {"batch_uid": uid, "batch_id": previous["batch_id"], "referenced_file_count": 0}
            )["referenced_file_count"] += 1
    return {
        "schema_version": "1.1",
        "manifest_id": f"{batch['batch_uid']}-r{batch['revision']}",
        "revision": batch["revision"],
        "finalized": True,
        "example_data": batch["mode"] == "demo",
        "generated_at": now,
        "generator": {
            "name": "木梨 Ingest",
            "version": "0.1.0",
            "tools": {"blake3": version("blake3")},
            "verification": (
                "independent-source-and-destination-readback"
                if batch["settings"].get("verification_mode", "exact") == "exact"
                else "file-count-and-size-only"
            ),
        },
        "batch": {
            k: batch[k]
            for k in [
                "batch_id",
                "batch_uid",
                "state",
                "result",
                "source_id",
                "source_session_id",
                "created_at",
                "completed_at",
                "copy_mode",
                "scope",
                "settings",
            ]
        },
        "source": batch["source"],
        "summary": batch["summary"],
        "source_coverage": batch.get("source_coverage", {"status": "UNKNOWN"}),
        "files": records,
        "directories": scan["directories"],
        "exclusions": scan["excluded"],
        "scan_errors": scan["errors"],
        "warnings": batch.get("warnings", []),
        "dependencies": list(dependencies.values()),
        "retention_policy_at_completion": batch["retention_policy_at_completion"],
    }


def write_report(root, batch, files, scan, now):
    root = Path(root)
    obj = build_manifest(batch, files, scan, now)
    data = (json.dumps(obj, ensure_ascii=False, indent=2).replace("`", "\\u0060") + "\n").encode()
    md = "# 木梨 Ingest Manifest\n\n"
    if obj["example_data"]:
        md += "合成演示区生成；不是客户生产素材回执。\n\n"
    markdown = md.encode() + b"```json\n" + data + b"```\n"
    revision = root / ".ingest/revisions" / f"r{batch['revision']:06}"
    revision.mkdir(parents=True, exist_ok=False)
    atomic_bytes(revision / "ingest_manifest.md", markdown)
    atomic_bytes(root / "ingest_manifest.md", markdown)
    if batch["settings"]["json_enabled"]:
        atomic_bytes(revision / "ingest_manifest.json", data)
        atomic_bytes(root / "ingest_manifest.json", data)
    sync_dir(revision.parent)
    if batch["state"] == "COMPLETED":
        receipt = {
            "batch_uid": batch["batch_uid"],
            "manifest_id": obj["manifest_id"],
            "revision": batch["revision"],
            "example_data": obj["example_data"],
            "algorithm": "blake3",
            "copy_verification": obj["generator"]["verification"],
            "md_blake3": digest_file(root / "ingest_manifest.md"),
            "json_blake3": digest_file(root / "ingest_manifest.json")
            if batch["settings"]["json_enabled"]
            else None,
        }
        temp = root / ".ingest" / ("receipt-" + uuid.uuid4().hex)
        atomic_json(temp, receipt)
        publish_file(temp, root / "ingest_complete.json")
    return obj
