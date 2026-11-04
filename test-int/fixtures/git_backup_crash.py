"""Whole-process exits at archive publication and restore handover boundaries."""

import asyncio
import os
from pathlib import Path
import sys
from unittest.mock import patch


def main() -> None:
    phase, source, destination = sys.argv[1:]
    target = Path(destination)
    if phase.startswith("handover_"):
        os.environ["BASIC_MEMORY_CONFIG_DIR"] = str(target / "config")
        from basic_memory.services import git_restore

        write, replace = git_restore.write_private, git_restore.os.replace

        def write_or_die(path, content):
            if path.name == "READY.json" and phase == "handover_before_ready":
                os._exit(84)
            write(path, content)
            if path.name == "READY.json" and phase == "handover_after_ready":
                os._exit(84)

        def replace_or_die(source_path, target_path):
            replace(source_path, target_path)
            if Path(target_path) == target / "config/config.json":
                os._exit(84)

        with (
            patch.object(git_restore, "write_private", write_or_die),
            patch.object(git_restore.os, "replace", replace_or_die),
        ):
            asyncio.run(git_restore.rebuild_restored_git_instance(target, timeout=30))
    else:
        from basic_memory.services import git_backup

        if phase == "restore_files":
            git_backup.restore_git_backup(Path(source), target)
            os._exit(84)
        link = git_backup.os.link

        def link_or_die(*args, **kwargs):
            if phase == "backup_before":
                os._exit(84)
            link(*args, **kwargs)
            os._exit(84)

        with patch.object(git_backup.os, "link", link_or_die):
            git_backup.create_git_backup(Path(source), target)
    raise RuntimeError("Crash boundary not reached")


if __name__ == "__main__":
    main()
