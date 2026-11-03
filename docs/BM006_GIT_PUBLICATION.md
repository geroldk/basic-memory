# BM-006: isolated Git publication proof

Status: first implementation slice, 2026-11-03. This is experimental code on
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
existing directory. An initial empty commit establishes the project basis.

1. `prepare(transaction_id, base_commit, files)` builds a full Markdown tree using
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
reopens Git, resolves the terminal transaction without a second publication, writes
only the published snapshot to the isolated project, and uses the **normal project
index API** to rebuild content, graph and vectors. This demonstrates content
recoverability. It does not install startup recovery, and it does not yet preserve
the prepared note's portable external UUID after a complete SQL rollback.

Additional cases exercise successful API edits and rejection/SQL rollback when a
different commit advances the same project's Git basis. Core tests use actual Git
repositories, concurrent publishers, and child processes that terminate with
`os._exit()` immediately before/after completed publication calls. These process
tests do not kill the whole ASGI process mid-SQL commit, inject failures inside
Git's ref implementation, or prove power-loss/filesystem durability.

## Required before runtime wiring

The first slice is intentionally insufficient for a production switch. Continue
in this order:

1. Add portable project/note UUID metadata committed with the Markdown tree and a
   projector that restores exactly those identities and canonical bytes. A rebuild
   must keep external note IDs stable after a post-publication SQL failure and in
   an independent clone. Paths/titles/numeric SQLite IDs cannot be the identity.
2. Add durable per-project progress for content, graph/full text and vectors, plus
   a single lifecycle-owned worker with retry/startup catch-up. Recovery must run
   before accepting a new write based on stale SQL. Catch-up may skip older work
   where safe; reported progress must describe completed work honestly. Existing
   post-commit graph/full-text work is still synchronous in this experiment.
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

The isolated suite passes 34 cases; the new Git module has 134/134 executable lines
covered. A further 123 existing write/read/materialization/BM-002/BM-003 regression
cases pass. Changed-file Ruff/type checks and the isolated Doctor pass. Whole-tree
type checking still reports the same 56 diagnostics as the verified baseline; it
is not a clean full-tree type-check result.

Run from the checkout with its `src` on `PYTHONPATH` and `BASIC_MEMORY_ENV=test`:

```sh
python -m pytest tests/repository/test_git_publication.py \
  test-int/test_git_publication_integration.py -q --no-cov
ruff check src/basic_memory/repository/git_project_repository.py \
  tests/repository/test_git_publication.py test-int/test_git_publication_integration.py
ty check src/basic_memory/repository/git_project_repository.py \
  tests/repository/test_git_publication.py test-int/test_git_publication_integration.py
```

Use directory-based coverage (`--cov=src/basic_memory/repository`); an earlier
experiment with module-based coverage imported the services package too early and
tripped a Pydantic import-time assertion in the shared development environment.
Directory-based coverage completed successfully. This is a test-tool interaction,
not a production fix.
