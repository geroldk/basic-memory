"""Fresh-process restore rebuild; configure private paths before application imports."""

import asyncio
import os
from pathlib import Path
import sys


def main() -> None:
    destination = Path(sys.argv[1]).resolve()
    os.environ["BASIC_MEMORY_CONFIG_DIR"] = str(destination / "config")
    os.environ["BASIC_MEMORY_HOME"] = str(destination / "notes")
    from basic_memory.services.git_restore import rebuild_restored_git_instance

    asyncio.run(rebuild_restored_git_instance(destination, timeout=float(sys.argv[2])))


if __name__ == "__main__":  # pragma: no cover - fresh-process CLI boundary
    main()
