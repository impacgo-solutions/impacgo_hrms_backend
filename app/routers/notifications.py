import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from starlette.concurrency import run_in_threadpool

from .. import crud, database, deps, models, schemas, security, ws_manager
from ..config import settings
from ..database import get_db
from ..deps import get_current_user

router = APIRouter(prefix="/api", tags=["notifications"])


@router.websocket("/notifications/ws")
async def notifications_ws(websocket: WebSocket, token: str | None = Query(None)):
    """Live push companion to GET /notifications -- every Notification row
    created via crud.create_notification (leave/work/attendance/payroll/
    travel/assets approval flows) is queued for push here by
    ws_manager.queue_push, right after the request that created it commits.

    Auth (AUTH-A6): a browser WebSocket can't set an Authorization header,
    so the client offers the subprotocols ["bearer", "<jwt>"] and the server
    accepts "bearer" -- the token stays out of the URL and access logs. The
    legacy `?token=` query param is still accepted for older app builds
    (redacted from logs). Same checks as every HTTP request
    (deps.authenticate_token: revocation, deactivation, tenant status).
    On failure the socket is accepted and then closed with 4401, so the
    client can actually see the code (a close before accept surfaces only as
    a generic handshake failure) and stop reconnecting / log out."""
    raw_token, subprotocol = deps.websocket_token(websocket.scope.get("subprotocols", []), token)
    user_id = await run_in_threadpool(deps.authenticate_websocket_user, raw_token)
    if user_id is None:
        await websocket.accept(subprotocol=subprotocol)
        await websocket.close(code=4401)
        return

    await ws_manager.manager.connect(user_id, websocket, subprotocol=subprotocol)
    try:
        while True:
            await websocket.receive_text()  # keepalive only; content ignored
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.manager.disconnect(user_id, websocket)


@router.get("/notifications", response_model=list[schemas.NotificationOut])
def list_notifications(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    # Check-on-read hook (no background scheduler in this app) -- same
    # pattern as check_overdue_milestones/GET /releases. This is the one
    # endpoint every login/session always hits, so Birthday/Work
    # Anniversary reminders reliably fire the same day they're due.
    # PERF-10: a no-op (no queries/DML/commit) after the first call of the
    # day; commits only when a celebration may actually have been written.
    # With the daily reminder scheduler on (app/reminders.py), it sends them
    # at REMINDER_SEND_HOUR instead -- this request never waits on a
    # company-wide broadcast.
    if not settings.reminders_enabled and crud.check_daily_celebrations(db, current_user.company_id):
        db.commit()
    return crud.list_notifications(db, current_user.id)


@router.get("/notifications/unread-count")
def unread_notification_count(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    return {"count": crud.count_unread_notifications(db, current_user.id)}


@router.patch("/notifications/{notification_id}/read", status_code=204)
def mark_notification_read(
    notification_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    found = crud.mark_notification_read(db, notification_id, current_user.id)
    if not found:
        raise HTTPException(status_code=404, detail="Notification not found")
    db.commit()


@router.post("/notifications/read-all", status_code=204)
def mark_all_notifications_read(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    crud.mark_all_notifications_read(db, current_user.id)
    db.commit()
