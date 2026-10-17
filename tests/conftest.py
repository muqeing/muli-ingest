"""Collection-time portability hooks for the offline Linux fixture bundle."""

from pathlib import Path
import shutil


def pytest_collection_modifyitems(session, config, items):  # noqa: ARG001
    """Replace macOS-only rclone constants after test modules are imported."""
    resolved = shutil.which("rclone")
    rclone = Path(resolved) if resolved else Path("/usr/local/bin/rclone")
    for item in items:
        module = getattr(item, "module", None)
        if module is not None and hasattr(module, "RCLONE"):
            module.RCLONE = rclone
