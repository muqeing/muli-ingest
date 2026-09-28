"""Descriptor-based source reads and no-clobber publication; never modify source."""

import contextlib
import ctypes
import json
import os
import stat
import sys
import uuid
from pathlib import Path

from blake3 import blake3


class SafetyError(ValueError):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


def parts(relative):
    if not isinstance(relative, str) or not relative or relative.startswith("/"):
        raise SafetyError("invalid_path")
    relative.encode("utf-8", "strict")
    values = relative.split("/")
    if any(p in ("", ".", "..") or "\x00" in p for p in values):
        raise SafetyError("invalid_path")
    return values


def signature(st):
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Root:
    def __init__(self, path):
        self.path = Path(path).absolute()
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in self.path.parts[1:]:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = nxt
            self.fd = fd
            st = os.fstat(fd)
            self.identity = (st.st_dev, st.st_ino)
        except BaseException:
            os.close(fd)
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        os.close(self.fd)

    def check(self):
        st = os.stat(self.path, follow_symlinks=False)
        if not stat.S_ISDIR(st.st_mode) or (st.st_dev, st.st_ino) != self.identity:
            raise SafetyError("root_changed")

    @contextlib.contextmanager
    def directory(self, components, create=False):
        self.check()
        fd = os.dup(self.fd)
        try:
            for name in components:
                if create:
                    try:
                        os.mkdir(name, mode=0o700, dir_fd=fd)
                        os.fsync(fd)
                    except FileExistsError:
                        pass
                nxt = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                if os.fstat(nxt).st_dev != self.identity[0]:
                    os.close(nxt)
                    raise SafetyError("nested_mount")
                os.close(fd)
                fd = nxt
            yield fd
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def file(self, relative):
        names = parts(relative)
        with self.directory(names[:-1]) as parent:
            fd = os.open(names[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_dev != self.identity[0]:
                    raise SafetyError("unsupported_entry")
                yield fd
                self.check()
            finally:
                os.close(fd)

    def mkdir(self, relative):
        with self.directory(parts(relative), create=True):
            pass

    def publish(self, temp, relative):
        names = parts(relative)
        with self.directory(names[:-1], create=True) as parent:
            os.link(temp, names[-1], dst_dir_fd=parent, follow_symlinks=False)
            os.fsync(parent)
        # After a crash either name may remain. The final name is never replaced.
        Path(temp).unlink()
        sync_dir(Path(temp).parent)


def digest_fd(fd, threads=2, check=lambda: None):
    before = signature(os.fstat(fd))
    os.lseek(fd, 0, os.SEEK_SET)
    h = blake3(max_threads=threads)
    while True:
        check()
        block = os.read(fd, 1024 * 1024)
        if not block:
            break
        h.update(block)
    if signature(os.fstat(fd)) != before:
        raise SafetyError("source_changed")
    return h.hexdigest()


def digest_file(path, threads=2, check=lambda: None):
    path = Path(path)
    with Root(path.parent) as root, root.file(path.name) as fd:
        return digest_fd(fd, threads, check)


def publish_file(temp, target):
    with Root(Path(target).parent) as root:
        root.publish(temp, Path(target).name)


def atomic_json(path, value):
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def atomic_bytes(path, data):
    path = Path(path)
    temp = path.parent / ("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    with open(temp, "xb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, path)  # Reports/settings only. Never used for SOURCE_DATA.
    sync_dir(path.parent)


def move_directory(source, target):
    """Atomic exclusive directory move on macOS/Linux; unsupported platforms fail closed."""
    source, target = Path(source), Path(target)
    if source.is_symlink() or not source.is_dir() or target.parent.is_symlink():
        raise SafetyError("invalid_directory")
    libc = ctypes.CDLL(None, use_errno=True)
    a, b = os.fsencode(source), os.fsencode(target)
    if sys.platform == "linux" and hasattr(libc, "renameat2"):
        rc = libc.renameat2(-100, ctypes.c_char_p(a), -100, ctypes.c_char_p(b), 1)
    elif sys.platform == "darwin":
        rc = libc.renamex_np(ctypes.c_char_p(a), ctypes.c_char_p(b), 4)
    else:
        raise SafetyError("exclusive_directory_move_unsupported")
    if rc:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e), str(target))
    sync_dir(source.parent)
    sync_dir(target.parent)
