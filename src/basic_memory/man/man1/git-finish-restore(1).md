---
title: git-finish-restore(1)
type: manpage
section: 1
name: git-finish-restore
summary: resume an incomplete independent Git restore and verify every index
generated: cli
---

# git-finish-restore(1)

## NAME

**git-finish-restore** — resume an incomplete independent Git restore and verify every index

## SYNOPSIS

```
bm git finish-restore DESTINATION [--timeout TIMEOUT]
```

## DESCRIPTION

Reopen only a reserved incomplete restored profile in a fresh process. Verify its
mapping and published commits, then recover content, files, graph/full text and
vectors. Ordinary source installations and already finished targets are refused.
This also handles an interrupted final handover with both completion markers.

No peer is enabled. Success requires READY.json and removal of RESTORE_INCOMPLETE.
To start the resulting instance, explicitly select DESTINATION/config using
BASIC_MEMORY_CONFIG_DIR.

## OPTIONS

- **--timeout** (default: 300) — Index rebuild time limit in seconds

## SEE ALSO

- see_also [[git-restore(1)]]
- see_also [[git-backup(1)]]
