import os
import stat
import uuid
from pathlib import Path

from blake3 import blake3

from .safeio import Root, SafetyError, parts, signature

PHOTOS = {
    ".arw",
    ".cr2",
    ".cr3",
    ".nef",
    ".raf",
    ".dng",
    ".rw2",
    ".orf",
    ".jpg",
    ".jpeg",
    ".heic",
    ".heif",
    ".hif",
    ".png",
    ".tif",
    ".tiff",
}
VIDEOS = {".mp4", ".mov", ".mxf", ".avi", ".mts", ".m2ts", ".braw", ".r3d", ".lrv", ".lrf"}
JUNK_DIRS = {".Trashes", ".Spotlight-V100", ".fseventsd"}


def _mount_path(value):
    for escaped, char in [("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")]:
        value = value.replace(escaped, char)
    return value


def _udev_properties(major_minor):
    properties = {}
    try:
        rows = (Path("/run/udev/data") / f"b{major_minor}").read_text(errors="replace").splitlines()
    except OSError:
        return properties
    for row in rows:
        if row.startswith("E:") and "=" in row:
            key, value = row[2:].split("=", 1)
            properties[key] = value
    return properties


def mounted_volume_identity(path):
    """Return whether path is an exact mount and its currently observed volume UUID.

    Docker bind mounts configured camera paths as exact mount targets. An unplugged
    camera can leave the host directory behind, so path existence alone is not a
    connection signal.
    """
    mountinfo = Path("/proc/self/mountinfo")
    if not mountinfo.exists():
        return False, None
    expected_path = str(Path(path).absolute())
    try:
        lines = mountinfo.read_text().splitlines()
    except OSError:
        return False, None
    for line in lines:
        fields = line.split()
        if len(fields) < 10 or _mount_path(fields[4]) != expected_path:
            continue
        major_minor = fields[2]
        properties = _udev_properties(major_minor)
        volume_uuid = properties.get("ID_FS_UUID")
        if not volume_uuid:
            sep = fields.index("-")
            device = Path(fields[sep + 2])
            by_uuid = Path("/dev/disk/by-uuid")
            if by_uuid.exists():
                for link in by_uuid.iterdir():
                    try:
                        if link.resolve() == device.resolve():
                            volume_uuid = link.name
                            break
                    except OSError:
                        continue
        return True, volume_uuid
    return False, None


def media_type(name):
    ext = Path(name).suffix.lower()
    return "photo" if ext in PHOTOS else "video" if ext in VIDEOS else "other"


def source_record(path, label=None, volume_uuid=None):
    path = Path(path).absolute()
    exact_mount, observed_uuid = mounted_volume_identity(path)
    if volume_uuid and exact_mount and observed_uuid != volume_uuid:
        raise SafetyError("source_identity_mismatch", "当前挂载卷与配置的设备身份不一致")
    with Root(path) as root:
        identity = list(root.identity)
        names = set(os.listdir(root.fd))
    identity_key = "uuid:" + volume_uuid if volume_uuid else str(path) + repr(identity)
    return {
        "source_id": blake3(identity_key.encode()).hexdigest()[:24],
        "session_id": uuid.uuid4().hex,
        "label": label or path.name,
        "path": str(path),
        "classification": "capture_media" if names & {"DCIM", "PRIVATE", "M4ROOT"} else "transfer_storage",
        "connected": True,
        "identity": identity,
        "identity_confidence": "high" if volume_uuid else "local_path",
        "volume_uuid": volume_uuid,
    }


def _mount_preference(item):
    """Prefer the stable dynamic-mount alias when one volume has aliases."""
    path = str(item.get("path") or "")
    parts = Path(path).parts
    return (0 if "external" in parts else 1, path)


def _deduplicate_mount_aliases(candidates):
    """Collapse aliases for one mounted root without merging cloned UUIDs.

    ``Root.identity`` is the local filesystem identity of the mount root.  It
    is stronger than a volume UUID for alias discovery: two paths with the
    same identity are aliases, while two roots with the same UUID may be
    cloned devices and must remain separate.  The selected path is stable
    regardless of mountinfo order, with ``/external`` preferred over the
    less reliable compatibility alias under ``/sources``.
    """
    groups = {}
    for item in candidates:
        key = tuple(item.get("identity") or ())
        groups.setdefault(key, []).append(item)

    selected = []
    for key in sorted(groups, key=lambda value: tuple(str(part) for part in value)):
        group = groups[key]
        winner = min(group, key=_mount_preference)
        selected.append(winner)

    by_uuid = {}
    for item in selected:
        volume_uuid = item.get("volume_uuid")
        if volume_uuid:
            by_uuid.setdefault(volume_uuid, set()).add(tuple(item.get("identity") or ()))
    for item in selected:
        volume_uuid = item.get("volume_uuid")
        if volume_uuid and len(by_uuid.get(volume_uuid, ())) > 1:
            # Preserve the old UUID-derived ID only for the unique-volume
            # case.  Cloned UUIDs are path-salted and low-confidence.
            item["source_id"] = blake3((item["source_id"] + item["path"]).encode()).hexdigest()[:24]
            item["identity_confidence"] = "low"
    return sorted(selected, key=lambda item: str(item.get("path") or ""))


