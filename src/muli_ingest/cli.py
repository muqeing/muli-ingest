"""Command line entry point for the loopback ingest service."""

from __future__ import annotations

import argparse
import ipaddress
import os
import shutil
from pathlib import Path

import uvicorn

from .access import AccessConfigurationError, LanAccess
from .api import create_app
from .engine import Engine

_DEMO_MARKER = "木梨 Ingest 0.1 合成演示素材\n不是真实客户或相机素材。\n"


def _default_rclone() -> str:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "work" / "tools" / "rclone"
        if candidate.is_file():
            return str(candidate)
    return "rclone"


def _is_loopback_host(value: str) -> bool:
    if value == "localhost":
        return True
    try:
        if ipaddress.ip_address(value).is_loopback:
            return True
    except ValueError:
        pass
    return False


def _bind_host(
    value: str,
    container_loopback: bool,
    environ: dict[str, str] | None = None,
    *,
    lan_access: bool = False,
) -> str:
    """Allow all-interface binding only inside the reviewed loopback-published container."""
    if _is_loopback_host(value):
        return value
    if lan_access:
        if value in {"0.0.0.0", "::"}:
            return value
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            address = None
        if address and address.is_private and not address.is_link_local:
            return value
    environment = os.environ if environ is None else environ
    if (
        value in {"0.0.0.0", "::"}
        and container_loopback
        and environment.get("MULI_INGEST_CONTAINER_LOOPBACK") == "1"
    ):
        return value
    raise ValueError(
        "--host 只能使用回环地址；容器内全接口监听需要同时设置 "
        "--container-loopback 和镜像内 MULI_INGEST_CONTAINER_LOOPBACK=1"
    )


def _port(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--port 必须是整数") from exc
    if not 1 <= result <= 65535:
        raise argparse.ArgumentTypeError("--port 必须在 1 到 65535 之间")
    return result


def _demo_source(state: Path) -> Path:
    """Create a new, clearly synthetic source without touching an existing path."""
    target = state.absolute().parent / "demo-source"
    if target.exists():
        marker = target / "README-DEMO.txt"
        if target.is_dir() and marker.is_file() and marker.read_text(encoding="utf-8") == _DEMO_MARKER:
            return target
        raise RuntimeError(f"拒绝复用未知演示来源：{target}")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.mkdir(mode=0o700)
    samples = {
        "README-DEMO.txt": _DEMO_MARKER,
        "demo-note.txt": "synthetic demo material\n",
        "nested/demo-sidecar.txt": "synthetic sidecar\n",
    }
    for relative, content in samples.items():
        path = target / relative
        path.parent.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, content.encode("utf-8"))
        finally:
            os.close(fd)
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="木梨 Ingest 本地回环服务（0.1 测试版）")
    parser.add_argument("--demo", action="store_true", help="创建隔离的合成 txt 来源并以演示模式运行")
    parser.add_argument("--state", type=Path, default=Path.cwd() / ".muli-ingest-state")
    parser.add_argument("--staging", type=Path)
    parser.add_argument("--source", action="append", default=[], help="来源目录，可重复指定")
    parser.add_argument(
        "--source-label", action="append", default=[], help="来源显示名称，顺序与 --source 一致"
    )
    parser.add_argument(
        "--source-uuid", action="append", default=[], help="来源卷 UUID，顺序与 --source 一致"
    )
    parser.add_argument("--rclone", default=_default_rclone())
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--container-loopback",
        action="store_true",
        help="仅供端口发布到宿主127.0.0.1的官方容器使用；不会开放局域网访问",
    )
    parser.add_argument(
        "--lan-access",
        action="store_true",
        help="允许私有局域网访问；必须同时提供访问口令文件",
    )
    parser.add_argument("--access-token-file", type=Path, help="局域网访问口令文件（不会写入数据库）")
    parser.add_argument("--allowed-host", action="append", default=[], help="额外允许的局域网主机名")
    parser.add_argument("--secure-cookie", action="store_true", help="仅在 HTTPS 入口下发送会话 Cookie")
    parser.add_argument("--port", type=_port, default=8765)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        bind_host = _bind_host(
            args.host,
            args.container_loopback,
            lan_access=args.lan_access,
        )
    except ValueError as exc:
        parser.error(str(exc))
    state = Path(os.path.abspath(args.state))
    staging = Path(os.path.abspath(args.staging or state.parent / "muli-ingest-staging"))
    if args.demo and args.source:
        parser.error("--demo 不能与 --source 同时使用")
    if args.lan_access and not args.access_token_file:
        parser.error("--lan-access 必须同时提供 --access-token-file")
    if args.access_token_file and not args.lan_access:
        parser.error("--access-token-file 仅能与 --lan-access 一起使用")
    if args.secure_cookie and not args.lan_access:
        parser.error("--secure-cookie 仅能与 --lan-access 一起使用")
    if not args.demo and not args.source:
        parser.error("本地模式至少需要一个 --source；演示模式请使用 --demo")
    if args.source_label and len(args.source_label) != len(args.source):
        parser.error("--source-label 数量必须与 --source 一致")
    if args.source_uuid and len(args.source_uuid) != len(args.source):
        parser.error("--source-uuid 数量必须与 --source 一致")
    rclone = (
        str(Path(args.rclone).absolute())
        if Path(args.rclone).is_file()
        else (shutil.which(args.rclone) or "")
    )
    if not rclone:
        parser.error(f"未找到 rclone：{args.rclone}")

    try:
        lan_access = (
            LanAccess.from_file(args.access_token_file, secure_cookie=args.secure_cookie)
            if args.lan_access
            else None
        )
        source_paths = [_demo_source(state)] if args.demo else [Path(item).absolute() for item in args.source]
        engine = Engine(
            state,
            staging,
            source_paths,
            rclone=rclone,
            mode="demo" if args.demo else "local",
            allow_cleanup=False,
            source_labels=args.source_label,
            source_uuids=args.source_uuid,
        )
    except (AccessConfigurationError, OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    app = create_app(engine, lan_access=lan_access, allowed_hosts=tuple(args.allowed_host))
    print(f"木梨 Ingest {app.version} listening on http://{bind_host}:{args.port}/")
    try:
        uvicorn.run(app, host=bind_host, port=args.port)
    finally:
        engine.close()


if __name__ == "__main__":
    main()
