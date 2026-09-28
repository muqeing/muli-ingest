import json
import sys

from muli_ingest.metadata import _parse_exiftool, _parse_ffprobe, extract_metadata


def test_exiftool_parser_preserves_time_source_and_unknown_gps():
    parsed = _parse_exiftool(
        {
            "EXIF:Make": "Sony",
            "EXIF:Model": "ILCE-7RM5",
            "EXIF:DateTimeOriginal": "2026:09:27 12:34:56",
            "EXIF:OffsetTimeOriginal": "+08:00",
            "EXIF:SubSecTimeOriginal": "123",
            "EXIF:ISO": 400,
            "EXIF:FocalLength": 35,
            "EXIF:ImageWidth": 100,
            "EXIF:ImageHeight": 80,
            "GPS:GPSLatitude": 31.2,
        }
    )
    assert parsed["capture_time"] == {
        "raw": "2026:09:27 12:34:56",
        "normalized": "2026-09-27T12:34:56+08:00",
        "wall_time": "2026-09-27T12:34:56",
        "timezone": "+08:00",
        "source": "EXIF:DateTimeOriginal",
        "raw_offset": "+08:00",
        "raw_subsecond": "123",
        "confidence": "timezone_aware",
    }
    assert parsed["camera"]["make"] == "Sony"
    assert parsed["capture"]["iso"] == 400
    assert parsed["image"]["width"] == 100
    assert parsed["camera"]["serial"] is None
    assert "GPSLatitude" not in json.dumps(parsed)


def test_ffprobe_parser_keeps_vfr_rationals_and_all_streams():
    parsed = _parse_ffprobe(
        {
            "format": {"duration": "2.5", "tags": {"creation_time": "2026-09-27T04:00:00Z"}},
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                    "avg_frame_rate": "30000/1001",
                    "r_frame_rate": "60000/1001",
                },
                {"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 2},
            ],
        }
    )
    assert parsed["video"]["fps"] == "30000/1001"
    assert parsed["video"]["avg_frame_rate"] == "30000/1001"
    assert parsed["video"]["r_frame_rate"] == "60000/1001"
    assert len(parsed["video"]["streams"]) == 2
    assert parsed["video"]["duration_seconds"] == 2.5


def test_quicktime_naive_time_is_not_treated_as_utc():
    parsed = _parse_exiftool(
        {
            "QuickTime:CreateDate": "2026:09:27 12:34:56",
            "EXIF:CreateDate": "2026:09:27 11:00:00",
        }
    )
    assert parsed["capture_time"]["source"] == "QuickTime:CreateDate"
    assert parsed["capture_time"]["wall_time"] == "2026-09-27T12:34:56"
    assert parsed["capture_time"]["normalized"] is None


def test_minimal_does_not_invoke_tools(tmp_path, monkeypatch):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"not a real video")
    monkeypatch.setattr(
        "muli_ingest.metadata._run_bounded", lambda *_args: (_ for _ in ()).throw(AssertionError())
    )
    assert extract_metadata(media, "video", level="minimal")["status"] == "not_requested"


def test_bad_file_tool_output_is_unavailable_without_copy_failure(tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"bad")
    result = extract_metadata(media, "video", exiftool=sys.executable, ffprobe=sys.executable, timeout=1)
    assert result["status"] in {"unavailable", "partial"}
    assert result["warnings"]
