import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = (ROOT / "compose.yaml").read_text(encoding="utf-8")
LAN_COMPOSE = (ROOT / "compose.lan.yaml").read_text(encoding="utf-8")
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
DOCKER_DOC = (ROOT / "docs" / "docker-local.md").read_text(encoding="utf-8")


def test_dockerfile_pins_runtime_and_installs_media_tools():
    assert "FROM rclone/rclone:${RCLONE_VERSION} AS rclone" in DOCKERFILE
    assert "ARG RCLONE_VERSION=1.75.1" in DOCKERFILE
    assert "FROM python:${PYTHON_VERSION}-slim-bookworm" in DOCKERFILE
    assert "ARG PYTHON_VERSION=3.12.11" in DOCKERFILE
    for package in ("ca-certificates", "ffmpeg", "libimage-exiftool-perl"):
        assert re.search(rf"^\s+{re.escape(package)}(?:\s+\\)?$", DOCKERFILE, re.MULTILINE)
    assert "rm -rf /var/lib/apt/lists/*" in DOCKERFILE
    assert "USER muli:muli" in DOCKERFILE
    assert 'ENTRYPOINT ["muli-ingest"]' in DOCKERFILE


def test_compose_requires_explicit_paths_and_keeps_source_read_only():
    for variable in ("MULI_INGEST_STATE", "MULI_INGEST_STAGING", "MULI_INGEST_SOURCE"):
        assert f"${{{variable}:?" in COMPOSE
    assert re.search(
        r"target: /sources/source-01\n\s+read_only: true\s*$", COMPOSE, re.MULTILINE
    )
    assert "target: /state\n" in COMPOSE
    assert "target: /staging\n" in COMPOSE
    assert "--state\n      - /state" in COMPOSE
    assert "--staging\n      - /staging" in COMPOSE
    assert "--source\n      - /sources/source-01" in COMPOSE


def test_compose_is_loopback_only_and_runs_with_reduced_privileges():
    assert '"127.0.0.1:${MULI_INGEST_PORT:-8765}:8765"' in COMPOSE
    assert "MULI_INGEST_CONTAINER_LOOPBACK: \"1\"" in COMPOSE
    assert "--container-loopback" in COMPOSE
    assert "--host\n      - 0.0.0.0" in COMPOSE
    assert re.search(r"^\s+read_only: true\s*$", COMPOSE, re.MULTILINE)
    assert "/tmp:rw,noexec,nosuid,nodev" in COMPOSE
    assert re.search(r"cap_drop:\n\s+- ALL", COMPOSE)
    assert "no-new-privileges:true" in COMPOSE
    assert "restart: unless-stopped" in COMPOSE
    assert "stop_grace_period: 45s" in COMPOSE
    assert "driver: bridge" in COMPOSE
    assert "privileged: true" not in COMPOSE
    assert "docker.sock" not in COMPOSE
    assert not re.search(r"^\s+devices:\s*$", COMPOSE, re.MULTILINE)
    assert not re.search(r"/dev(?:/|$)", COMPOSE)


def test_compose_healthcheck_uses_public_health_endpoint_and_expected_command():
    assert "healthcheck:" in COMPOSE
    assert "http://127.0.0.1:8765/api/v1/health" in COMPOSE
    assert "interval: 15s" in COMPOSE
    assert "timeout: 3s" in COMPOSE
    assert "retries: 5" in COMPOSE
    assert "start_period: 10s" in COMPOSE
    for value in ("--rclone\n      - /usr/local/bin/rclone", "--port\n      - \"8765\""):
        assert value in COMPOSE
    assert "TZ: Asia/Shanghai" in COMPOSE
    assert "TZ: Asia/Shanghai" in LAN_COMPOSE


def test_lan_compose_requires_access_code_and_keeps_source_read_only():
    assert "--lan-access" in LAN_COMPOSE
    assert "--access-token-file\n      - /run/secrets/muli_ingest_access_code" in LAN_COMPOSE
    assert "${MULI_INGEST_ACCESS_CODE_FILE:?" in LAN_COMPOSE
    assert "${MULI_INGEST_BIND_ADDRESS:-0.0.0.0}:${MULI_INGEST_PORT:-18765}:8765" in LAN_COMPOSE
    assert "target: /run/secrets/muli_ingest_access_code\n        read_only: true" in LAN_COMPOSE
    assert "source: ${MULI_INGEST_EXTERNAL_ROOT:-/vol00}" in LAN_COMPOSE
    assert "target: /external\n        read_only: true" in LAN_COMPOSE
    assert "propagation: rslave" in LAN_COMPOSE
    assert "source: /run/udev/data\n        target: /run/udev/data\n        read_only: true" in LAN_COMPOSE
    assert "target: /sources/source-01\n        read_only: true" in LAN_COMPOSE
    assert "target: /sources/source-02\n        read_only: true" in LAN_COMPOSE
    assert "--source-label" in LAN_COMPOSE
    assert "--source-uuid" in LAN_COMPOSE
    assert "${MULI_INGEST_SOURCE_02:?" in LAN_COMPOSE
    assert "http://127.0.0.1:8765/api/v1/health" in LAN_COMPOSE
    assert "privileged: true" not in LAN_COMPOSE
    assert "docker.sock" not in LAN_COMPOSE


def test_docker_doc_keeps_scope_and_filesystem_contract_explicit():
    for phrase in (
        "只用于在本机 Docker",
        "不开放局域网",
        "不能再把它的子目录拆成其他挂载",
        "/staging/.ingest-trash",
        "docker compose down",
        "不要使用 `docker compose down -v`",
        "未验证真实相机数据",
        "NAS/fnOS",
    ):
        assert phrase in DOCKER_DOC
