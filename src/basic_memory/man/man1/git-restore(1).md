---
title: git-restore(1)
type: manpage
section: 1
name: git-restore
summary: restore a private Git backup into an independent new instance
generated: cli
---

# git-restore(1)

## NAME

**git-restore** — restore a private Git backup into an independent new instance

## SYNOPSIS

```
bm git restore BACKUP_PATH DESTINATION [--timeout TIMEOUT]
```

## DESCRIPTION

Validate inventory, paths and checksums, restore Git and private transaction state,
and rebuild SQLite content, files, graph/full text and vectors through the normal
Git host. DESTINATION must be new, its parent must exist, and it must be outside
the original sources. Existing data is never overwritten.

UUIDs and history survive; local paths are remapped. Original working files and
bindings remain under recovery/ outside the indexed note roots. Peer configuration
and automatic package updates remain disabled. Local model artifacts must be
available or downloadable. Caller-held transaction keys are preserved separately.

A failed or interrupted rebuild leaves RESTORE_INCOMPLETE and a blocked profile.
Use git-finish-restore to resume it. READY.json and the absence of the incomplete
marker indicate success. The caller's installation is not initialized or migrated.

## OPTIONS

- **--timeout** (default: 300) — Index rebuild time limit in seconds

## SEE ALSO

- see_also [[git-backup(1)]]
- see_also [[git-finish-restore(1)]]
