"""Durable handoff from the source reader to NAS-only metadata processing.

This is a release receipt, not a content-verification or formatting receipt.
Only metadata may change after it has been published. Unknown publication
outcomes are preserved for review instead of restarting the source copy.
"""

import json
import os
import uuid

from blake3 import blake3

from .safeio import Root, SafetyError, digest_fd, publish_file, signature

NAME = ".ingest/source-release-v1.json"
FILE_KEYS = (
    "relative_path", "size_bytes", "signature", "file_id", "copy_status",
    "source_hash", "destination_hash", "destination_relative_path",
    "destination_signature", "existing_copy", "existing_hash", "hash_match",
)
BATCH_KEYS = (
    "batch_uid", "batch_id", "source_id", "source_session_id", "source",
    "scope", "copy_mode", "settings", "settings_version",
)


def digest(value):
    return blake3(json.dumps(value, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":")).encode()).hexdigest()


def binding(batch, scan, files):
    return digest({
        "batch": {key: batch.get(key) for key in BATCH_KEYS},
        "scan": scan,
        "files": [{key: row.get(key) for key in FILE_KEYS}
                  for row in sorted(files, key=lambda row: row["relative_path"])],
    })


def publish(staging, batch, scan, files, whole_selection, now):
    allowed = {"verified", "size_verified", "skipped_existing"}
    if (len(files) != len(scan["files"]) or
            any(row["copy_status"] not in allowed or not row.get("source_hash") or row.get("error")
                for row in files)):
        raise SafetyError("source_release_incomplete")
    root_path = staging / batch["batch_id"]
    with Root(root_path) as root:
        root_identity = list(root.identity)
    value = {
        "schema": "source-release-v1", "batch_uid": batch["batch_uid"],
        "batch_id": batch["batch_id"], "released_at": now,
        "binding": binding(batch, scan, files), "root_identity": root_identity,
        "whole_selection": bool(whole_selection), "file_count": len(files),
        "total_bytes": sum(row["size_bytes"] for row in files),
        "format_action_available": False,
    }
    temp = root_path / ".ingest" / ("release-" + uuid.uuid4().hex + ".tmp")
    with open(temp, "xb") as out:
        out.write((json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode())
        out.flush()
        os.fsync(out.fileno())
    publish_file(temp, root_path / NAME)
    return value


def validate(staging, batch, scan, files):
    expected = batch.get("source_release")
    if batch.get("source_released") is not True or not isinstance(expected, dict):
        raise SafetyError("source_release_missing")
    with Root(staging / batch["batch_id"]) as root:
        if list(root.identity) != expected.get("root_identity"):
            raise SafetyError("source_release_root_changed")
        with root.file(NAME) as fd:
            if os.fstat(fd).st_size > 16384:
                raise SafetyError("source_release_invalid")
            raw = os.read(fd, 16385)
    value = json.loads(raw)
    if (value != expected or value.get("schema") != "source-release-v1" or
            value.get("binding") != binding(batch, scan, files)):
        raise SafetyError("source_release_changed")
    return value


def check_targets(staging, batch, files, threads, checkpoint, proofs=None):
    """Selected destinations only; unchanged files need no content reread."""
    with Root(staging) as root:
        for index, row in enumerate(files):
            if index % 32 == 0:
                checkpoint()
            ref = row.get("existing_copy")
            path = (ref["staging_relative_path"] if ref else
                    batch["batch_id"] + "/" + row["destination_relative_path"])
            with root.file(path) as fd:
                current = os.fstat(fd)
                expected = (ref.get("source_release_signature") if ref else
                            row.get("destination_signature"))
                if current.st_size != row["size_bytes"] or current.st_nlink != 1:
                    raise SafetyError("released_target_changed", row["relative_path"])
                actual = signature(current)
                if expected and actual[:4] != expected[:4]:
                    raise SafetyError("released_target_changed", row["relative_path"])
                # A legacy/reused destination may lack a saved signature. A
                # ctime-only NAS attribute change needs content proof once.
                if not expected or actual != expected:
                    key = (path, tuple(actual), row["source_hash"])
                    if proofs is None or key not in proofs:
                        if digest_fd(fd, threads, checkpoint) != row["source_hash"]:
                            raise SafetyError("released_target_changed", row["relative_path"])
                        if proofs is not None:
                            if len(proofs) >= 8192:
                                proofs.pop(next(iter(proofs)))
                            proofs[key] = True