def disconnected_source_record(path, label=None, volume_uuid=None, reason="source_unavailable"):
    path = Path(path).absolute()
    identity_key = "uuid:" + volume_uuid if volume_uuid else "path:" + str(path)
    return {
        "source_id": blake3(identity_key.encode()).hexdigest()[:24],
        "session_id": uuid.uuid4().hex,
        "label": label or path.name,
        "path": str(path),
        "classification": "capture_media",
        "connected": False,
        "identity": [],
        "identity_confidence": "high" if volume_uuid else "low",
        "volume_uuid": volume_uuid,
        "unavailable_reason": reason,
    }


def check_source(source):
    with Root(source["path"]) as root:
        if list(root.identity) != source["identity"]:
            raise SafetyError("source_changed", "来源挂载或目录身份已改变")


def scan_source(source, selected_roots=None, exclude_os_junk=True):
    roots = sorted(set(selected_roots or ["."]))
    if "." in roots:
        roots = ["."]
    else:
        for path in roots:
            parts(path)
        roots = [p for p in roots if not any(p.startswith(q + "/") for q in roots if q != p)]
    files = []
    directories = set()
    errors = []
    excluded = []
    with Root(source["path"]) as root:
        if list(root.identity) != source["identity"]:
            raise SafetyError("source_changed")

        def walk(names):
            try:
                with root.directory(names) as fd:
                    for entry in sorted(os.scandir(fd), key=lambda e: e.name):
                        rel = "/".join(names + [entry.name])
                        try:
                            parts(rel)
                            st = entry.stat(follow_symlinks=False)
                            if exclude_os_junk and (
                                entry.name == ".DS_Store"
                                and stat.S_ISREG(st.st_mode)
                                or not names
                                and entry.name in JUNK_DIRS
                                and stat.S_ISDIR(st.st_mode)
                            ):
                                excluded.append(
                                    {
                                        "relative_path": rel,
                                        "kind": "directory" if stat.S_ISDIR(st.st_mode) else "file",
                                        "rule": "os-junk-v1",
                                    }
                                )
                            elif stat.S_ISDIR(st.st_mode) and st.st_dev == root.identity[0]:
                                directories.add(rel)
                                walk(names + [entry.name])
                            elif stat.S_ISREG(st.st_mode) and st.st_dev == root.identity[0]:
                                files.append(
                                    {
                                        "relative_path": rel,
                                        "filename": entry.name,
                                        "extension": Path(entry.name).suffix,
                                        "size_bytes": st.st_size,
                                        "signature": signature(st),
                                        "media_type": media_type(entry.name),
                                    }
                                )
                            else:
                                errors.append(
                                    {
                                        "path": rel,
                                        "type": "unsupported_entry",
                                        "message": "不支持链接、特殊文件或嵌套挂载",
                                    }
                                )
                        except (OSError, ValueError, UnicodeError) as exc:
                            errors.append({"path": rel, "type": "scan_error", "message": str(exc)})
            except (OSError, ValueError) as exc:
                errors.append({"path": "/".join(names) or ".", "type": "scan_error", "message": str(exc)})

        for selected in roots:
            names = [] if selected == "." else parts(selected)
            if names:
                directories.update("/".join(names[:i]) for i in range(1, len(names) + 1))
            walk(names)
        root.check()
    return {
        "scan_id": uuid.uuid4().hex,
        "revision": 1,
        "source_id": source["source_id"],
        "source_session_id": source["session_id"],
        "selected_roots": roots,
        "files": files,
        "directories": sorted(directories),
        "errors": errors,
        "excluded": excluded,
        "file_count": len(files),
        "total_bytes": sum(f["size_bytes"] for f in files),
        "complete": not errors,
        "exclude_os_junk": exclude_os_junk,
    }


def linux_external_mounts(protected=()):
    """Read-only Linux mount discovery. No mount/umount or block device writes."""
    mountinfo = Path("/proc/self/mountinfo")
    if not mountinfo.exists():
        return []
    candidates = []
    for line in mountinfo.read_text().splitlines():
        fields = line.split()
        sep = fields.index("-")
        major_minor = fields[2]
        sysdev = Path("/sys/dev/block") / major_minor
        topology = str(sysdev.resolve())
        if "/usb" not in topology and "/mmc" not in topology:
            continue
        path = _mount_path(fields[4])
        p = Path(path)
        if any(p == Path(x) or p in Path(x).parents or Path(x) in p.parents for x in protected):
            continue
        volume_uuid = None
        properties = _udev_properties(major_minor)
        volume_uuid = properties.get("ID_FS_UUID")
        dev = Path(fields[sep + 2])
        by_uuid = Path("/dev/disk/by-uuid")
        if not volume_uuid and by_uuid.exists():
            for link in by_uuid.iterdir():
                if link.resolve() == dev.resolve():
                    volume_uuid = link.name
                    break
        try:
            vendor = properties.get("ID_VENDOR", "").replace("_", " ").strip()
            model = properties.get("ID_MODEL", "").replace("_", " ").strip()
            volume = properties.get("ID_FS_LABEL", "").replace("_", " ").strip() or p.name
            device = " ".join(x for x in (vendor, model) if x)
            label = f"{device} · {volume}" if device and device.lower() not in volume.lower() else volume
            item = source_record(p, label=label, volume_uuid=volume_uuid)
            item["mount_id"] = fields[0]
            item["filesystem"] = fields[sep + 1]
            item["identity_confidence"] = "high" if volume_uuid else "low"
            candidates.append(item)
        except (OSError, ValueError):
            continue
    # Root identity deduplicates dynamic mount aliases first; only distinct
    # roots sharing a UUID are treated as cloned devices and downgraded.
    return _deduplicate_mount_aliases(candidates)
