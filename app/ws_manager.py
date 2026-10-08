import asyncio
import uuid

from fastapi import WebSocket
from sqlalchemy import event
from sqlalchemy.orm import Session


class ConnectionManager:
    """In-process WebSocket registry for live notification push.

    Single-process only: a push queued by a request handled on one uvicorn
    worker never reaches a socket connected to a different worker. Fine for
    today's single --reload worker; a multi-worker deployment would need a
    Redis (or similar) pub/sub layer feeding `push` from every worker.
    """

    def __init__(self) -> None:
        self._connections: dict[uuid.UUID, set[WebSocket]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(
        self, user_id: uuid.UUID, websocket: WebSocket, subprotocol: str | None = None
    ) -> None:
        await websocket.accept(subprotocol=subprotocol)
        self._connections.setdefault(user_id, set()).add(websocket)

    def disconnect(self, user_id: uuid.UUID, websocket: WebSocket) -> None:
        conns = self._connections.get(user_id)
        if not conns:
            return
        conns.discard(websocket)
        if not conns:
            self._connections.pop(user_id, None)

    async def _send(self, user_id: uuid.UUID, payload: dict) -> None:
        for ws in list(self._connections.get(user_id, ())):
            try:
                await ws.send_json(payload)
            except Exception:
                self.disconnect(user_id, ws)

    def push(self, user_id: uuid.UUID, payload: dict) -> None:
        """Best-effort, sync-callable (same philosophy as
        crud.create_audit_log/create_notification: never the reason a
        request fails). Schedules the actual send onto the bound event loop
        from whatever thread the caller is on -- this is invoked from the
        `after_commit` listener below, which fires inside FastAPI's
        threadpool for sync route handlers, not on the event loop itself."""
        if self._loop is None or user_id not in self._connections:
            return
        asyncio.run_coroutine_threadsafe(self._send(user_id, payload), self._loop)


    async def _close_user(self, user_id: uuid.UUID, code: int) -> None:
        for ws in list(self._connections.get(user_id, ())):
            try:
                await ws.close(code=code)
            except Exception:
                pass
            self.disconnect(user_id, ws)

    def close_user(self, user_id: uuid.UUID, code: int = 4401) -> None:
        """Closes every socket this user has open (their login was just
        deactivated -- the app treats 4401 like an expired session). Same
        sync-callable, best-effort contract as push."""
        if self._loop is None or user_id not in self._connections:
            return
        asyncio.run_coroutine_threadsafe(self._close_user(user_id, code), self._loop)


manager = ConnectionManager()


def queue_close(db: Session, user_id: uuid.UUID) -> None:
    """Closes the user's sockets once the current transaction commits
    (never on a rollback) -- see access_lifecycle.sync_login_access."""
    db.info.setdefault("_pending_ws_closes", []).append(user_id)


def queue_push(db: Session, user_id: uuid.UUID, payload: dict) -> None:
    """Queues a push for after the current transaction actually commits --
    crud.create_notification calls this right after db.flush(), before the
    caller's db.commit(), so a rollback never results in a phantom push."""
    db.info.setdefault("_pending_ws_pushes", []).append((user_id, payload))


@event.listens_for(Session, "after_commit")
def _flush_pending_pushes(session: Session) -> None:
    """Registered on the base Session class (same pattern as
    database.py's _stamp_audit_fields/_apply_tenant_search_path) so it
    fires for every session with no per-router wiring. Pops (not just
    reads) the queue because some request handlers -- e.g. leave.py's
    create/decide endpoints -- call db.commit() twice per request; the
    second commit's after_commit must not re-send what the first already
    flushed."""
    for user_id in session.info.pop("_pending_ws_closes", None) or ():
        manager.close_user(user_id)
    pending = session.info.pop("_pending_ws_pushes", None)
    if not pending:
        return
    for user_id, payload in pending:
        manager.push(user_id, payload)


@event.listens_for(Session, "after_transaction_end")
def _drop_pending_pushes(session: Session, transaction) -> None:
    """When the session's OUTERMOST transaction ends without committing
    (after_commit above already popped the queue on a commit), its pushes
    are discarded -- otherwise they stayed queued in session.info and went
    out with the NEXT commit on the same session: a phantom notification /
    shift_changed for a change that never happened. A nested savepoint
    (begin_nested) ending is ignored, so rolling one back inside a request
    keeps whatever was queued before it."""
    if transaction.parent is not None or transaction.nested:
        return
    session.info.pop("_pending_ws_pushes", None)
    session.info.pop("_pending_ws_closes", None)
