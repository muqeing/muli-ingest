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


def media_type(name):
    ext = Path(name).suffix.lower()
    return "photo" if ext in PHOTOS else "video" if ext in VIDEOS else "other"


def source_record(path, label=None, volume_uuid=None):
    path = Path(path).absolute()
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
        path = fields[4]
        for esc, char in [("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")]:
            path = path.replace(esc, char)
        p = Path(path)
        if any(p == Path(x) or p in Path(x).parents or Path(x) in p.parents for x in protected):
            continue
        volume_uuid = None
        properties = {}
        udev = Path("/run/udev/data") / f"b{major_minor}"
        try:
            for row in udev.read_text(errors="replace").splitlines():
                if row.startswith("E:") and "=" in row:
                    key, value = row[2:].split("=", 1)
                    properties[key] = value
        except OSError:
            pass
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
    # Cloned UUIDs are not a trustworthy unique identity.
    repeated = {
        x["source_id"] for x in candidates if sum(y["source_id"] == x["source_id"] for y in candidates) > 1
    }
    for item in candidates:
        if item["source_id"] in repeated:
            item["source_id"] = blake3((item["source_id"] + item["path"]).encode()).hexdigest()[:24]
            item["identity_confidence"] = "low"
    return candidates
