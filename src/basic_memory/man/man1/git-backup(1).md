---
title: git-backup(1)
type: manpage
section: 1
name: git-backup
summary: back up all adopted local Git projects and private transaction state
generated: cli
---

# git-backup(1)

## NAME

**git-backup** — back up all adopted local Git projects and private transaction state

## SYNOPSIS

```
bm git backup DESTINATION --config CONFIG
```

## DESCRIPTION

Create a private offline archive containing published history, all private Git refs,
transaction metadata/receipts, original configuration, local mappings and regular
working files. Stop the API/MCP owner and other writers first; running ownership
or transaction locks refuse the backup. Existing outputs are never replaced.

Branch synchronization alone does not preserve this private state. Caller-held
transaction keys and cached embedding models are not included. Protect the archive:
configuration can contain credentials; the archive is not encrypted.

## OPTIONS

- **--config** — Explicit source config.json; host must stop

## SEE ALSO

- see_also [[git-restore(1)]]
- see_also [[git-finish-restore(1)]]
