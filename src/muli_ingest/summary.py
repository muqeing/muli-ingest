"""Transient per-run totals; durable file rows remain the authority.

Only the worker owning a batch uses this accumulator. Restart and terminal
paths rebuild from the file table, so no cached counter becomes a receipt.
"""


def summarize(files, scan):
    verified = [f for f in files if f["copy_status"] == "verified"]
    size_verified = [f for f in files if f["copy_status"] == "size_verified"]
    staged = [f for f in files if f["copy_status"] == "copied_unverified"]
    skipped = [f for f in files if f["copy_status"] == "skipped_existing"]
    failed = [f for f in files if f["copy_status"] == "failed"]
    copied = verified + size_verified + staged
    return {
        "selected_file_count": len(files),
        "selected_bytes": scan["total_bytes"],
        "source_file_count": len(files) if scan["selected_roots"] == ["."] else None,
        "source_observed_file_count": len(files),
        "source_scan_complete": scan["selected_roots"] == ["."],
        "selection_scan_complete": scan["complete"],
        "copy_required_count": len(files) - len(skipped),
        "copied_file_count": len(copied),
        "verified_file_count": len(verified),
        "size_verified_file_count": len(size_verified),
        "awaiting_hash_file_count": len(staged),
        "previously_ingested_count": len(skipped),
        "failed_file_count": len(failed),
        "pending_file_count": len(files) - len(copied) - len(skipped) - len(failed),
        "copied_bytes": sum(f["size_bytes"] for f in copied),
        "previously_ingested_bytes": sum(f["size_bytes"] for f in skipped),
        "photo_count": sum(f["media_type"] == "photo" for f in files),
        "video_count": sum(f["media_type"] == "video" for f in files),
        "other_count": sum(f["media_type"] == "other" for f in files),
        "selected_directory_count": len(scan["directories"]),
        "excluded_entry_count": len(scan["excluded"]),
    }

STATUSES = {
    "verified": "verified_file_count", "size_verified": "size_verified_file_count",
    "copied_unverified": "awaiting_hash_file_count", "skipped_existing": "previously_ingested_count",
    "failed": "failed_file_count",
}
COPIED = frozenset(("verified", "size_verified", "copied_unverified"))


def contribution(file):
    status, size = file["copy_status"], file["size_bytes"]
    result = {name: int(status == key) for key, name in STATUSES.items()}
    copied, skipped = status in COPIED, status == "skipped_existing"
    result.update(copied_file_count=int(copied), copied_bytes=size if copied else 0,
                  previously_ingested_bytes=size if skipped else 0,
                  copy_required_count=int(not skipped),
                  pending_file_count=int(not copied and not skipped and status != "failed"))
    return result


class SummaryAccumulator:
    def __init__(self, files, scan):
        self.value = summarize(files, scan)

    def replace(self, before, file):
        after = contribution(file)
        for key in after:
            self.value[key] += after[key] - before[key]
        return dict(self.value)
