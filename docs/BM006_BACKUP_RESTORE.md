# BM-006: offline backup and independent restore

The experimental local Git host now has a complete offline backup path. It is
separate from branch synchronization, which transfers published history alone.
The CLI does not initialize or migrate the caller's database. A broken caller
profile does not prevent restoring into another, new profile.

## Backup contents and consistency

`bm git backup DESTINATION --config /absolute/source/config.json` requires an
adopted, entirely local Git-managed SQLite installation. Every configured project
must have its own bare repository and matching local root binding.

Stop that installation's API/MCP owner and all other writers, including editors,
imports and file synchronization, before backing it up. The command takes the
same worker ownership locks as the host, plus each transaction metadata lock,
across all projects. A running owner or concurrent transaction refuses the backup.
No service or timer is stopped automatically. Locks are released on process death.

The private ZIP archive contains:

- The original configuration and project-name/UUID/root/repository mapping.
- Complete Git bundles containing all refs and reachable history: published
  commits, private candidate drafts, incoming histories and no-op receipts.
- Transaction JSON, including ownership hashes, original leases, terminal
  evidence and the retained source/successor links used in conflict recovery.
- Local binding, adoption and peer-observation metadata.
- Regular working files, including unpublished editor changes and assets.
  Locks and the working root's internal control directory are excluded.

An inventory records each member's size and SHA-256. Transaction pointers, source
pins and committed evidence are checked before publication. The configuration and
Git ref inventories must still match their captured state. The complete archive
is published without replacing an existing destination; temporary artifacts are
private. A lost success response leaves a complete archive which can be restored.

The archive and restored directories use private permissions. The archive contains
private notes, drafts and potentially credentials from configuration. It is not
encrypted or authenticated by a signature and belongs in protected backup storage,
outside indexed roots and public repositories. SHA-256 detects corruption; restore
assumes a trusted archive. Symlinks and special working files are refused.

Caller-held transaction keys are never stored in plaintext by BM. Preserve them
separately in the caller's protected state if private drafts must remain accessible.
Backup does not extend transaction leases or retention periods.

## Restore contract

```sh
bm git backup /srv/private-backups/memory.bm.zip --config /srv/bm/config/config.json
bm git restore /srv/private-backups/memory.bm.zip /srv/bm-restored --timeout 900
```

Restore accepts only a new destination whose parent already exists. Existing
targets, destinations overlapping source data, duplicate or unsafe archive paths,
unexpected members and inventory/checksum mismatches are refused. No production
database, configuration or notes are overwritten.

The new instance contains `config/`, `repositories/`, `notes/` and `recovery/`.
Project and note UUIDs, exact published Markdown and Git history survive. Local
root mappings are regenerated; old absolute bindings are retained as evidence,
not activated. Published files are materialized from Git into empty note roots.
Working files from the backup are retained under `recovery/working/<project-uuid>`
for review, outside every indexed root. Editor changes cannot replace published
content during restoration.

Transaction metadata and refs are restored together. Peer observations are retained
in recovery evidence and the restored repository; remote configuration is disabled
until explicitly reviewed and enabled. Adoption/binding evidence stays in recovery.
The active profile is local, with automatic package updates disabled. Original
configuration remains in `recovery/original-config.json`.

SQLite, its numeric IDs, progress rows, graph, full-text and vector indexes are
rebuilt through the ordinary Git host. The CLI uses a fresh child process configured
for the new profile before opening a database. It waits for all four projection
stages, including physical vectors. Local embedding models must be available or
downloadable; cached model artifacts are not included. This rebuild is not a model
quality or inference-latency benchmark.

Until rebuild completes, `RESTORE_INCOMPLETE` is present and normal host startup
is disabled in the restored configuration. A failed rebuild exits nonzero and
retains its target. Resume it with:

```sh
bm git finish-restore /srv/bm-restored --timeout 900
```

Completion writes `READY.json`, enables ordinary startup, and removes the incomplete
marker. A process death between these steps is resumable, including the case with
both markers. The CLI reports success only when the child succeeded and the final
marker state agrees. Select the new instance explicitly when starting it:

```sh
BASIC_MEMORY_CONFIG_DIR=/srv/bm-restored/config bm mcp
```

This is a point-in-time restore. Changes and receipts created after the backup
are absent; callers must reconcile their state before retrying operations from a
later timeline. V1 has no global exactly-once guarantee across independent hosts
or restores. General operation replay/epochs remain V2 work.

## Evidence and limits

`tests/services/test_git_backup.py` checks history, drafts, source pins, no-op and
effective commit receipts, lost publication answers, independent project mappings,
working edits, integrity failures and refused overwrites. Whole child processes
exit before/after archive publication and after filesystem restore.

`test-int/test_git_backup_integration.py` rebuilds a fresh SQLite database, reads
the original UUIDs through the ordinary API, and checks graph, FTS, vector manifests
and physical vectors. It tests failed rebuilds, profile changes, and whole-process
death before/after READY and after configuration handover. Deterministic test
embeddings exercise the real storage/projection pipeline. These are process-crash
proofs, not a power-loss/filesystem-durability certification.

The initial implementation targets the local Linux Git host. There is no online
backup API, scheduler, automatic retention, encryption or active-source rollback.
A pilot requires an independent restore proof before activation and another backup/
restore exercise with actual pilot changes. Productive paths, storage, peers and
cutover still require explicit operational decisions.
