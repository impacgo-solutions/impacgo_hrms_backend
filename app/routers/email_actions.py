"""Public pages behind the Approve / Reject buttons in approval emails
(see app/email_actions.py for the token, security notes and rules).

GET  /api/email-actions/{token}  -> confirmation page (never changes data)
POST /api/email-actions/{token}  -> applies the decision via the same
                                    endpoint function the app uses
"""

from __future__ import annotations

import html

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import HTMLResponse

from .. import email_actions as ea
from .. import email_service, schemas
from . import leave as leave_router

router = APIRouter(prefix="/api/email-actions", tags=["email-actions"])

_DECIDABLE = ("pending", "l1_approved")


def _e(v) -> str:
    return html.escape("" if v is None else str(v))


def _page(title: str, body: str, company: str = "", status: int = 200) -> HTMLResponse:
    return HTMLResponse(status_code=status, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"},
                        content=f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<meta name="referrer" content="no-referrer"><title>{_e(title)}</title><style>
*{{box-sizing:border-box}} body{{margin:0;background:#f1f5f9;font-family:'Segoe UI',Roboto,Arial,sans-serif;color:#0f172a}}
.wrap{{max-width:560px;margin:0 auto;padding:28px 16px}} .brand{{color:#64748b;font-size:13px;font-weight:600;margin-bottom:12px}}
.card{{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:24px;box-shadow:0 1px 2px rgba(15,23,42,.04)}}
h1{{font-size:20px;margin:0 0 6px}} .muted{{color:#64748b;font-size:13.5px;line-height:1.55}}
table{{width:100%;border-collapse:collapse;margin:16px 0 4px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px}}
td{{padding:9px 14px;font-size:14px;vertical-align:top;border-bottom:1px solid #eef2f7}} tr:last-child td{{border-bottom:0}}
td.k{{color:#64748b;width:38%;font-weight:600;font-size:13px}}
label{{display:block;font-size:13px;font-weight:600;color:#334155;margin:16px 0 6px}}
textarea{{width:100%;min-height:90px;padding:10px;border:1px solid #cbd5e1;border-radius:8px;font:inherit;font-size:14px;resize:vertical}}
.btn{{display:inline-block;border:0;border-radius:8px;padding:12px 22px;font-size:14.5px;font-weight:700;cursor:pointer;text-decoration:none}}
.approve{{background:#16a34a;color:#fff}} .reject{{background:#dc2626;color:#fff}} .ghost{{background:#fff;color:#2564cf;border:1px solid #bfdbfe}}
.row{{display:flex;gap:10px;flex-wrap:wrap;margin-top:20px}} .row .btn{{flex:1 1 180px;text-align:center}}
.icon{{width:52px;height:52px;border-radius:26px;display:flex;align-items:center;justify-content:center;font-size:26px;margin-bottom:12px}}
.ok{{background:#dcfce7;color:#15803d}} .bad{{background:#fee2e2;color:#b91c1c}} .info{{background:#e0e7ff;color:#3730a3}} .warn{{background:#fef3c7;color:#b45309}}
</style></head><body><div class="wrap"><div class="brand">{_e(company) or 'Impacgo HRMS'}</div><div class="card">{body}</div>
<div class="muted" style="text-align:center;margin-top:16px;font-size:12px">Secure approval link &middot; Impacgo HRMS</div></div></body></html>""")


def _message(title: str, text: str, *, kind: str = "info", company: str = "", status: int = 200) -> HTMLResponse:
    icon = {"ok": "&#10003;", "bad": "&#10005;", "warn": "!", "info": "i"}[kind]
    open_link = f'<div class="row"><a class="btn ghost" href="{_e(email_service.hrms_url())}">Open Impacgo HRMS</a></div>'
    return _page(title, f'<div class="icon {kind}">{icon}</div><h1>{_e(title)}</h1>'
                        f'<p class="muted">{_e(text)}</p>{open_link}', company, status)


def _summary_table(rows: list[tuple[str, str]]) -> str:
    return "<table>" + "".join(f'<tr><td class="k">{_e(k)}</td><td>{_e(v)}</td></tr>' for k, v in rows) + "</table>"


def _status_words(status: str) -> str:
    return {"approved": "approved", "rejected": "rejected", "cancelled": "cancelled by the employee",
            "withdrawn": "withdrawn", "sent_back": "sent back for revision"}.get(status, status.replace("_", " "))


@router.get("/{token}", response_class=HTMLResponse)
def confirm_page(token: str):
    ctx = ea.ActionContext(token)
    try:
        if ctx.error:
            return _message("Link can't be used", ctx.error, kind="warn", company=ctx.company_name, status=404)
        leave = ctx.leave
        if leave.status not in _DECIDABLE:
            return _message("Already handled", f"This leave request was already {_status_words(leave.status)}. "
                            "Nothing more is needed.", kind="info", company=ctx.company_name)
        approve = ctx.action == "approve"
        rows = ea.leave_summary(ctx.db, leave)
        employee = rows[0][1]
        if approve:
            form = ('<label for="n">Comment (optional)</label>'
                    '<textarea id="n" name="notes" maxlength="2000" placeholder="Add a note for the employee"></textarea>'
                    '<div class="row"><button class="btn approve" type="submit">&#10003;&nbsp; Confirm approval</button></div>')
        else:
            form = ('<label for="n">Reason for rejection (required)</label>'
                    '<textarea id="n" name="notes" maxlength="2000" required placeholder="Let the employee know why"></textarea>'
                    '<div class="row"><button class="btn reject" type="submit">&#10005;&nbsp; Confirm rejection</button></div>')
        body = (f'<div class="icon {"ok" if approve else "bad"}">{"&#10003;" if approve else "&#10005;"}</div>'
                f'<h1>{"Approve" if approve else "Reject"} this leave request?</h1>'
                f'<p class="muted">Review the details, then confirm. You are acting as '
                f'<strong>{_e(ctx.user.full_name or ctx.user.email)}</strong>.</p>'
                f'{_summary_table(rows)}'
                f'<form method="post" autocomplete="off">{form}</form>'
                f'<p class="muted" style="margin-top:14px;font-size:12.5px">Prefer the app? '
                f'<a href="{_e(email_service.hrms_url())}">Open Impacgo HRMS</a>.</p>')
        return _page(f"{'Approve' if approve else 'Reject'} leave — {employee}", body, ctx.company_name)
    finally:
        ctx.close()


@router.post("/{token}", response_class=HTMLResponse)
def apply_decision(token: str, notes: str = Form("")):
    ctx = ea.ActionContext(token)
    try:
        if ctx.error:
            return _message("Link can't be used", ctx.error, kind="warn", company=ctx.company_name, status=404)
        approve = ctx.action == "approve"
        notes = (notes or "").strip()[:2000] or None
        if not approve and not notes:
            return _message("Reason needed", "Please go back and enter a reason for the rejection.",
                            kind="warn", company=ctx.company_name, status=400)
        if ctx.leave.status not in _DECIDABLE:
            return _message("Already handled", f"This leave request was already {_status_words(ctx.leave.status)}. "
                            "Nothing more is needed.", kind="info", company=ctx.company_name)
        try:
            out = leave_router.update_leave_request(
                ctx.leave.id,
                schemas.LeaveRequestUpdate(status=ea.ACTIONS[ctx.action], decision_notes=notes),
                db=ctx.db,
                current_user=ctx.user,
            )
        except HTTPException as exc:
            ctx.db.rollback()
            return _message("Couldn't record your decision", str(exc.detail), kind="warn",
                            company=ctx.company_name, status=exc.status_code)
        final = getattr(out, "status", None) or ctx.leave.status
        employee = ea.leave_summary(ctx.db, ctx.leave)[0][1]
        if final == "approved":
            return _message("Leave approved", f"{employee}'s leave is approved. They have been notified.",
                            kind="ok", company=ctx.company_name)
        if final == "rejected":
            return _message("Leave rejected", f"{employee}'s leave is rejected. They have been notified with your reason.",
                            kind="bad", company=ctx.company_name)
        return _message("Approval recorded", f"Your approval of {employee}'s leave is recorded. "
                        "It now moves to the next approver.", kind="ok", company=ctx.company_name)
    finally:
        ctx.close()
