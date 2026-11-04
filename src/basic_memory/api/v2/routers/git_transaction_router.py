"""Capability-owned explicit transactions for the opt-in local Git host."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response

from basic_memory.deps.git import GitRuntimeDep
from basic_memory.schemas.git_transactions import (
    GitTransactionCommit,
    GitTransactionControl,
    GitTransactionDelete,
    GitTransactionListing,
    GitTransactionMove,
    GitTransactionRead,
    GitTransactionStart,
    GitTransactionStarted,
    GitTransactionStatus,
    GitTransactionWrite,
)
from basic_memory.services.git_transactions import GitTransactionService


async def private_transaction_response(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(
    prefix="/git/transactions",
    tags=["git-transactions"],
    dependencies=[Depends(private_transaction_response)],
)
type TransactionKey = Annotated[str, Header(alias="X-BM-Transaction-Key")]


async def get_git_transactions(runtime: GitRuntimeDep) -> GitTransactionService:
    if runtime is None:
        raise HTTPException(404, "Explicit transactions require a local Git host")
    return runtime.transactions


type TransactionsDep = Annotated[GitTransactionService, Depends(get_git_transactions)]


@router.post("", response_model=GitTransactionStarted)
async def start_transaction(
    request: GitTransactionStart, transactions: TransactionsDep
) -> GitTransactionStarted:
    return await transactions.start(request)


@router.get("/{transaction_id}", response_model=GitTransactionStatus)
async def transaction_status(
    transaction_id: UUID, transactions: TransactionsDep, transaction_key: TransactionKey
) -> GitTransactionStatus:
    return await transactions.status(transaction_id, transaction_key)


@router.get("/{transaction_id}/notes", response_model=GitTransactionListing)
async def list_transaction_notes(
    transaction_id: UUID,
    transactions: TransactionsDep,
    transaction_key: TransactionKey,
    view: Literal["base", "draft"] = "draft",
) -> GitTransactionListing:
    return await transactions.listing(transaction_id, transaction_key, view=view)


@router.get("/{transaction_id}/note", response_model=GitTransactionRead)
async def read_transaction_note(
    transaction_id: UUID,
    transactions: TransactionsDep,
    transaction_key: TransactionKey,
    identifier: str,
    view: Literal["base", "draft"] = "draft",
) -> GitTransactionRead:
    return await transactions.read(transaction_id, transaction_key, identifier, view=view)


@router.post("/{transaction_id}/write", response_model=GitTransactionStatus)
async def write_transaction_note(
    transaction_id: UUID,
    request: GitTransactionWrite,
    transactions: TransactionsDep,
    transaction_key: TransactionKey,
) -> GitTransactionStatus:
    return await transactions.write(transaction_id, transaction_key, request)


@router.post("/{transaction_id}/move", response_model=GitTransactionStatus)
async def move_transaction_note(
    transaction_id: UUID,
    request: GitTransactionMove,
    transactions: TransactionsDep,
    transaction_key: TransactionKey,
) -> GitTransactionStatus:
    return await transactions.write(transaction_id, transaction_key, request)


@router.post("/{transaction_id}/delete", response_model=GitTransactionStatus)
async def delete_transaction_note(
    transaction_id: UUID,
    request: GitTransactionDelete,
    transactions: TransactionsDep,
    transaction_key: TransactionKey,
) -> GitTransactionStatus:
    return await transactions.write(transaction_id, transaction_key, request)


@router.post("/{transaction_id}/keep-alive", response_model=GitTransactionStatus)
async def keep_alive_transaction(
    transaction_id: UUID, transactions: TransactionsDep, transaction_key: TransactionKey
) -> GitTransactionStatus:
    return await transactions.status(transaction_id, transaction_key, keep_alive=True)


@router.post("/{transaction_id}/commit", response_model=GitTransactionStatus)
async def commit_transaction(
    transaction_id: UUID,
    request: GitTransactionCommit,
    transactions: TransactionsDep,
    transaction_key: TransactionKey,
) -> GitTransactionStatus:
    return await transactions.commit(transaction_id, transaction_key, request)


@router.post("/{transaction_id}/rollback", response_model=GitTransactionStatus)
async def rollback_transaction(
    transaction_id: UUID,
    request: GitTransactionControl,
    transactions: TransactionsDep,
    transaction_key: TransactionKey,
) -> GitTransactionStatus:
    return await transactions.rollback(transaction_id, transaction_key, request)
