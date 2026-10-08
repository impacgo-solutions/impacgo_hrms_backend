"""Candidate self-service pages (no HRMS login): the secure link emailed
with an offer (view / download / accept / decline) and with preboarding
tasks (joining details, document uploads, acknowledgements).

The link is /api/candidate-portal/{tenant}/{token}: the token is 256 bits
of randomness, only its SHA-256 is stored (hcm_candidate_portal_links),
it is bound to one record + purpose, expires, and is revoked once the
candidate has responded / the record closes. Every action goes through the
same recruitment_onboarding functions HR uses, so every rule, status and
history entry is identical."""

from __future__ import annotations

import datetime
import html
import re
import urllib.parse

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from .. import crud, database, models
from .. import recruitment_onboarding as onb
from .. import recruitment_workflow as rw

router = APIRouter(prefix="/api/candidate-portal", tags=["candidate-portal"])

_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{1,60}$")


class _Portal:
    def __init__(self, slug: str, token: str):
        if not _SLUG_RE.match(slug) or slug.startswith("_"):
            raise HTTPException(status_code=404, detail="This link is not valid.")
        self.slug, self.token = slug, token
        self.db = database.SessionLocal()
        tenant = self.db.scalar(select(models.Tenant).where(models.Tenant.slug == slug, models.Tenant.is_active.is_(True)))
        if tenant is None:
            self.db.close()
            raise HTTPException(status_code=404, detail="This link is not valid.")
        database.set_session_tenant_slug(self.db, slug)
        database.ensure_tenant_search_path(self.db)
        self.link = onb.resolve_portal_token(self.db, token)
        self.ctx = None
        if self.link is not None:
            self.ctx = rw.system_ctx(self.db, self.link.company_id, "Candidate")

    def close(self):
        self.db.close()

    @property
    def base(self) -> str:
        return f"/api/candidate-portal/{self.slug}/{self.token}"


