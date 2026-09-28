"""rclone transport operating on an already-open source descriptor."""

import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from blake3 import blake3

from .safeio import SafetyError


class RcloneCopier:
    def __init__(self, executable):
        self.executable = str(Path(executable).resolve())
        if not Path(self.executable).is_file():
            raise SafetyError("rclone_missing", "未找到 rclone，复制被禁用")

    def copy(self, fd, temp, settings, check, progress):
        os.lseek(fd, 0, os.SEEK_SET)
        source = f"/proc/self/fd/{fd}" if Path("/proc/self/fd").exists() else f"/dev/fd/{fd}"
        args = [
            self.executable,
            "copyto",
            source,
            str(temp),
            "--config",
            os.devnull,
            "--copy-links",
            "--immutable",
            "--retries",
            "1",
            "--low-level-retries",
            "1",
            "--transfers",
            "1",
            "--checkers",
            "1",
            "--multi-thread-streams",
            "0",
            "--disable",
            "Copy",
            "--local-no-clone",
            "--local-no-preallocate",
            "--log-level",
            "ERROR",
        ]
        if settings.bandwidth_mib:
            args += ["--bwlimit", f"{settings.bandwidth_mib}M"]
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}
        with tempfile.TemporaryFile() as errors:
            proc = subprocess.Popen(
                args, pass_fds=[fd], stdout=subprocess.DEVNULL, stderr=errors, env=env, start_new_session=True
            )
            last_update = 0.0
            try:
                while proc.poll() is None:
                    check()
                    now = time.monotonic()
                    if now - last_update > 0.25:
                        partials = (
                            [temp]
                            if Path(temp).exists()
                            else list(Path(temp).parent.glob(Path(temp).name + "*.partial"))
                        )
                        progress(sum(p.stat().st_size for p in partials if p.is_file()))
                        last_update = now
                    time.sleep(0.04)
                check()
                if proc.returncode:
                    errors.seek(0)
                    text = errors.read(8192).decode("utf-8", "replace")
                    if "no space left" in text.lower() or "read-only file system" in text.lower():
                        raise SafetyError("target_unavailable", text)
                    raise OSError(f"rclone exit={proc.returncode}: {text}")
                progress(Path(temp).stat().st_size)
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait(timeout=3)

    def copy_and_hash(self, fd, temp, settings, check, progress):
        """Copy a new file while hashing the same source bytes in one pass."""
        os.lseek(fd, 0, os.SEEK_SET)
        before = os.fstat(fd)
        digest = blake3(max_threads=settings.hash_threads)
        copied = 0
        started = time.monotonic()
        limit = settings.bandwidth_mib * 1024 * 1024 if settings.bandwidth_mib else 0
        with open(temp, "xb", buffering=0) as out:
            while True:
                check()
                block = os.read(fd, 8 * 1024 * 1024)
                if not block:
                    break
                digest.update(block)
                view = memoryview(block)
                while view:
                    written = out.write(view)
                    if not written:
                        raise OSError("short write while copying")
                    view = view[written:]
                copied += len(block)
                progress(copied)
                if limit:
                    delay = copied / limit - (time.monotonic() - started)
                    if delay > 0:
                        time.sleep(delay)
            os.fsync(out.fileno())
        after = os.fstat(fd)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise SafetyError("source_changed")
        return digest.hexdigest()
