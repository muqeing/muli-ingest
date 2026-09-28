"""Bounded, local-only media metadata extraction.

The extractor is deliberately an optional sidecar to ingest: a parser failure
produces a warning and never changes the source file or copy outcome.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import selectors
import subprocess
import time
from pathlib import Path
from typing import Any

_MAX_STDOUT = 4 * 1024 * 1024
_MAX_STDERR = 64 * 1024
_READ_CHUNK = 64 * 1024
_OFFSET_RE = re.compile(r"(?:\s|T)?(Z|[+-]\d{2}:?\d{2})$")
_DATE_RE = re.compile(r"^(\d{4})[:-](\d{2})[:-](\d{2})(?:[ T](.*))?$")


def _empty_result(status: str = "unavailable", warnings: list[str] | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "warnings": list(warnings or []),
        "capture_time": {
            "raw": None,
            "normalized": None,
            "wall_time": None,
            "timezone": None,
            "source": None,
            "raw_offset": None,
            "raw_subsecond": None,
            "confidence": None,
        },
        "camera": {"make": None, "model": None, "serial": None},
        "capture": {
            "iso": None,
            "shutter_speed": None,
            "aperture": None,
            "focal_length_mm": None,
            "lens": None,
        },
        "image": {"width": None, "height": None, "orientation": None},
        "video": {
            "duration_seconds": None,
            "width": None,
            "height": None,
            "fps": None,
            "avg_frame_rate": None,
            "r_frame_rate": None,
            "codec": None,
            "streams": None,
            "timecode": None,
            "creation_time_raw": None,
        },
        "sidecars": [],
    }


def _key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _tag(record: dict[str, Any], *names: str) -> Any:
    """Return the first matching ExifTool tag, ignoring its group prefix."""
    wanted = {_key(name) for name in names}
    # Prefer a fully qualified tag when callers distinguish EXIF from
    # QuickTime. This makes source attribution deterministic even when both
    # groups contain similarly named tags.
    for key, value in record.items():
        if _key(key) in wanted and ":" in str(key):
            return value
    for key, value in record.items():
        if _key(str(key).split(":")[-1]) in wanted:
            return value
    return None


def _present(value: Any) -> Any:
    if value is None or value == "":
        return None
    return value


def _number(value: Any) -> int | float | str | None:
    value = _present(value)
    if value is None or isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        try:
            number = float(stripped)
        except ValueError:
            match = re.match(r"^([+-]?\d+(?:\.\d+)?)\s*(?:mm)?$", stripped, re.IGNORECASE)
            if not match:
                return value
            number = float(match.group(1))
        return int(number) if number.is_integer() else number
    return value


def _offset(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    match = _OFFSET_RE.search(text)
    if not match:
        return None
    value = match.group(1)
    if value == "Z":
        return "+00:00"
    if len(value) == 5:
        return value[:3] + ":" + value[3:]
    return value


def _wall_time(raw: Any) -> tuple[str | None, str | None]:
    """Return an ISO wall time and any offset embedded in the raw value."""
    if raw is None:
        return None, None
    text = str(raw).strip()
    raw_offset = _offset(text)
    if raw_offset:
        text = _OFFSET_RE.sub("", text).strip()
    match = _DATE_RE.match(text)
    if not match:
        try:
            parsed = _dt.datetime.fromisoformat(text)
        except ValueError:
            return None, raw_offset
        return _iso_wall(parsed), raw_offset
    date_part = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    rest = match.group(4) or "00:00:00"
    # ExifTool can return a fractional second with either '.' or a comma.
    rest = rest.replace(",", ".")
    try:
        parsed = _dt.datetime.fromisoformat(f"{date_part}T{rest}")
    except ValueError:
        return None, raw_offset
    return _iso_wall(parsed), raw_offset


def _iso_wall(value: _dt.datetime) -> str:
    rendered = value.replace(tzinfo=None).isoformat(timespec="microseconds")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _capture_time(record: dict[str, Any]) -> dict[str, Any]:
    candidates = (
        (
            "EXIF:DateTimeOriginal",
            ("EXIF:DateTimeOriginal", "DateTimeOriginal"),
            ("EXIF:OffsetTimeOriginal", "OffsetTimeOriginal"),
            ("EXIF:SubSecTimeOriginal", "SubSecTimeOriginal"),
        ),
        ("QuickTime:CreateDate", ("QuickTime:CreateDate", "QuickTime:CreationDate", "CreationDate"), (), ()),
        (
            "EXIF:CreateDate",
            ("EXIF:CreateDate", "CreateDate"),
            ("EXIF:OffsetTime", "OffsetTime"),
            ("EXIF:SubSecTime", "SubSecTime"),
        ),
    )
    for source, raw_names, offset_names, subsecond_names in candidates:
        raw = _present(_tag(record, *raw_names))
        if raw is None:
            continue
        wall, embedded_offset = _wall_time(raw)
        explicit_offset = _present(_tag(record, *offset_names)) if offset_names else None
        raw_offset = str(explicit_offset).strip() if explicit_offset is not None else embedded_offset
        normalized_offset = _offset(raw_offset)
        normalized = None
        if wall and normalized_offset:
            try:
                parsed = _dt.datetime.fromisoformat(wall)
                normalized = parsed.replace(
                    tzinfo=_dt.timezone(
                        _dt.timedelta(hours=int(normalized_offset[1:3]), minutes=int(normalized_offset[4:]))
                        * (-1 if normalized_offset[0] == "-" else 1)
                    )
                ).isoformat()
            except (ValueError, OverflowError):
                normalized = None
        subsecond = _present(_tag(record, *subsecond_names)) if subsecond_names else None
        return {
            "raw": raw,
            "normalized": normalized,
            "wall_time": wall,
            "timezone": normalized_offset,
            "source": source,
            "raw_offset": raw_offset,
            "raw_subsecond": subsecond,
            "confidence": "timezone_aware" if normalized else "wall_time",
        }
    return _empty_result()["capture_time"]


def _sidecars(record: dict[str, Any]) -> list[dict[str, Any]]:
    value = _tag(record, "SidecarFiles", "SidecarFile", "RelatedFile")
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    return [
        {
            "path": str(item),
            "relative_path": str(item),
            "relation": "reported",
            "evidence": "ExifTool:SidecarFiles",
        }
        for item in values
        if item not in (None, "")
    ]


def _parse_exiftool(value: Any) -> dict[str, Any]:
    """Normalize one ExifTool JSON record without retaining GPS fields."""
    record = value if isinstance(value, dict) else {}
    result = _empty_result("ok")
    result["capture_time"] = _capture_time(record)
    result["camera"] = {
        "make": _present(_tag(record, "Make", "CameraMake")),
        "model": _present(_tag(record, "Model", "CameraModel")),
        "serial": _present(_tag(record, "SerialNumber", "CameraSerialNumber", "InternalSerialNumber")),
    }
    result["capture"] = {
        "iso": _number(_tag(record, "ISO", "ISOSpeed")),
        "shutter_speed": _number(_tag(record, "ExposureTime", "ShutterSpeed")),
        "aperture": _number(_tag(record, "FNumber", "Aperture")),
        "focal_length_mm": _number(_tag(record, "FocalLength", "FocalLengthIn35mmFormat")),
        "lens": _present(_tag(record, "LensModel", "Lens", "LensDescription")),
    }
    result["image"] = {
        "width": _number(_tag(record, "ImageWidth", "ExifImageWidth", "PixelXDimension", "ImageSizeWidth")),
        "height": _number(
            _tag(record, "ImageHeight", "ExifImageHeight", "PixelYDimension", "ImageSizeHeight")
        ),
        "orientation": _number(_tag(record, "Orientation")),
    }
    result["video"] = {
        "duration_seconds": _number(_tag(record, "Duration")),
        "width": _number(_tag(record, "ImageWidth", "VideoFrameSizeWidth")),
        "height": _number(_tag(record, "ImageHeight", "VideoFrameSizeHeight")),
        "fps": _present(_tag(record, "VideoFrameRate", "FrameRate")),
        "avg_frame_rate": None,
        "r_frame_rate": None,
        "codec": _present(_tag(record, "CompressorName", "VideoCodec")),
        "streams": None,
        "timecode": _present(_tag(record, "TimeCode", "Timecode")),
        "creation_time_raw": _present(_tag(record, "CreateDate", "CreationDate")),
    }
    result["sidecars"] = _sidecars(record)
    return result


def _parse_ffprobe(value: Any) -> dict[str, Any]:
    """Normalize ffprobe JSON while retaining original frame-rate fractions."""
    result = _empty_result("ok")
    if not isinstance(value, dict):
        return result
    streams = value.get("streams") if isinstance(value.get("streams"), list) else []
    video_stream = next(
        (item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"), {}
    )
    fmt = value.get("format") if isinstance(value.get("format"), dict) else {}
    tags = fmt.get("tags") if isinstance(fmt.get("tags"), dict) else {}
    stream_tags = video_stream.get("tags") if isinstance(video_stream.get("tags"), dict) else {}
    avg = _present(video_stream.get("avg_frame_rate"))
    rate = _present(video_stream.get("r_frame_rate"))
    selected_streams: list[dict[str, Any]] = []
    keep = (
        "index",
        "codec_type",
        "codec_name",
        "width",
        "height",
        "duration",
        "avg_frame_rate",
        "r_frame_rate",
        "channels",
        "sample_rate",
        "channel_layout",
    )
    for stream in streams:
        if isinstance(stream, dict):
            selected_streams.append({key: stream[key] for key in keep if key in stream})
    result["video"] = {
        "duration_seconds": _number(fmt.get("duration", video_stream.get("duration"))),
        "width": _number(video_stream.get("width")),
        "height": _number(video_stream.get("height")),
        "fps": avg or rate,
        "avg_frame_rate": avg,
        "r_frame_rate": rate,
        "codec": _present(video_stream.get("codec_name")),
        "streams": selected_streams,
        "timecode": _present(tags.get("timecode") or stream_tags.get("timecode")),
        "creation_time_raw": _present(tags.get("creation_time") or stream_tags.get("creation_time")),
    }
    return result


def _run_bounded(command: list[str], timeout: float) -> tuple[str, bytes, int | None]:
    """Run an argv without a shell, capping output and wall time."""
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
    except (FileNotFoundError, PermissionError):
        return "missing", b"", None
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    for stream in (process.stdout, process.stderr):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ)
    stdout = bytearray()
    stderr = bytearray()
    deadline = time.monotonic() + timeout
    reason = "ok"
    while selector.get_map():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            reason = "timeout"
            process.kill()
            remaining = 0.2
        for key, _ in selector.select(max(0.0, min(remaining, 0.1))):
            try:
                chunk = os.read(key.fd, _READ_CHUNK)
            except BlockingIOError:
                continue
            if not chunk:
                selector.unregister(key.fileobj)
                key.fileobj.close()
                continue
            target = stdout if key.fileobj is process.stdout else stderr
            limit = _MAX_STDOUT if target is stdout else _MAX_STDERR
            if len(target) + len(chunk) > limit:
                reason = "output_limit"
                process.kill()
                chunk = chunk[: max(0, limit - len(target))]
            target.extend(chunk)
    process.wait()
    selector.close()
    if reason != "ok":
        return reason, bytes(stdout), process.returncode
    if process.returncode:
        return "failed", bytes(stdout), process.returncode
    return "ok", bytes(stdout), process.returncode


def _decode_json(raw: bytes) -> Any:
    return json.loads(raw.decode("utf-8-sig"))


def extract_metadata(
    path: Path,
    media_type: str,
    level: str = "standard",
    timeout: float = 30,
    *,
    exiftool: str | None = None,
    ffprobe: str | None = None,
) -> dict[str, Any]:
    """Extract optional metadata from a verified local file.

    ``level="minimal"`` intentionally performs no tool invocation.  All
    subprocesses receive argv arrays and a local protocol whitelist; no shell
    or network access is used.
    """
    if level == "minimal":
        return _empty_result("not_requested")
    if level != "standard":
        raise ValueError("level must be 'minimal' or 'standard'")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    path = Path(path)
    result = _empty_result()
    if not path.is_file():
        result["warnings"].append("file_unreadable")
        return result
    kind = str(media_type).lower()
    if kind not in {"photo", "video", "image"}:
        result["warnings"].append("unsupported_media_type")
        return result

    successes = 0
    attempts = 0
    exiftool_command = exiftool or "exiftool"
    attempts += 1
    state, raw, _ = _run_bounded(
        [exiftool_command, "-config", "", "-json", "-G1", "-s", "-n", "-api", "QuickTimeUTC=0", str(path)],
        timeout,
    )
    if state == "ok":
        try:
            parsed = _decode_json(raw)
            if not isinstance(parsed, list) or not parsed or not isinstance(parsed[0], dict):
                raise ValueError("unexpected ExifTool JSON shape")
            parsed_result = _parse_exiftool(parsed[0])
            for key in ("capture_time", "camera", "capture", "image", "video", "sidecars"):
                result[key] = parsed_result[key]
            successes += 1
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            result["warnings"].append("exiftool_invalid_json")
    else:
        result["warnings"].append(f"exiftool_{state}")

    if kind == "video":
        ffprobe_command = ffprobe or "ffprobe"
        attempts += 1
        command = [
            ffprobe_command,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            "-protocol_whitelist",
            "file,pipe",
            str(path),
        ]
        state, raw, _ = _run_bounded(command, timeout)
        if state == "ok":
            try:
                parsed_result = _parse_ffprobe(_decode_json(raw))
                result["video"] = parsed_result["video"]
                if result["capture_time"]["raw"] is None and result["video"]["creation_time_raw"]:
                    result["capture_time"] = {
                        **_empty_result()["capture_time"],
                        "raw": result["video"]["creation_time_raw"],
                        "wall_time": _wall_time(result["video"]["creation_time_raw"])[0],
                        "source": "ffprobe:format.tags.creation_time",
                        "confidence": "wall_time",
                    }
                successes += 1
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
                result["warnings"].append("ffprobe_invalid_json")
        else:
            result["warnings"].append(f"ffprobe_{state}")

    if not attempts:
        result["status"] = "unavailable"
    elif successes == attempts and not result["warnings"]:
        result["status"] = "ok"
    elif successes:
        result["status"] = "partial"
    else:
        result["status"] = "unavailable"
    return result
