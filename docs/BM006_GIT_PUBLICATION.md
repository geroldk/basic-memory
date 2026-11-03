# BM-006: isolated Git publication and identity recovery

Status: two isolated implementation slices, 2026-11-03. This is experimental code on
`feat/bm006-git-publication`, based on `local/bm003-baseline`. It is not enabled by
configuration, dependency wiring, CLI, API or MCP. Existing projects and the running
installation continue to use the existing write path.

## Binding scope

Each BM project has its own private note repository. The public implementation
repository is separate. Git defines published note content; local SQLite content
reads, graph, full text and vectors may lag or mix **published** revisions in V1.
Private drafts must never leak through these readers. Explicit consistent snapshot
read APIs and general per-operation idempotency/replay are V2 work.

Effective implicit writes produce one commit; unchanged bytes produce no empty
commit. Explicit multi-note transactions have a fixed project base, keep private
drafts without holding Git/SQL locks during agent work, and eventually publish one
commit. V1 rejects a changed base anywhere in that project. Commits in another
project do not invalidate it. Offline divergence requires explicit review; no force
push, timestamp winner or silent rebase is promised.

## Implemented primitive

`repository/git_project_repository.py` owns a **dedicated bare repository outside every
indexed note directory**. The normal implementation checkout, editor working files,
global Git configuration and user index do not participate. Creation refuses an
existing directory. An initial commit stores the project UUID and establishes the basis.

1. `prepare(transaction_id, base_commit, files, note_ids)` builds a full Markdown tree using
   a temporary Git index and immutable blobs. It validates relative note paths and
   retains the candidate under `refs/bm/candidates/<uuid>/<sha>`. Preparation does
   not change the published branch or hold a planning-time lock.
2. Effective candidates contain a `Basic-Memory-Transaction: <uuid>` commit trailer.
   `publish()` compares the fixed base and advances `refs/heads/main`. The terminal
   evidence is **in that same commit**, not dependent on a second ref update.
3. `transaction_status()` finds the transaction in published history. Retrying a
   terminal publication returns its original SHA even after subsequent commits;
   it never resets the branch or publishes the candidate a second time.
4. A no-op records a local terminal receipt pointing to its existing base. One ref
   transaction locks/verifies the branch but modifies only the receipt. Effective
   publications also verify that this no-op receipt is absent. Thus a no-op and an
   effective candidate cannot both close the same transaction.
5. `snapshot()` reads exact UTF-8 Markdown bytes from a published commit. Candidate
   commits outside published ancestry and non-regular files are rejected. This is
   an internal primitive, not a public V2 consistency API.

## Portable identities and SQL projection

Every tree contains `.basic-memory/project.json`, validated by `schemas/git_project.py`:

```json
{
  "format_version": 1,
  "project_external_id": "11111111-1111-4111-8111-111111111111",
  "notes": [
    {"external_id": "22222222-2222-4222-8222-222222222222", "path": "notes/example.md"}
  ]
}
```

The manifest and Markdown publish in the same tree. UUIDs are explicit: missing or
duplicate identities/paths, unidentified files, reserved control paths and unknown
manifest versions fail validation. An existing path cannot silently receive a new
UUID. Canonical JSON ordering prevents a reordered identity list creating an empty
content change. The manifest contains no host paths, numeric SQL IDs or credentials.
The old first-slice repositories without a manifest are disposable experimental
stores; there is no implicit migration/fallback for that format.

`repository/git_projection_repository.py` explicitly registers a clone's project UUID
with a local root/name. Existing local names/roots or UUIDs bound elsewhere are not
silently adopted. `services/git_project_projection.py` restores exact `NoteContent`
under the manifest's note UUIDs, with short per-note SQL transactions and read-cache
invalidation. Repetition keeps the UUID and content version, while making missing
or interrupted file materialization retryable. The normal BM guarded materializer
then writes bytes; it preserves an unexpected editor file and reports conflict.