def _page(title: str, body: str, company: str = "", status: int = 200) -> HTMLResponse:
    return HTMLResponse(status_code=status, content=f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>{html.escape(title)}</title><style>
body{{margin:0;background:#f1f5f9;font-family:Segoe UI,Roboto,Arial,sans-serif;color:#0f172a}}
.wrap{{max-width:760px;margin:0 auto;padding:24px 16px}} .card{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:20px;margin-bottom:16px}}
h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:16px;margin:0 0 12px}} .muted{{color:#64748b;font-size:13px}}
table{{width:100%;border-collapse:collapse}} td{{padding:6px 0;font-size:14px;vertical-align:top}} td.k{{color:#64748b;width:42%}}
label{{display:block;font-size:13px;color:#334155;margin:10px 0 4px}} input,select,textarea{{width:100%;box-sizing:border-box;padding:8px;border:1px solid #cbd5e1;border-radius:6px;font-size:14px}}
.btn{{display:inline-block;border:0;border-radius:6px;padding:10px 20px;font-size:14px;font-weight:600;cursor:pointer;text-decoration:none}}
.primary{{background:#2564cf;color:#fff}} .danger{{background:#fff;color:#b91c1c;border:1px solid #fca5a5}} .ghost{{background:#fff;color:#2564cf;border:1px solid #bfdbfe}}
.row{{display:flex;gap:10px;flex-wrap:wrap;align-items:center}} .grid{{display:grid;grid-template-columns:1fr 1fr;gap:0 12px}}
@media (max-width:560px){{.grid{{grid-template-columns:1fr}}}}
.badge{{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;font-weight:600;background:#e2e8f0}}
.ok{{background:#dcfce7;color:#166534}} .warn{{background:#fef3c7;color:#92400e}} .bad{{background:#fee2e2;color:#991b1b}}
.note{{padding:10px 12px;border-radius:6px;background:#eff6ff;color:#1e3a8a;font-size:14px;margin-bottom:12px}}
.task{{border-top:1px solid #e2e8f0;padding:12px 0}}
</style></head><body><div class="wrap"><div class="muted" style="margin-bottom:10px">{html.escape(company)}</div>{body}
<div class="muted" style="text-align:center;margin-top:18px">Secure candidate link · Impacgo HRMS</div></div></body></html>""")


def _invalid() -> HTMLResponse:
    return _page("Link not valid", '<div class="card"><h1>This link is no longer valid</h1><p class="muted">'
                 "It may have expired or been replaced by a newer link, or your response was already recorded. "
                 "Please contact the HR team if you need help.</p></div>", status=404)


def _e(v) -> str:
    return html.escape("" if v is None else str(v))


def _money(v) -> str:
    return f"INR {float(v):,.2f}" if v is not None else "—"


def _date(d) -> str:
    return d.strftime("%d %b %Y") if d else "—"


def _redirect(portal: _Portal, msg: str) -> RedirectResponse:
    return RedirectResponse(f"{portal.base}?msg={urllib.parse.quote(msg)}", status_code=303)


@router.get("/{slug}/{token}", response_class=HTMLResponse)
def portal_page(slug: str, token: str, msg: str | None = None):
    portal = _Portal(slug, token)
    try:
        if portal.link is None:
            return _invalid()
        portal.link.last_used_at = rw.now()
        company = portal.db.get(models.Company, portal.link.company_id)
        note = f'<div class="note">{_e(msg)}</div>' if msg else ""
        if portal.link.purpose == "offer":
            out = _offer_page(portal, note)
        else:
            out = _preboarding_page(portal, note)
        portal.db.commit()
        return _page("Your offer" if portal.link.purpose == "offer" else "Joining formalities", out,
                     company.name if company else "")
    finally:
        portal.close()


# ── offer ───────────────────────────────────────────────────────────────────

def _offer_for(portal: _Portal) -> models.Offer | None:
    o = portal.db.get(models.Offer, portal.link.entity_id)
    if o is None or o.application.opening.company_id != portal.link.company_id:
        return None
    return o


def _offer_page(portal: _Portal, note: str) -> str:
    o = _offer_for(portal)
    if o is None:
        return '<div class="card"><h1>Offer not found</h1></div>'
    onb.expire_due_offers(portal.db, portal.link.company_id)
    st = onb.offer_status(o)
    if st == "sent":
        onb.mark_offer_viewed(portal.ctx, o, source="portal")
        st = "viewed"
    a = o.application
    rows = [("Position", o.designation or a.opening.title),
            ("Department", rw._name_of(portal.db, models.Department, o.department_id or a.opening.department_id)),
            ("Location", rw._name_of(portal.db, models.Branch, o.branch_id or a.opening.branch_id)),
            ("Employment type", rw.EMPLOYMENT_TYPE_LABELS.get(o.employment_type or "", o.employment_type)),
            ("Work mode", o.work_mode), ("Annual CTC", _money(o.offered_ctc)),
            ("Joining date", _date(o.proposed_joining_date)),
            ("Probation", f"{o.probation_months} months" if o.probation_months else None),
            ("Notice period", f"{o.notice_period_days} days" if o.notice_period_days else None),
            ("Working hours", o.working_hours), ("Please respond by", _date(o.expiry_date))]
    table = "".join(f'<tr><td class="k">{_e(k)}</td><td>{_e(v)}</td></tr>' for k, v in rows if v)
    extra = "".join(f'<h2 style="margin-top:14px">{_e(t)}</h2><p style="white-space:pre-wrap;font-size:14px">{_e(v)}</p>'
                    for t, v in (("Benefits", o.benefits), ("Other terms", o.terms)) if v)
    body = (f'{note}<div class="card"><h1>Offer of employment</h1><p class="muted">Dear {_e(a.candidate.name)}, '
            f'we are delighted to offer you the position below.</p><table>{table}</table>{extra}'
            f'<p style="margin-top:14px"><a class="btn ghost" href="{portal.base}/offer.pdf">Download offer letter (PDF)</a></p></div>')
    if st == "viewed":
        body += (f'<div class="card"><h2>Accept the offer</h2><form method="post" action="{portal.base}/accept">'
                 '<label><input type="checkbox" name="confirm" value="yes" required style="width:auto"> '
                 'I have read the offer letter and accept the offer and its terms.</label>'
                 '<label>Message to HR (optional)</label><textarea name="message" rows="2" maxlength="1000"></textarea>'
                 '<p><button class="btn primary" type="submit">Accept offer</button></p></form></div>'
                 f'<div class="card"><h2>Decline the offer</h2><form method="post" action="{portal.base}/decline">'
                 '<label>Reason (optional)</label><textarea name="reason" rows="2" maxlength="1000"></textarea>'
                 '<p><button class="btn danger" type="submit">Decline offer</button></p></form></div>')
    else:
        label = {"accepted": ("ok", "You accepted this offer. Thank you -- the HR team will contact you about joining formalities."),
                 "declined": ("bad", "You declined this offer."), "expired": ("warn", "This offer has expired."),
                 "withdrawn": ("bad", "This offer has been withdrawn.")}.get(st, ("warn", f"This offer is {st}."))
        body += f'<div class="card"><span class="badge {label[0]}">{_e(onb.OFFER_LABELS.get(st, st))}</span><p>{_e(label[1])}</p></div>'
    return body


@router.get("/{slug}/{token}/offer.pdf")
def portal_offer_pdf(slug: str, token: str):
    from .recruitment import offer_letter_pdf

    portal = _Portal(slug, token)
    try:
        if portal.link is None or portal.link.purpose != "offer":
            return _invalid()
        o = _offer_for(portal)
        company = portal.db.get(models.Company, portal.link.company_id)
        pdf, safe = offer_letter_pdf(portal.db, company, crud.get_offer_detail(portal.db, o.id, company.id))
        return Response(content=pdf, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="offer-letter-{safe}.pdf"'})
    finally:
        portal.close()


@router.post("/{slug}/{token}/accept")
def portal_accept(slug: str, token: str, confirm: str = Form(""), message: str = Form("")):
    portal = _Portal(slug, token)
    try:
        if portal.link is None or portal.link.purpose != "offer":
            return _invalid()
        if confirm != "yes":
            return _redirect(portal, "Tick the confirmation box to accept the offer.")
        o = portal.db.scalar(select(models.Offer).where(models.Offer.id == portal.link.entity_id).with_for_update())
        portal.ctx.actor_name = o.application.candidate.name
        try:
            onb.accept_offer(portal.ctx, o, source="portal", notes=message.strip()[:1000] or None)
        except HTTPException as exc:
            portal.db.commit()
            return _page("Offer", f'<div class="card"><h1>Could not accept</h1><p>{_e(exc.detail)}</p></div>', status=409)
        portal.db.commit()
        return _page("Offer accepted", '<div class="card"><h1>Thank you!</h1><p>Your acceptance has been recorded. '
                     'The HR team will send you the joining formalities shortly.</p></div>')
    finally:
        portal.close()


@router.post("/{slug}/{token}/decline")
def portal_decline(slug: str, token: str, reason: str = Form("")):
    portal = _Portal(slug, token)
    try:
        if portal.link is None or portal.link.purpose != "offer":
            return _invalid()
        o = portal.db.scalar(select(models.Offer).where(models.Offer.id == portal.link.entity_id).with_for_update())
        portal.ctx.actor_name = o.application.candidate.name
        try:
            onb.decline_offer(portal.ctx, o, source="portal", reason=reason.strip()[:1000] or None)
        except HTTPException as exc:
            return _page("Offer", f'<div class="card"><h1>Could not record your response</h1><p>{_e(exc.detail)}</p></div>', status=409)
        portal.db.commit()
        return _page("Offer declined", '<div class="card"><h1>Response recorded</h1><p>You have declined the offer. '
                     'Thank you for letting us know.</p></div>')
    finally:
        portal.close()


# ── preboarding ─────────────────────────────────────────────────────────────

def _pb_for(portal: _Portal) -> models.Preboarding | None:
    p = portal.db.get(models.Preboarding, portal.link.entity_id)
    return p if p is not None and p.company_id == portal.link.company_id else None


_STATUS_BADGE = {"approved": ("ok", "Approved"), "completed": ("ok", "Done"), "uploaded": ("warn", "Under review"),
                 "resubmission_required": ("bad", "Please upload again"), "requested": ("warn", "Pending"),
                 "not_started": ("warn", "Pending")}
_FIELD_LABELS = {
    "personal": [("first_name", "First name"), ("last_name", "Last name"), ("gender", "Gender"),
                 ("date_of_birth", "Date of birth"), ("blood_group", "Blood group"), ("marital_status", "Marital status"),
                 ("nationality", "Nationality"), ("pan", "PAN")],
    "contact": [("personal_email", "Personal email"), ("personal_phone", "Mobile number"),
                ("current_address", "Current address"), ("permanent_address", "Permanent address")],
    "emergency": [("name", "Contact name"), ("relation", "Relationship"), ("phone", "Phone")],
    "bank": [("account_holder", "Account holder name"), ("bank_name", "Bank name"), ("account_no", "Account number"),
             ("ifsc", "IFSC")],
}
_GROUP_TITLES = {"personal": "Personal details", "contact": "Address & contact", "emergency": "Emergency contact",
                 "bank": "Bank account"}


def _preboarding_page(portal: _Portal, note: str) -> str:
    p = _pb_for(portal)
    if p is None or p.status in ("completed", "cancelled"):
        return '<div class="card"><h1>Nothing to complete</h1><p class="muted">This joining checklist is closed.</p></div>'
    details = p.joiner_details or {}
    groups = sorted({t.field_group for t in p.tasks if t.task_type == "information" and t.field_group in _FIELD_LABELS},
                    key=list(_FIELD_LABELS).index)
    body = (f'{note}<div class="card"><h1>Welcome, {_e(p.candidate.name)}!</h1><p class="muted">Joining as '
            f'{_e(p.designation)} on {_e(_date(p.joining_date))}. Please complete the items below.</p></div>')
    if groups:
        fields = ""
        for g in groups:
            sec = details.get(g) or {}
            fields += f'<h2 style="margin-top:12px">{_e(_GROUP_TITLES[g])}</h2><div class="grid">'
            for key, label in _FIELD_LABELS[g]:
                val = sec.get(key) or ""
                if g == "bank" and key == "account_no" and val:
                    val = ""
                typ = "date" if key == "date_of_birth" else "text"
                fields += (f'<div><label>{_e(label)}</label><input type="{typ}" name="{g}.{key}" value="{_e(val)}" '
                           f'maxlength="300"{" placeholder=" + chr(34) + "(saved -- re-enter to change)" + chr(34) if g == "bank" and key == "account_no" and sec.get(key) else ""}></div>')
            fields += "</div>"
        body += (f'<div class="card"><form method="post" action="{portal.base}/details">{fields}'
                 '<p style="margin-top:14px"><button class="btn primary" type="submit">Save details</button></p></form></div>')
    tasks = ""
    for t in p.tasks:
        if t.task_type not in ("document", "acknowledgement"):
            continue
        badge = _STATUS_BADGE.get(t.status, ("warn", t.status.replace("_", " ").title()))
        row = (f'<div class="task"><div class="row" style="justify-content:space-between"><strong>{_e(t.name)}'
               f'{"" if t.required else " (optional)"}</strong><span class="badge {badge[0]}">{_e(badge[1])}</span></div>')
        if t.status == "resubmission_required" and t.review_notes:
            row += f'<p class="muted">HR note: {_e(t.review_notes)}</p>'
        if t.task_type == "document" and t.status in ("not_started", "requested", "resubmission_required"):
            row += (f'<form method="post" enctype="multipart/form-data" action="{portal.base}/upload/{t.id}" class="row" '
                    'style="margin-top:8px"><input type="file" name="file" required accept=".pdf,.jpg,.jpeg,.png,.doc,.docx" '
                    'style="flex:1;min-width:200px"><button class="btn primary" type="submit">Upload</button></form>')
        if t.task_type == "acknowledgement" and t.status in ("not_started", "requested"):
            row += (f'<form method="post" action="{portal.base}/ack/{t.id}" style="margin-top:8px"><label>'
                    '<input type="checkbox" name="confirm" value="yes" required style="width:auto"> '
                    f'I have read and acknowledge: {_e(t.name)}</label><button class="btn primary" type="submit">Acknowledge</button></form>')
        tasks += row + "</div>"
    if tasks:
        body += f'<div class="card"><h2>Documents & acknowledgements</h2>{tasks}<p class="muted">PDF, JPG, PNG, DOC or DOCX, up to 25 MB.</p></div>'
    return body


def _pb_action(slug: str, token: str, fn) -> Response:
    portal = _Portal(slug, token)
    try:
        if portal.link is None or portal.link.purpose != "preboarding":
            return _invalid()
        p = portal.db.scalar(select(models.Preboarding).where(models.Preboarding.id == portal.link.entity_id).with_for_update())
        if p is None or p.status in ("completed", "cancelled"):
            return _invalid()
        portal.ctx.actor_name = p.candidate.name
        try:
            msg = fn(portal, p)
        except HTTPException as exc:
            portal.db.rollback()
            detail = exc.detail["message"] if isinstance(exc.detail, dict) else exc.detail
            return _redirect(portal, str(detail))
        portal.db.commit()
        return _redirect(portal, msg)
    finally:
        portal.close()


@router.post("/{slug}/{token}/details")
async def portal_details(slug: str, token: str, request: Request):
    form = await request.form()
    data: dict = {}
    for key, value in form.items():
        if "." not in key or not isinstance(value, str):
            continue
        group, field = key.split(".", 1)
        if group in _FIELD_LABELS and field in dict(_FIELD_LABELS[group]):
            if group == "bank" and field == "account_no" and not value.strip():
                continue
            data.setdefault(group, {})[field] = value.strip()[:300] or None

    def run(portal, p):
        onb.update_joiner_details(portal.ctx, p, data, via="portal")
        return "Your details were saved. Thank you!"
    return _pb_action(slug, token, run)


@router.post("/{slug}/{token}/upload/{task_id}")
def portal_upload(slug: str, token: str, task_id: str, file: UploadFile = File(...)):
    def run(portal, p):
        t = next((x for x in p.tasks if str(x.id) == task_id), None)
        if t is None:
            raise HTTPException(status_code=404, detail="Item not found.")
        onb.upload_document(portal.ctx, t, file, via="portal", uploader_name=p.candidate.name)
        return f"{t.name} uploaded -- HR will review it."
    return _pb_action(slug, token, run)


@router.post("/{slug}/{token}/ack/{task_id}")
def portal_ack(slug: str, token: str, task_id: str, confirm: str = Form("")):
    def run(portal, p):
        t = next((x for x in p.tasks if str(x.id) == task_id and x.task_type == "acknowledgement"), None)
        if t is None:
            raise HTTPException(status_code=404, detail="Item not found.")
        if confirm != "yes":
            raise HTTPException(status_code=422, detail="Tick the box to acknowledge.")
        if t.status != "completed":
            t.status = "completed"
            t.completed_at = rw.now()
            rw.record(portal.ctx, "preboarding", p.id, f"{t.name} acknowledged by candidate", old="requested",
                      new="completed", application=p.application, actor_user_id=None)
            onb.recompute(portal.ctx, p)
        return f"{t.name}: acknowledged."
    return _pb_action(slug, token, run)



