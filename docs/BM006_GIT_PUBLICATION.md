# BM-006: isolated Git publication, identity recovery and catch-up

Status: five isolated implementation slices, 2026-11-04. This is experimental code on
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
UUID. An explicit permutation of existing UUIDs supports path swaps; inventing a
new UUID at an occupied path or replacing a disappearing UUID still requires a
separate delete publication. Canonical JSON ordering prevents a reordered identity list creating an empty
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
objects do not count as published history. Adoption now proves the UUID, old path
and exact bytes belonged together in a published ancestor. The check traverses
the selected published history, not every object in the repository.
See [git-rev-list](https://git-scm.com/docs/git-rev-list).

The projector accepts only the current published head and checks it again before
each per-note write. If Git advances it stops and requires a retry on the latest
head; it does not finish an older target over newer SQL. A partial pass can leave a
mixture of published revisions, which V1 permits. It does not acknowledge complete
project/index progress. Proven moves/deletions are reconciled as described below.
Unproven local notes and raw imports lacking canonical BM `title`/`type`/`permalink`
still require controlled preparation and are refused.

## Guarded moves, deletions and old-file cleanup

`repository/git_layout_repository.py` records all local note identities/content in
an immutable basis. Target notes are parsed/validated before changing that layout.
A short SQL transaction compares the basis again, claims moved/deleted NoteContent
using version/checksum CAS, and records durable old-file retirement permissions.
No Git or file I/O runs inside that transaction. Moved notes keep their UUID and
numeric ID; advancing the content version fences older materialization requests.
Their destination file lineage is cleared so it cannot authorize overwriting an
unrelated destination. SQL-only temporary names permit path/permalink permutations;
none are committed or materialized. Deleted targets leave incoming relations unresolved.

`models/git_file_retirement.py` and migration `0044` store the old path, permitted
checksums and retirement attempt UUID independently of the Entity's lifecycle.
Permissions include proven accepted bytes and known materialized lineage. Repeated
moves merge older permissions; a stale acknowledgement cannot erase a newer one.
An inconsistent accepted checksum grants no cleanup permission.

The files stage retires old files before writing destinations, supporting path
reuse and swaps. It uses BM's existing checksum-guarded storage deletion. A missing
file is already retired; a reused path containing its exact current Git bytes is
preserved. Unexpected editor bytes keep the retirement and files progress pending.
The graph stage purges orphan full-text rows; the vector stage removes stale
manifest/physical vectors through the normal repository cleanup/reconciliation.
These guards do not replace the preproduction requirement to control all writers.

`test-int/test_git_layout_integration.py` covers moves, swaps, deletions through an
empty project, path reuse, coalesced moves and rapid round trips without intervening
file work. It verifies preserved identities, incoming unresolved relations, FTS and
physical vector cleanup. Child processes terminate after layout/file work and
immediately after file deletion but before retirement acknowledgement. Restart
repeats safely, including when an old file has disappeared or a new file now owns
its path. Editor conflicts remain visible and preserve the editor's bytes.

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
clone keeps the original canonical project naming/settings. Production lifecycle
wiring, configuration variation and production migration remain separate work.

Additional cases exercise successful API edits and rejection/SQL rollback when a
different commit advances the same project's Git basis. Core tests use actual Git
repositories, concurrent publishers, and child processes that terminate with
`os._exit()` immediately before/after completed publication calls. These process
tests do not kill the whole ASGI process mid-SQL commit, inject failures inside
Git's ref implementation, or prove power-loss/filesystem durability.

## Durable catch-up worker

`index/git_projection.py` explicitly composes the existing BM guarded materializer,
single-file graph/full-text indexer and `SearchService.sync_entity_vectors`. It
does not enable them in existing API/MCP/startup dependencies. Indexing uses a
writer that **rejects frontmatter rewrites** before touching the published file.
Unsupported local normalization settings therefore leave graph progress pending
instead of changing Git's bytes. Safe automatic adaptation remains future work.

`models/git_projection_progress.py`, migration `0043` and
`repository/git_progress_repository.py` persist one local row per project and stage:

| Stage | Completion means |
| --- | --- |
| `content` | Exact current Git Markdown and portable UUIDs have reached SQL. |
| `files` | Every note passed BM's guarded materializer and exact byte verification. |
| `graph` | Every note explicitly ran graph/full-text indexing, including unchanged files. |
| `vectors` | Every note completed vector synchronization or its explicit note opt-out. |

Rows retain target SHA, last completely processed SHA, running SHA/attempt UUID,
attempt count and the most recent error. Successful file recovery never implies
graph or vector completion. Missing semantic infrastructure and failed/deferred
vector results stay pending; they do not change a requested search mode. BM's
unchanged-vector outcome can count as both synced and skipped, which is accepted
without double-counting it as a failure. A vector configuration key invalidates
only the vector checkpoint when model/index/dimensions/token policy changes. Its
version must be bumped when the derived chunk/index policy changes in code.

`index/git_projection_worker.py` provides an explicit `async with worker.run()`
lifecycle. It owns two serial tasks: content → files → graph, and vectors after
graph completion. A slow embedding call cannot block newer content/file/graph
catch-up. Each lane retries failures with bounded exponential backoff (poll
interval × up to 64); publication notifications only reduce latency. Polling Git
and SQL on startup and subsequent passes recovers even completely lost wakeups.
There is no per-write coroutine backlog and no new daemon. Local worker ownership
is now enforced by `index/git_project_ownership.py` before either task starts.

Work samples the latest published head and may coalesce A/B into C. Head changes
stop an old pass; attempt/target guards reject late acknowledgements. The worker
records completion only after work returns. Cancellation/process death before that
point leaves a durable running marker; a new owner repeats the stage safely.
Context exit cancels and awaits both tasks. SQL transactions do not span embedding
or Git/file I/O.

`worker.status()` reports the current source SHA plus each stage's distinct target,
completed and running SHA/error. `caught_up` also checks the active vector policy.
These are **historical whole-stage completion checkpoints**, not a promise that
all rows remain on that revision during a newer partial pass. Ordinary V1 readers
can still mix published revisions; private drafts are not inputs. No general V2
consistent read endpoint or public progress API is introduced here.

`test-int/test_git_projection_worker_integration.py` proves real API content reads,
graph relations, full-text results and physical vectors without a manual full-index
call. It tests A/B/C advancement during blocked vectors, independent vector failure,
configuration invalidation, preserved editor bytes, cancellation before/after each
stage and a fresh child interpreter terminating with `os._exit()` after each stage's
work but before checkpoint acknowledgement. Restart uses the same durable SQLite
and Git store. Both interpreters use the same deterministic test embedding methods
and provider identity; this is not an embedding benchmark or power-loss proof.

## Local root binding and exclusive ownership

The worker validates the project UUID/root against SQL and the Git manifest, and
rejects overlapping registered project roots or a bare repository inside an indexed
root. `filelock` provides two nonblocking OS ownership locks: one at the note root
and one at the bare repository. A second object or process cannot run the same
projection; another project can proceed independently. These are local filesystem
ownership locks, not long-held Git/SQL locks for an agent transaction.

Local-only binding metadata lives at `<root>/.basic-memory/git-binding.json` and
`<bare-repo>/bm-local-binding.json`; neither belongs to the published tree or an
ordinary Git clone/push. The paired markers bind project UUID, canonical root and
repository path. Both are checked before creating either. Interrupted first creation
can repair a missing matching marker; differing bindings require controlled remapping.
Control metadata symlinks are refused. SQL registration is rechecked for status,
each worker pass and each stage, so a live root change does not silently continue.

Context exit waits for the worker tasks and releases the locks. Process death also
releases the tested local OS locks; lock files remain and must not be unlinked by
cleanup. The verified environment is local Linux/SQLite. Other filesystem/platform
semantics and controlled root remapping still need their own operational validation.
This ownership does not prevent a legacy API, watcher or Cloud importer from writing;
controlling those sources remains mandatory before production enablement.

## Required before runtime wiring

The first slice is intentionally insufficient for a production switch. Continue
in this order:

1. Complete controlled first import/migration and host integration. The isolated
   adoption primitive below preserves accepted-but-unwritten content, but does not
   stop legacy writers. Unindexed files, resources and noncanonical input remain
   protected. Define controlled root remapping. Index rewrites are refused;
   automatic normalization adaptation is not implemented. Measure the
   current whole-stage rebuild/catch-up cost before enabling it for real projects.
2. Select the opt-in executor in controlled API/MCP lifespans/dependencies, expose
   honest public progress and reject remaining import/index/directory bypasses.
   Startup content recovery and the post-commit worker are isolated below; default
   dependencies still do not enable them. Core runners prepare typed graph data and
   a pending-generation marker before commit; graph/FTS publication is already
   deferred on this path. Review the remaining synchronous delete cleanup separately.
3. Preserve the captured Git basis before preparation and its verified cancellation,
   SQLite failure and whole-ASGI-process recovery behavior when wiring the host.
   Create/update/edit/move/delete are covered by the isolated executor; imports and
   bulk/directory operations must use the publisher or be refused before acceptance.
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

## Reviewable first adoption

`services/git_project_adoption.py` provides `prepare_git_adoption()` and
`publish_git_adoption()`. They require externally stopped/drained legacy writers,
including accepted in-flight mutations, materializers, editor and Cloud imports.
The existing root/repository ownership excludes other Git workers; it does not
prove that external precondition or perform a productive cutover.

Preparation requires an unused initialization commit and preserves project/note
UUIDs and exact bytes in local `bm-adoption.json`. Accepted NoteContent takes
precedence over an older or missing materialized file. Old disk bytes and materialization
lineage are retained as migration evidence. File-first notes require the indexed
file checksum. Unknown editor bytes, unindexed files, symlinks, unreadable directories,
non-Markdown resources/entities and noncanonical frontmatter are refused before
publication. Unknown control files are not silently excluded. The reserved root
control directory contains only the owned Git binding/lock files during adoption.

The retained plan is not silently refreshed. Publication revalidates SQL/file state,
root registration and the fixed Git basis, then publishes through the same CAS.
Retry resolves its terminal transaction before comparing the old migration basis:
normal projection may already have advanced SQL/materialized files after a lost
answer. The plan is local operational evidence containing private content; retain
it with backups. An ordinary Git clone or branch push does not carry it.

`settle_git_publication()` in the Git repository module waits for publication to
settle even if the caller is cancelled repeatedly. Ownership/admission cannot be
released while a detached thread can still change the published branch. Cancellation
still reaches the caller; durable Git determines subsequent recovery.

## Opt-in startup and real API acceptance

`services/git_project_runtime.py` implements the optional mutation executor accepted
by `NoteContentMutationService`. Default dependencies do not select it. Its lifecycle
owns the existing Git worker and first restores SQL content before yielding admission;
it does not wait for files, graph or vectors. The exact restored startup commit is
retained, rather than incorrectly sampling a potentially newer head afterward.

Each implicit mutation acquires a short local admission gate, captures immutable Git
before calling the real BM preparer/runner, and verifies that SQL contains that
published basis. A newer peer publication first causes content recovery. Private SQL
bytes/checksums/identities cannot become the basis of a new publication. The worker
shares the gate only for SQL content projection; vectors never hold it. Explicit
agent planning must not run inside this implicit mutation gate.

Create, update, append/edit, move and delete prepare through the normal BM core.
Git publication precedes SQL commit; old-file permissions survive successful SQL
move/delete even after the old entity is gone. The returned change suppresses legacy
materialization/cleanup and the service skips its legacy post-commit graph/FTS work.
The lifecycle worker performs files/graph/FTS/vectors. Canonical normalization and
validation, typed graph preparation and a pending-generation marker remain inside
acceptance; they do not publish observation/relation/FTS rows. The blocked-worker
API test verifies those rows and vector manifests are still absent at response time.
Existing delete runners still remove search/vector state in their SQL transaction;
review that cleanup separately before productive host integration.

A changed Git basis returns a distinct HTTP 409 publication conflict, including base
and actual source commits. It must not be converted by `write_note()` into an
`already_exists` path outcome. A failed SQL commit after successful Git publication
reports HTTP 503 with `published_commit` and an instruction to inspect status before
retrying; it never claims the Git write was rolled back. This is not V2 operation
deduplication. Cancellation/process loss recovers from Git without replaying append.

Both Git content/layout projection and this acceptance path use BM's existing
`db.scoped_session` for writes. SQLite foreign-key enforcement is connection-local;
a raw fresh session can otherwise leave an incoming relation pointing at a deleted
target. A fresh-connection regression verifies preserved links with `to_id=NULL`.
This adds no row locks or wider derived-state transaction.

`test-int/test_git_runtime_integration.py` exercises the real API with an explicitly
injected executor, blocked graph/vector work, concurrent writes, peer publication
during preparation, SQL commit failure, shutdown and repeated cancellation.
`git_runtime_crash.py` terminates the entire in-process ASGI interpreter before/after
Git publication and before/after SQLite's commit call. Restart uses the real worker
against the same Git/SQLite state. Background loops are paused only in the dying
child to target the foreground commit boundary. This is not a TCP server or a
power-loss proof. All ordinary tests use deterministic embeddings.

## Validation

The isolated suite passes 204 cases; all 15 Git publication/projection/worker/
adoption/runtime modules (including both migrations) have 1115/1115 executable
lines covered. One statically unreachable exhaustive-match guard is excluded.
A further 132 existing write/read/materialization/authorship/BM-002/BM-003 regression
cases pass. Changed-file Ruff/type checks and the isolated Doctor pass. Whole-tree
type checking still reports the same 56 diagnostics as the verified baseline; it
is not a clean full-tree type-check result.

Run from the checkout with its `src` on `PYTHONPATH` and `BASIC_MEMORY_ENV=test`:

```sh
python -m pytest tests/repository/test_git_publication.py \
  tests/schemas/test_git_project_manifest.py tests/services/test_git_project_projection.py \
  tests/repository/test_git_progress_repository.py tests/test_git_projection_progress_migration.py \
  tests/test_migration_graph.py test-int/test_git_publication_integration.py \
  tests/repository/test_git_layout_repository.py tests/repository/test_git_cleanup_permissions.py \
  tests/index/test_git_project_ownership.py \
  tests/test_git_file_retirement_migration.py test-int/test_git_layout_integration.py \
  test-int/test_git_projection_worker_integration.py test-int/test_git_adoption_integration.py \
  test-int/test_git_runtime_integration.py -q --no-cov
ruff check src/basic_memory/{repository,services,schemas,index,models}/git*.py \
  src/basic_memory/alembic/versions/0043_add_git_projection_progress.py \
  src/basic_memory/alembic/versions/0044_add_git_file_retirement.py \
  tests/{repository,services,schemas,index}/test_git*.py tests/test_git_projection_progress_migration.py \
  tests/test_git_file_retirement_migration.py \
  src/basic_memory/services/note_content_writes.py \
  test-int/test_git*.py test-int/fixtures/git*crash.py
ty check src/basic_memory/{repository,services,schemas,index,models}/git*.py \
  src/basic_memory/alembic/versions/0043_add_git_projection_progress.py \
  src/basic_memory/alembic/versions/0044_add_git_file_retirement.py \
  tests/{repository,services,schemas,index}/test_git*.py tests/test_git_projection_progress_migration.py \
  tests/test_git_file_retirement_migration.py \
  src/basic_memory/services/note_content_writes.py \
  test-int/test_git*.py test-int/fixtures/git*crash.py
```

Use directory-based coverage (`--cov=src/basic_memory/repository`); an earlier
experiment with module-based coverage imported the services package too early and
tripped a Pydantic import-time assertion in the shared development environment.
Directory-based coverage completed successfully. This is a test-tool interaction,
not a production fix.
