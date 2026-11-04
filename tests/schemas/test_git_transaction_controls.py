"""API controls must not escape paths, forge evidence or half-bind a source."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from basic_memory.schemas.git_transactions import (
    GitTransactionCommit,
    GitTransactionDelete,
    GitTransactionMove,
    GitTransactionStart,
    GitTransactionWrite,
)


@pytest.mark.parametrize(
    "data",
    [
        {"source_transaction_id": uuid4()},
        {"source_key": "secret"},
        {"source_revision": 0},
    ],
)
def test_transfer_source_needs_all_three_controls(data):
    with pytest.raises(ValidationError, match="together"):
        GitTransactionStart(**data)


@pytest.mark.parametrize("message", [" ", "x\0y", "Oops\n\nBasic-Memory-Transaction: forged"])
def test_commit_message_cannot_forge_a_different_terminal_transaction(message):
    with pytest.raises(ValidationError):
        GitTransactionCommit(expected_revision=0, message=message)


@pytest.mark.parametrize("path", ["../x.md", ".git/x.md", ".basic-memory/x.md", "/absolute.md"])
def test_all_draft_mutation_paths_are_validated(path):
    with pytest.raises(ValidationError):
        GitTransactionWrite(expected_revision=0, path=path, content="not accepted")
    with pytest.raises(ValidationError):
        GitTransactionDelete(expected_revision=0, path=path)
    with pytest.raises(ValidationError):
        GitTransactionMove(expected_revision=0, path="safe.md", destination=path)