Existing SQL revisions use version CAS. An earlier absence uses a unique-key INSERT,
so a concurrently appearing content row cannot be overwritten by an initial-version
upsert. Existing UUID/path ownership and mirrored content identities are checked.
SQL bytes outside the target's published history are refused; private candidate
objects do not count as published history. The check traverses the selected published
history, not every object in the repository. See [git-rev-list](https://git-scm.com/docs/git-rev-list).

The projector accepts only the current published head and checks it again before
each per-note write. If Git advances it stops and requires a retry on the latest
head; it does not finish an older target over newer SQL. A partial pass can leave a
mixture of published revisions, which V1 permits. It does not acknowledge complete
project/index progress. Moving existing UUIDs, deleting extra local notes and raw
imports lacking canonical BM `title`/`type`/`permalink` need explicit reconciliation
and are refused by this slice.

The one-ref rule matters: Git documents atomic updates of individual refs while
concurrent readers can observe a subset of a multi-ref update. This implementation
does not depend on crash atomicity of multiple modified refs.
See [git-update-ref](https://git-scm.com/docs/git-update-ref).

The UUID here identifies a terminal publication. It is **not** an idempotency key
for an implicit API call, append operation, or draft update. General operation
deduplication remains V2. No-op receipts and retained private candidates are local
operational state; an ordinary branch push does not transfer them. Their retention,
backup and restore contract must be completed before a production rollout.

## Real BM integration experiment

`test-int/test_git_publication_integration.py` installs the publisher **only in its
test fixture**, using `NoteContentMutationService.on_accepted_mutation`. Real API
create/edit calls still run the existing preparer and accepted-note runner.

The hook receives canonical Markdown while the SQLite transaction is uncommitted.
A second SQL connection cannot read those new bytes. The hook publishes Git first;
SQLite then commits and the existing post-commit/materialization code proceeds.
Git failure rejects and rolls back the prepared SQLite mutation.

The experiment pauses the existing production materializer, not a replacement
index callback. The write and normal content read complete while files/vectors
are still outstanding. Releasing that worker produces identical file bytes,
observation/relation rows, full-text results, and vector manifests/physical vectors
for entity, observation and relation chunks. Embeddings use the repository's normal
deterministic test provider; this is a storage/pipeline proof, not a model benchmark.

An interruption after Git publication intentionally rolls back SQLite. The test
reopens Git, resolves the terminal transaction without a second publication, restores
the original UUID/content through the projector, and calls the existing recovery
materializer. A **full normal project index API** run rebuilds graph/full text and
vectors. The full index is explicit: file recovery alone can leave matching file
checksums while graph/search have never been built.

An independent `git clone --bare --no-local` is then registered in a second empty
SQLite database. Occupied numeric IDs force different local project/entity numbers.
The normal API still reads the original UUIDs and exact contents; relations resolve
to the new numeric target IDs, and physical vectors match the expected chunks. This
proves portable identity/content recovery in the isolated SQLite experiment. The
clone keeps the original canonical project naming/settings. Automatic lifecycle
recovery, configuration variation and production migration remain separate work.

Additional cases exercise successful API edits and rejection/SQL rollback when a
different commit advances the same project's Git basis. Core tests use actual Git
repositories, concurrent publishers, and child processes that terminate with
`os._exit()` immediately before/after completed publication calls. These process
tests do not kill the whole ASGI process mid-SQL commit, inject failures inside
Git's ref implementation, or prove power-loss/filesystem durability.

## Required before runtime wiring

The first slice is intentionally insufficient for a production switch. Continue
in this order:

1. Add durable per-project progress for content, graph/full text and vectors, plus
   a single lifecycle-owned worker with retry/startup catch-up. Recovery must run
   before accepting a new write based on stale SQL. Catch-up may skip older work
   where safe; reported progress must describe completed work honestly. Existing
   post-commit graph/full-text work is still synchronous in this experiment.
2. Extend guarded projection to moves/deletions and safe adoption/migration of
   existing project state. Verify indexing never rewrites canonical Git bytes to
   match local settings. Complete root binding/registration under runtime ownership.
3. Capture the Git basis before write preparation. Handle cancellation, SQLite
   commit failure after Git publication, concurrent processes and whole-process
   termination at that boundary. Cover move/delete and imports through the same
   publisher or reject them before acceptance. Do not leave a bypass enabled.
4. Add explicit transaction API/state, fixed-base and own-draft reads, status,
   keep-alive, 30-minute inactivity expiry and seven-day expired-draft retention.
   Expiry never commits. Terminal evidence must survive draft cleanup. Conflict
   transfer preserves old B/D, creates a new draft on C, explicitly carries B→D
   under B→C, rechecks dependencies, and commits normally again; never silently
   rebase or replay append. Preserve a protected source until transfer completes.
5. Add per-project opportunistic offline synchronization, explicit divergence
   handling, clone/restore tests and recovery of local operational state. A
   conflict in one project must not block unrelated projects.
6. Before the **first productive Git publication**, control every legacy writer:
   API/MCP, active/in-flight materializers, watcher/editor imports, Cloud imports,
   timers and manual sync. Accepted but unwritten content must be included. The
   agreed initial cutover suspends direct Cloud file sync for converted projects
   and routes editor drafts through controlled import. Index drainage is not a
   substitute for write-path control. Migration and rollback retain new Git history.

Do not enable the experimental hook on existing projects before these obligations
are implemented and the isolated boundary tests are extended accordingly.

## Validation

The isolated suite passes 70 cases; all four Git identity/publication/projection
modules have 100% executable-line coverage. A further 123 existing write/read/materialization/BM-002/BM-003 regression
cases pass. Changed-file Ruff/type checks and the isolated Doctor pass. Whole-tree
type checking still reports the same 56 diagnostics as the verified baseline; it
is not a clean full-tree type-check result.

Run from the checkout with its `src` on `PYTHONPATH` and `BASIC_MEMORY_ENV=test`:

```sh
python -m pytest tests/repository/test_git_publication.py \
  tests/schemas/test_git_project_manifest.py tests/services/test_git_project_projection.py \
  test-int/test_git_publication_integration.py -q --no-cov
ruff check src/basic_memory/{repository,services,schemas}/git*.py \
  tests/{repository,services,schemas}/test_git*.py test-int/test_git_publication_integration.py
ty check src/basic_memory/{repository,services,schemas}/git*.py \
  tests/{repository,services,schemas}/test_git*.py test-int/test_git_publication_integration.py
```

Use directory-based coverage (`--cov=src/basic_memory/repository`); an earlier
experiment with module-based coverage imported the services package too early and
tripped a Pydantic import-time assertion in the shared development environment.
Directory-based coverage completed successfully. This is a test-tool interaction,
not a production fix.
