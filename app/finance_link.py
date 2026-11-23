"""Expense claims post to IMPACGO Finance; HRMS assets report who holds Finance's fixed assets.

Configuration (in HRMS):
  * Expense category -> GL account   hcm_expense_claim_types.gl_account_id: the expense account (Finance's account id) each claim row of that category is debited to; several
                                      categories may share one.
  * Employee -> payable account      core_employees.payable_account_id: the liability account (Finance's account id) that holds what the company owes that employee. The
                                      employee is NOT a supplier in Finance.

What reaches Finance (through its integration gateway, POST /api/fin/integration/transactions, with the HRMS connector key):
  * claim PENDING (submitted, waiting for approval)  -> a DRAFT journal in Finance (not in the ledger). Changed while pending, the draft is replaced.
  * claim APPROVED                                   -> the journal is POSTED, numbered by Finance's 'Expense Claims' journal name (its own series):
        Dr  each claim row's category GL account        the row amount
        Dr  claim taxes & charges account               the claim's taxes (only when the claim has any)
        Cr  employee advances account                   the advances applied (only when any)
        Cr  the employee's payable account              the net payable (rows + taxes - advances)
  * claim back to DRAFT / SENT BACK / REJECTED       -> the draft journal is discarded (it never reached the ledger).
  * claim marked PAID                                -> Dr employee's payable account / Cr bank, on the clearance date. Un-marked -> that payment journal is reversed.

How it works: any change of a claim (status, paid, amounts) queues the claim in hcm_finance_outbox in the SAME transaction (a session hook, so every path -- the form, the
approvals inbox, an approval engine -- is covered). The sender brings Finance in line with the claim as it is NOW, so it is safe to repeat: Finance's idempotency keys
mean a resend never posts twice. A refusal (an unmapped category, an employee with no payable account, a closed period ...) is kept on the claim (`finance_error`) and the
queue row is FAILED until someone fixes the cause and retries.

Currencies & FX: HRMS keeps no exchange rates. A claim in another currency than Finance's base currency posts in its own currency at the rate IMPACGO Finance's
Currency & FX gives for the posting date (the same rate for the draft and the posting; HRMS shows it on the claim with the base amount). Its payment posts in the
base currency for exactly the base amount owed, so the employee's payable account clears. A currency or a rate missing in Finance is named on the claim.

Assets (no ledger effect): an HRMS asset may be linked to the fixed asset it is in Finance's register (hcm_asset_inventory.finance_asset_id; Finance keeps the books --
cost, depreciation, disposal). When it is assigned to an employee or taken back, the holder is reported to Finance (PUT /api/fin/fixed-assets/{id}/custodian): on the
shared tenant an HRMS employee is the same core_employees row Finance reads. Unlinking or deleting the HRMS asset clears the holder it had reported.

Settings, per company: Administration > Integrations > IMPACGO Finance (hcm_finance_link_settings): the Finance address, the HRMS connector key the customer
creates in their Finance (stored encrypted with TENANT_EMAIL_ENCRYPTION_KEY, never shown again), and the accounts below. "Test connection" checks the key belongs to
the same company in Finance. A company with no saved settings may still use the environment below (one company per deployment, the earlier set-up):
  FINANCE_URL, FINANCE_API_KEY (the HRMS connector key; never in git), FINANCE_TENANT (X-Tenant, default 'finance'),
  FINANCE_COMPANY_ID (the HRMS company whose claims post with this key), FINANCE_DELIVER_SCHEMAS (tenant schemas the sender serves, default FINANCE_TENANT),
  FINANCE_DELIVER_EVERY (seconds, default 30), FINANCE_JOURNAL_NAME (default 'Expense Claims'),
  FINANCE_BANK_ACCOUNT_ID (GL account a claim is paid from), FINANCE_ADVANCE_ACCOUNT_ID (employee advances), FINANCE_CLAIM_TAX_ACCOUNT_ID (claim taxes & charges),
  FINANCE_BUSINESS_UNIT_ID (the business unit expense lines carry when the employee's department has none: Finance requires one on its expense accounts).
"""
from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import event, inspect, select, text
from sqlalchemy.orm import Session

from . import models
from .database import SessionLocal, get_db
from .deps import require_permission

log = logging.getLogger("uvicorn.error")
D = Decimal


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


class Settings:
    """Read on use, so a test or a restart with new values takes effect."""

    @property
    def url(self): return _env("FINANCE_URL").rstrip("/")
    @property
    def key(self): return _env("FINANCE_API_KEY")
    @property
    def tenant(self): return _env("FINANCE_TENANT", "finance")
    @property
    def company(self): return _env("FINANCE_COMPANY_ID")
    @property
    def schemas(self): return [s.strip() for s in _env("FINANCE_DELIVER_SCHEMAS", self.tenant).split(",") if s.strip()]
    @property
    def every(self): return int(_env("FINANCE_DELIVER_EVERY", "30") or 30)
    @property
    def journal_name(self): return _env("FINANCE_JOURNAL_NAME", "Expense Claims")
    @property
    def bank(self): return _env("FINANCE_BANK_ACCOUNT_ID")
    @property
    def advance(self): return _env("FINANCE_ADVANCE_ACCOUNT_ID")
    @property
    def claim_tax(self): return _env("FINANCE_CLAIM_TAX_ACCOUNT_ID")
    @property
    def business_unit(self): return _env("FINANCE_BUSINESS_UNIT_ID")
    @property
    def enabled(self): return bool(self.url and self.key and self.company)


settings = Settings()


@dataclasses.dataclass(frozen=True)
class LinkConfig:
    """How one company reaches its Finance: from its saved settings, or (one company per deployment) the environment."""
    company_id: str
    url: str
    key: str
    tenant: str
    journal_name: str = "Expense Claims"
    bank: str | None = None
    advance: str | None = None
    claim_tax: str | None = None
    business_unit: str | None = None
    source: str = "settings"


def _env_config() -> LinkConfig | None:
    if not settings.enabled:
        return None
    return LinkConfig(company_id=settings.company, url=settings.url, key=settings.key, tenant=settings.tenant, journal_name=settings.journal_name, bank=settings.bank or None,
                      advance=settings.advance or None, claim_tax=settings.claim_tax or None, business_unit=settings.business_unit or None, source="environment")


def _has_settings_table(db: Session) -> bool:
    return db.execute(text("SELECT to_regclass('hcm_finance_link_settings') IS NOT NULL")).scalar()


def config_for(db: Session, company_id) -> LinkConfig | None:
    """The company's link, or None when it is not linked. Saved settings win over the environment; a disabled saved row means not linked."""
    from .database import get_session_tenant_slug

    row = db.get(models.FinanceLinkSettings, uuid.UUID(str(company_id))) if _has_settings_table(db) else None
    if row is not None:
        if not (row.enabled and row.api_key_encrypted and row.finance_url):
            return None
        from .tenant_email.encryption import decrypt_secret

        return LinkConfig(company_id=str(row.company_id), url=row.finance_url.rstrip("/"), key=decrypt_secret(row.api_key_encrypted),
                          tenant=row.finance_tenant or get_session_tenant_slug(db) or settings.tenant, journal_name=row.journal_name or "Expense Claims",
                          bank=_str(row.bank_account_id), advance=_str(row.advance_account_id), claim_tax=_str(row.claim_tax_account_id),
                          business_unit=_str(row.business_unit_id))
    env = _env_config()
    return env if env is not None and env.company_id == str(company_id) else None


def _str(v) -> str | None:
    return str(v) if v else None


def is_linked(session: Session, company_id) -> bool:
    """Cached per session: the before-flush hook asks for every changed claim / asset."""
    cache = session.info.setdefault("finance_linked", {})
    key = str(company_id)
    if key not in cache:
        with session.no_autoflush:
            if _has_settings_table(session):
                row = session.execute(text("SELECT enabled AND api_key_encrypted IS NOT NULL FROM hcm_finance_link_settings WHERE company_id = :c"), {"c": key}).first()
                if row is not None:
                    cache[key] = bool(row[0])
                    return cache[key]
            cache[key] = settings.enabled and settings.company == key
    return cache[key]


class LinkError(Exception):
    """Finance cannot take the claim as it is: a person has to fix the cause (the message says what)."""


class Retry(Exception):
    """Finance is not reachable or busy: try again later."""


def _money(v) -> D:
    return D(str(v or 0)).quantize(D("0.01"), rounding=ROUND_HALF_UP)


def _s(v: D) -> str:
    return str(_money(v))


# ================================================================ the journals (pure: no database, no network)
def claim_journal(claim: dict, cfg: dict) -> dict:
    """The gateway lines of a claim. claim: {id, claim_no, posting_date, currency_code, base_currency, employee_payable, business_unit_id, total_taxes, advance_amount, total_amount,
    lines: [{category, gl_account_id, amount}]} and, for a claim in another currency, exchange_rate (IMPACGO Finance's rate: 1 claim currency = rate base); cfg: {claim_tax, advance}.
    The expense debits carry the business unit (Finance's account structure asks for one).

    A claim in another currency is posted in Finance's base currency, converted at Finance's rate: each line rounded to the paisa, the employee's payable taking
    any rounding so the journal balances exactly. Each line's description keeps the original amount and the rate. (Lines sent in a foreign currency would be
    stored in Finance's ledger with unrounded base amounts, and a rounding difference would need an Exchange Gain/Loss line.)"""
    bu = claim.get("business_unit_id")
    dims = {"business_unit_id": str(bu)} if bu else {}
    code, base = (claim.get("currency_code") or "INR"), (claim.get("base_currency") or "INR")
    rate = None
    if code != base:
        if not claim.get("exchange_rate"):
            raise LinkError(f"The claim is in {code}, not the company's {base}, and IMPACGO Finance has no {code} rate for it: add the currency and its rate under "
                            f"Finance > Currency & FX, then retry")
        rate = D(str(claim["exchange_rate"]))
    conv = (lambda a: base_amount(a, rate)) if rate else _money
    note = (lambda a: f" · {code} {_money(a)} @ {rate.normalize():f}") if rate else (lambda a: "")
    if not claim.get("employee_payable"):
        raise LinkError("The employee has no payable account: set it on the Employee Master (Finance payable account)")
    lines: list[dict] = []
    for row in claim.get("lines", []):
        amount = _money(row["amount"])
        if amount == 0:
            continue
        if not row.get("gl_account_id"):
            raise LinkError(f"Expense category '{row.get('category') or '(none)'}' has no GL account: set it under Expense Categories")
        lines.append({"account_id": str(row["gl_account_id"]), "debit": _s(conv(amount)), "credit": "0", "description": f"{row.get('category') or 'Expense'}{note(amount)}"[:200], **dims})
    taxes = _money(claim.get("total_taxes"))
    if taxes:
        if not cfg.get("claim_tax"):
            raise LinkError("The claim has taxes & charges but no claim-tax GL account is configured (Integrations > IMPACGO Finance)")
        lines.append({"account_id": cfg["claim_tax"], "debit": _s(conv(taxes)), "credit": "0", "description": f"Taxes & charges{note(taxes)}"[:200], **dims})
    advance = _money(claim.get("advance_amount"))
    if advance:
        if not cfg.get("advance"):
            raise LinkError("The claim uses an advance but no employee-advance GL account is configured (Integrations > IMPACGO Finance)")
        lines.append({"account_id": cfg["advance"], "debit": "0", "credit": _s(conv(advance)), "description": f"Advance applied{note(advance)}"[:200]})
    net = _money(claim.get("total_amount"))
    if not lines or not net and not advance:
        raise LinkError("The claim has no amount to post")
    if _money(sum((_money(r["amount"]) for r in claim.get("lines", [])), D(0)) + taxes - advance) != net:
        raise LinkError(f"The claim does not add up: rows + taxes - advances = {_money(sum((_money(r['amount']) for r in claim.get('lines', [])), D(0)) + taxes - advance)}, "
                        f"net payable = {net}")
    dr = sum((D(l["debit"]) for l in lines), D(0))
    owed = dr - sum((D(l["credit"]) for l in lines), D(0))  # in the base currency; equals net when the claim is in the base currency
    if net:
        lines.append({"account_id": str(claim["employee_payable"]), "debit": "0", "credit": _s(owed),
                      "description": f"Payable to employee · {claim.get('claim_no') or ''}{note(net)}".strip(" ·")[:200]})
    if dr == 0:
        raise LinkError("The claim has no amount to post")
    return {"lines": lines, "total": dr, "base_payable": owed}


def base_amount(amount, rate) -> D:
    """An amount in the claim currency in Finance's base currency, rounded the way Finance rounds each converted line (to the paisa, half-even)."""
    return (D(str(amount or 0)) * D(str(rate))).quantize(D("0.01")) if rate else _money(amount)


def gateway_body(*, doctype: str, source_id, source_no: str | None, key: str, posting_date: str, remarks: str, lines: list[dict], draft: bool = False,
                 journal_name: str | None = None) -> dict:
    b: dict[str, Any] = {"source_module": "hrms", "source_doctype": doctype, "source_id": str(source_id), "source_no": (source_no or "")[:60] or None, "idempotency_key": key[:100],
                         "posting_date": posting_date, "remarks": remarks[:500], "lines": lines}
    if draft:
        b["draft"] = True
    if journal_name:
        b["journal_name"] = journal_name
    return b


def content_key(lines: list[dict], posting_date: str) -> str:
    return hashlib.sha256(json.dumps([lines, posting_date], sort_keys=True).encode()).hexdigest()[:10]


# ================================================================ talking to Finance
class Finance:
    def __init__(self, url: str | None = None, key: str | None = None, tenant: str | None = None, opener=None, cfg: LinkConfig | None = None):
        self.cfg = cfg or _env_config() or LinkConfig(company_id="", url=url or "", key=key or "", tenant=tenant or settings.tenant)
        self.url, self.key, self.tenant = (url or self.cfg.url), (key or self.cfg.key), (tenant or self.cfg.tenant)
        self.open = opener or urllib.request.urlopen

    @classmethod
    def of(cls, cfg: LinkConfig) -> "Finance":
        return cls(cfg=cfg)

    # ---- Currencies & FX, read from Finance (one client = one company; cached for the client's life, i.e. one sender round or one request)
    def currencies(self) -> dict[str, dict]:
        """Finance's active currencies: code -> {id, code, name, symbol}."""
        if not hasattr(self, "_currencies"):
            d = self.call("GET", "/api/core/currencies?limit=200").get("data") or {}
            items = d.get("items", []) if isinstance(d, dict) else d
            self._currencies = {c["code"].upper(): c for c in items if c.get("is_active", True)}
        return self._currencies

    def base_currency(self) -> str:
        """The company's base (default) currency in Finance."""
        if not hasattr(self, "_base"):
            me = self.call("GET", "/api/core/companies/me").get("data") or {}
            self._base = (me.get("default_currency_code") or "INR").upper()
        return self._base

    def rate(self, code: str, day: str) -> tuple[D, str] | None:
        """(rate, effective date) to convert 1 [code] into the base currency on [day], as Finance resolves it: the latest rate on or before the day, the
        inverse pair when only that is recorded. None when Finance has none."""
        base = self.base_currency()
        if code.upper() == base:
            return D(1), day
        cur = self.currencies()
        if code.upper() not in cur or base not in cur:
            return None
        a, b = cur[code.upper()]["id"], cur[base]["id"]
        for frm, to, inverse in ((a, b, False), (b, a, True)):
            try:
                r = (self.call("GET", f"/api/core/currencies/exchange-rates?from_currency_id={frm}&to_currency_id={to}&as_of={day}").get("data") or {})
            except LinkError as e:
                if "(404)" in str(e):
                    continue
                raise
            if r.get("rate"):
                value = D(str(r["rate"]))
                return ((D(1) / value).quantize(D("0.00000001")) if inverse else value), str(r.get("effective_date") or day)
        return None

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(self.url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-API-Key": self.key, "X-Tenant": self.tenant, "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self.open(req, timeout=30) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                j = json.loads(e.read() or b"{}")
            except ValueError:
                j = {}
            msg = j.get("message") or j.get("detail") or str(e)
            errs = (j.get("data") or {}).get("errors") if isinstance(j.get("data"), dict) else None
            if errs:
                msg = f"{msg}: " + "; ".join(f"{x.get('field') or ''} {x.get('error') or ''}".strip() for x in errs)
            if e.code == 429 or e.code >= 500:
                raise Retry(f"Finance answered {e.code}: {msg}") from e
            raise LinkError(f"Finance refused it ({e.code}): {msg}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise Retry(f"Finance is not reachable: {e}") from e


def _voucher(d: dict) -> str | None:
    tr = d.get("trace") or {}
    return (tr.get("voucher") or {}).get("voucher_number") or (tr.get("journal_entry") or {}).get("entry_no")


# ================================================================ bringing Finance in line with a claim
def _claim_facts(db: Session, claim: models.ExpenseClaim, cfg: LinkConfig | None = None) -> dict:
    from . import crud

    emp = db.get(models.Employee, claim.employee_id)
    dept = db.get(models.Department, emp.department_id) if emp is not None and emp.department_id else None
    types = {t.id: t for t in db.scalars(select(models.ExpenseClaimType).where(models.ExpenseClaimType.company_id == claim.company_id))}
    by_name = {t.name.lower(): t for t in types.values()}
    rows = []
    for l in sorted(claim.lines, key=lambda l: l.sort_order or 0):
        t = types.get(l.claim_type_id) or by_name.get((l.category or "").lower())
        rows.append({"category": l.category or (t.name if t else None), "gl_account_id": t.gl_account_id if t else None, "amount": l.amount})
    return {"id": claim.id, "claim_no": claim.claim_no, "posting_date": str(claim.posting_date or claim.claim_date), "currency_code": (claim.currency_code or "INR").upper(),
            "base_currency": crud.company_base_currency(db, claim.company_id), "employee_payable": emp.payable_account_id if emp else None,  # replaced by Finance's in sync_claim
            "business_unit_id": getattr(dept, "business_unit_id", None) or (cfg.business_unit if cfg else None),
            "total_taxes": claim.total_taxes, "advance_amount": claim.advance_amount, "total_amount": claim.total_amount, "lines": rows}


def _claim_fx(fin: Finance, code: str, day: str) -> dict:
    """Finance's base currency and, for a claim in another currency, Finance's currency id and rate for the posting day (HRMS keeps no rates)."""
    base = fin.base_currency()
    if code.upper() == base:
        return {"base_currency": base}
    cur = fin.currencies().get(code.upper())
    if cur is None:
        raise LinkError(f"Currency {code} is not set up in IMPACGO Finance: add it under Finance > Currency & FX, then retry")
    found = fin.rate(code, day)
    if found is None:
        raise LinkError(f"IMPACGO Finance has no {code} to {base} exchange rate on or before {day}: add it under Finance > Currency & FX, then retry")
    return {"base_currency": base, "currency_id": cur["id"], "exchange_rate": found[0], "rate_date": found[1]}


def sync_claim(db: Session, claim: models.ExpenseClaim, fin: Finance) -> None:
    """Brings Finance in line with the claim as it is now. Raises LinkError (a person must act) or Retry (try later); on success the claim's finance_* fields say what Finance holds."""
    link = fin.cfg
    cfg = {"claim_tax": link.claim_tax, "advance": link.advance}
    want = {"pending": "DRAFT", "approved": "POSTED"}.get(claim.status)
    remarks = f"Expense claim {claim.claim_no or ''}".strip()
    if want is not None and claim.finance_status != "POSTED":
        day = str(claim.posting_date or claim.claim_date)
        facts = _claim_facts(db, claim, link)
        facts.update(_claim_fx(fin, facts["currency_code"], day))
        j = claim_journal(facts, cfg)
        claim.finance_base_currency = facts["base_currency"]
        claim.finance_exchange_rate = facts.get("exchange_rate")
        claim.finance_base_amount = j["base_payable"]  # what the employee is owed in the base currency (the payment pays exactly this)
        draft = want == "DRAFT"
        key = f"expense-claim-{claim.id}-draft-{content_key(j['lines'], day)}" if draft else f"expense-claim-{claim.id}"
        d = (fin.call("POST", "/api/fin/integration/transactions", gateway_body(doctype="expense_claim", source_id=claim.id, source_no=claim.claim_no, key=key, posting_date=day,
                                                                              remarks=remarks, lines=j["lines"], draft=draft, journal_name=link.journal_name)) or {}).get("data") or {}
        claim.finance_tx_id, claim.finance_status = d.get("id"), d.get("status") or want
        claim.finance_voucher_no = _voucher(d) if claim.finance_status == "POSTED" else ((d.get("trace") or {}).get("journal_entry") or {}).get("entry_no")
    elif want is None and claim.finance_status == "DRAFT" and claim.finance_tx_id:
        fin.call("POST", f"/api/fin/integration/transactions/{claim.finance_tx_id}/discard", {"reason": f"Claim {claim.claim_no or claim.id} is {claim.status.replace('_', ' ')} in HRMS"})
        claim.finance_tx_id, claim.finance_status, claim.finance_voucher_no = None, None, None
    # the payment
    if claim.status == "approved" and claim.finance_status == "POSTED" and claim.is_paid and not claim.finance_payment_tx_id:
        if not link.bank:
            raise LinkError("The claim is paid but no bank GL account is configured for claim payments (Integrations > IMPACGO Finance)")
        emp = db.get(models.Employee, claim.employee_id)
        if not (emp and emp.payable_account_id):
            raise LinkError("The employee has no payable account: set it on the Employee Master (Finance payable account)")
        # in the base currency for exactly what the claim's posting credited to the payable account (its amount at the claim's rate)
        net = _money(claim.finance_base_amount) if claim.finance_base_amount is not None else _money(claim.total_amount)
        day = str(claim.clearance_date or datetime.date.today())
        stamp = int((claim.updated_at or datetime.datetime.now(datetime.timezone.utc)).timestamp())
        lines = [{"account_id": str(emp.payable_account_id), "debit": _s(net), "credit": "0", "description": f"Claim {claim.claim_no or ''} paid".strip()},
                 {"account_id": link.bank, "debit": "0", "credit": _s(net), "description": f"Claim {claim.claim_no or ''} paid".strip()}]
        d = (fin.call("POST", "/api/fin/integration/transactions", gateway_body(doctype="expense_claim_payment", source_id=claim.id, source_no=claim.claim_no,
                                                                              key=f"expense-claim-paid-{claim.id}-{stamp}", posting_date=day, remarks=f"{remarks} paid", lines=lines,
                                                                              journal_name=link.journal_name)) or {}).get("data") or {}
        claim.finance_payment_tx_id, claim.finance_payment_voucher_no = d.get("id"), _voucher(d)
    elif not claim.is_paid and claim.finance_payment_tx_id:
        fin.call("POST", f"/api/fin/integration/transactions/{claim.finance_payment_tx_id}/reverse", {"reason": f"Claim {claim.claim_no or claim.id} marked unpaid in HRMS"})
        claim.finance_payment_tx_id, claim.finance_payment_voucher_no = None, None
    claim.finance_error = None


# ================================================================ queueing (in the same transaction as the change)
_WATCHED = ("status", "is_paid", "clearance_date", "total_amount", "total_claimed", "total_taxes", "advance_amount", "posting_date", "updated_at")


def _changed(obj) -> bool:
    st = inspect(obj)
    if st.pending:
        return True
    return any(st.attrs[a].history.has_changes() for a in _WATCHED if a in st.attrs)


@event.listens_for(SessionLocal, "before_flush")
def _queue_changed_claims(session: Session, flush_context, instances) -> None:
    if session.info.get("finance_link_sending"):
        return  # the sender writing back what Finance said
    queued = session.info.setdefault("finance_queued", set())
    for obj in list(session.new) + list(session.dirty):
        if not isinstance(obj, models.ExpenseClaim) or obj.id in queued or not is_linked(session, obj.company_id):
            continue
        if not _changed(obj):
            continue
        with session.no_autoflush:
            waiting = session.execute(select(models.FinanceOutbox.id).where(models.FinanceOutbox.company_id == obj.company_id, models.FinanceOutbox.doctype == "expense_claim",
                                                                            models.FinanceOutbox.doc_id == obj.id, models.FinanceOutbox.status == "pending")).first()
        if waiting is None:
            session.add(models.FinanceOutbox(id=uuid.uuid4(), company_id=obj.company_id, doctype="expense_claim", doc_id=obj.id, status="pending"))
        queued.add(obj.id)


def _queue(session: Session, company_id, doctype: str, doc_id) -> None:
    queued = session.info.setdefault("finance_queued", set())
    if (doctype, doc_id) in queued:
        return
    with session.no_autoflush:
        waiting = session.execute(select(models.FinanceOutbox.id).where(models.FinanceOutbox.company_id == company_id, models.FinanceOutbox.doctype == doctype,
                                                                        models.FinanceOutbox.doc_id == doc_id, models.FinanceOutbox.status == "pending")).first()
    if waiting is None:
        session.add(models.FinanceOutbox(id=uuid.uuid4(), company_id=company_id, doctype=doctype, doc_id=doc_id, status="pending"))
    queued.add((doctype, doc_id))


def _attr_changed(obj, *names: str) -> bool:
    st = inspect(obj)
    return st.pending or any(st.attrs[a].history.has_changes() for a in names)


@event.listens_for(SessionLocal, "before_flush")
def _queue_asset_holders(session: Session, flush_context, instances) -> None:
    """An assignment made, changed, returned or removed, or an asset linked / unlinked -> report the holder; a deleted linked asset -> clear the holder it reported."""
    if session.info.get("finance_link_sending"):
        return
    assets = set()
    for obj in list(session.new) + list(session.dirty) + list(session.deleted):
        if isinstance(obj, models.AssetAssignment):
            if obj in session.deleted or _attr_changed(obj, "employee_id", "returned_on", "asset_id"):
                assets.add(obj.asset_id)
                old = inspect(obj).attrs["asset_id"].history.deleted
                assets.update(a for a in old if a)
        elif isinstance(obj, models.AssetInventoryItem) and is_linked(session, obj.company_id):
            if obj in session.deleted:
                if obj.finance_custodian_asset_id:
                    _queue(session, obj.company_id, "asset_custodian_clear", obj.finance_custodian_asset_id)
            elif _attr_changed(obj, "finance_asset_id") and (obj.finance_asset_id or obj.finance_custodian_asset_id):
                assets.add(obj.id)
    for aid in assets:
        with session.no_autoflush:
            a = session.get(models.AssetInventoryItem, aid)
        if a is not None and a not in session.deleted and (a.finance_asset_id or a.finance_custodian_asset_id) and is_linked(session, a.company_id):
            _queue(session, a.company_id, "asset_custodian", a.id)


@event.listens_for(SessionLocal, "after_commit")
def _forget_queued(session: Session) -> None:
    session.info.pop("finance_queued", None)
    session.info.pop("finance_linked", None)


@event.listens_for(SessionLocal, "after_rollback")
def _forget_queued_rb(session: Session) -> None:
    session.info.pop("finance_queued", None)
    session.info.pop("finance_linked", None)


# ================================================================ asset holders (no ledger effect)
def current_holder(db: Session, asset_id) -> uuid.UUID | None:
    """The employee holding the asset now: its open assignment (not returned), the latest if there were ever two."""
    return db.scalar(select(models.AssetAssignment.employee_id).where(models.AssetAssignment.asset_id == asset_id, models.AssetAssignment.returned_on.is_(None))
                     .order_by(models.AssetAssignment.assigned_on.desc()).limit(1))


def report_holder(fin: Finance, finance_asset_id, employee_id) -> None:
    try:
        fin.call("PUT", f"/api/fin/fixed-assets/{finance_asset_id}/custodian", {"custodian_employee_id": str(employee_id) if employee_id else None, "source": "hrms"})
    except LinkError as e:
        if employee_id is None and "(404)" in str(e):
            return  # clearing the holder of an asset Finance no longer has: nothing to clear
        raise


def sync_asset_holder(db: Session, asset: models.AssetInventoryItem, fin: Finance) -> None:
    """Brings Finance's holder of the linked fixed asset in line with HRMS: the employee HRMS has it assigned to, and nobody once HRMS takes back (or unlinks) an
    asset whose holder it had reported. HRMS only reports what it knows: linking an asset HRMS has not assigned leaves the holder Finance already records alone."""
    if asset.finance_custodian_asset_id and asset.finance_custodian_asset_id != asset.finance_asset_id:
        report_holder(fin, asset.finance_custodian_asset_id, None)
        asset.finance_custodian_asset_id = None
    if asset.finance_asset_id:
        holder = current_holder(db, asset.id)
        if holder is not None or asset.finance_custodian_asset_id == asset.finance_asset_id:
            report_holder(fin, asset.finance_asset_id, holder)
            asset.finance_custodian_asset_id = asset.finance_asset_id
    asset.finance_error = None


# ================================================================ sending
def deliver(schema: str, limit: int = 100, fin: Finance | None = None) -> dict:
    """Works the waiting rows of one tenant schema, oldest first, one transaction per row; each company's rows go to its own Finance with its own key. A company
    that is not linked (any more) keeps its rows waiting. [fin] (tests) replaces every company's client."""
    counts: dict[str, int] = {}
    db = SessionLocal()
    db.info["tenant_slug"] = schema
    db.info["finance_link_sending"] = True
    clients: dict = {}
    try:
        companies = [r[0] for r in db.execute(select(models.FinanceOutbox.company_id).where(models.FinanceOutbox.status == "pending").distinct())]
        for c in companies:
            try:
                cfg = config_for(db, c)
            except Exception:  # noqa: BLE001 -- e.g. a saved key that cannot be decrypted: that company waits, the others go on
                log.exception("Finance link settings of company %s cannot be used", c)
                db.rollback()
                cfg = None
            if cfg is not None or fin is not None:
                clients[c] = fin or Finance.of(cfg)
        ids = [r[0] for r in db.execute(select(models.FinanceOutbox.id).where(models.FinanceOutbox.status == "pending", models.FinanceOutbox.company_id.in_(list(clients) or [None]))
                                        .order_by(models.FinanceOutbox.created_at).limit(limit))]
        db.commit()
        if not ids and companies and not clients:
            return {"detail": "No company with waiting documents is linked to IMPACGO Finance: nothing was sent"}
        for oid in ids:
            row = db.execute(select(models.FinanceOutbox).where(models.FinanceOutbox.id == oid, models.FinanceOutbox.status == "pending").with_for_update(skip_locked=True)).scalars().first()
            if row is None:
                db.rollback()
                continue
            client = clients[row.company_id]
            doc = (db.get(models.ExpenseClaim, row.doc_id) if row.doctype == "expense_claim" else
                   db.get(models.AssetInventoryItem, row.doc_id) if row.doctype == "asset_custodian" else None)
            row.attempts += 1
            row.updated_at = datetime.datetime.now(datetime.timezone.utc)
            try:
                if row.doctype == "asset_custodian_clear":
                    report_holder(client, row.doc_id, None)
                elif doc is not None:
                    (sync_claim if row.doctype == "expense_claim" else sync_asset_holder)(db, doc, client)
                row.status, row.last_error = "done", None
            except LinkError as e:
                row.status, row.last_error = "failed", str(e)[:2000]
                if doc is not None:
                    doc.finance_error = str(e)[:2000]
            except Retry as e:
                row.last_error = str(e)[:2000]
                if row.attempts >= 20:
                    row.status = "failed"
            db.commit()
            counts[row.status] = counts.get(row.status, 0) + 1
    finally:
        db.close()
    return counts


def linked_schemas() -> list[str]:
    """The tenant schemas the sender serves: FINANCE_DELIVER_SCHEMAS when set; otherwise every schema with a company linked in its settings, plus the environment's
    tenant when the environment links a company."""
    if _env("FINANCE_DELIVER_SCHEMAS"):
        return settings.schemas
    out = [settings.tenant] if settings.enabled else []
    db = SessionLocal()
    try:
        for (schema,) in db.execute(text("SELECT table_schema FROM information_schema.tables WHERE table_name = 'hcm_finance_link_settings' AND table_type = 'BASE TABLE'")):
            if schema not in out and db.execute(text(f'SELECT 1 FROM "{schema}".hcm_finance_link_settings WHERE enabled AND api_key_encrypted IS NOT NULL LIMIT 1')).first():
                out.append(schema)
    finally:
        db.close()
    return out


def _deliver_forever() -> None:
    while True:
        try:
            schemas = linked_schemas()
        except Exception:  # noqa: BLE001
            log.exception("Finance delivery: could not list the linked tenants")
            schemas = []
        for schema in schemas:
            try:
                deliver(schema)
            except Exception:  # noqa: BLE001 -- one tenant's trouble never stops the others
                log.exception("Finance delivery failed for %s", schema)
        time.sleep(max(5, settings.every))


def start_sender() -> None:
    """Always on: which tenants it serves is read on every round, so a customer who links their Finance needs no restart."""
    if _env("FINANCE_SENDER", "on").lower() not in ("off", "false", "0"):
        threading.Thread(target=_deliver_forever, daemon=True, name="finance-link").start()


# ================================================================ API (Finance / administrators)
router = APIRouter(prefix="/api/finance-link", tags=["finance-link"])
_ADMIN = "travel_expense_approval"
_SETTINGS = "system_settings_rbac"


def require_permission_any(*columns: str):
    """Passes with Edit / Admin on any of [columns] (the Owner always)."""
    checks = [require_permission(c) for c in columns]

    def checker(db: Session = Depends(get_db), user: models.User = Depends(_current_user())) -> models.User:
        last: HTTPException | None = None
        for check in checks:
            try:
                return check(user=user, db=db)
            except HTTPException as e:
                last = e
        raise last or HTTPException(status_code=403, detail="You don't have permission to do this")

    return checker


def _current_user():
    from .deps import get_current_user

    return get_current_user




def _own(db: Session, user: models.User) -> LinkConfig:
    """The caller's company link, or 409 when the company is not linked (the screens then hide their Finance fields)."""
    from .tenant_email.encryption import EncryptionConfigError

    try:
        cfg = config_for(db, user.company_id)
    except EncryptionConfigError as e:
        raise HTTPException(status_code=409, detail=f"The saved IMPACGO Finance key cannot be read ({e}): enter it again under Integrations") from e
    if cfg is None:
        raise HTTPException(status_code=409, detail="This company is not linked to IMPACGO Finance")
    return cfg


def _linked(db: Session, user: models.User) -> LinkConfig | None:
    try:
        return _own(db, user)
    except HTTPException:
        return None


@router.get("/status")
def status(db: Session = Depends(get_db), user: models.User = Depends(require_permission(_ADMIN))):
    queue = {r[0]: r[1] for r in db.execute(text("select status, count(*) from hcm_finance_outbox where company_id = :c group by status"), {"c": user.company_id})}
    unmapped = [r[0] for r in db.execute(select(models.ExpenseClaimType.name).where(models.ExpenseClaimType.company_id == user.company_id, models.ExpenseClaimType.is_active.is_(True),
                                                                                   models.ExpenseClaimType.gl_account_id.is_(None)))]
    no_payable = db.scalar(text("select count(*) from core_employees where company_id = :c and payable_account_id is null"), {"c": user.company_id})
    cfg = _linked(db, user)
    fin_base = None
    if cfg is not None:
        try:
            fin_base = Finance.of(cfg).base_currency()
        except (LinkError, Retry):
            fin_base = None
    return {"linked": cfg is not None, "source": cfg.source if cfg else None, "financeBaseCurrency": fin_base, "hrmsBaseCurrency": _hrms_base(db, user.company_id), "financeUrl": cfg.url if cfg else None, "tenant": cfg.tenant if cfg else None,
            "journalName": cfg.journal_name if cfg else None, "bankAccountSet": bool(cfg and cfg.bank), "advanceAccountSet": bool(cfg and cfg.advance),
            "claimTaxAccountSet": bool(cfg and cfg.claim_tax),
            "categoriesWithoutGl": unmapped, "employeesWithoutPayableAccount": int(no_payable or 0), "queue": queue}


@router.get("/outbox")
def outbox(status: str = "", db: Session = Depends(get_db), user: models.User = Depends(require_permission(_ADMIN))):
    q = select(models.FinanceOutbox).where(models.FinanceOutbox.company_id == user.company_id).order_by(models.FinanceOutbox.created_at.desc()).limit(200)
    if status:
        q = q.where(models.FinanceOutbox.status == status)
    out = []
    for r in db.scalars(q):
        c = db.get(models.ExpenseClaim, r.doc_id) if r.doctype == "expense_claim" else None
        out.append({"id": str(r.id), "doctype": r.doctype, "docId": str(r.doc_id), "claimNo": c.claim_no if c else None, "claimStatus": c.status if c else None, "status": r.status,
                    "attempts": r.attempts, "message": r.last_error, "financeStatus": c.finance_status if c else None, "financeVoucher": c.finance_voucher_no if c else None,
                    "paymentVoucher": c.finance_payment_voucher_no if c else None, "updatedAt": r.updated_at.isoformat() if r.updated_at else None})
    return {"items": out}


@router.post("/sync")
def sync_now(db: Session = Depends(get_db), user: models.User = Depends(require_permission(_ADMIN))):
    _own(db, user)
    from .database import get_session_tenant_slug

    db.commit()  # deliver works in its own session
    return {"result": deliver(get_session_tenant_slug(db) or settings.tenant)}


@router.post("/outbox/{oid}/retry")
def retry(oid: uuid.UUID, db: Session = Depends(get_db), user: models.User = Depends(require_permission(_ADMIN))):
    row = db.get(models.FinanceOutbox, oid)
    if row is None or row.company_id != user.company_id or row.status != "failed":
        raise HTTPException(status_code=404, detail="No failed row with that id")
    waiting = db.execute(select(models.FinanceOutbox.id).where(models.FinanceOutbox.company_id == row.company_id, models.FinanceOutbox.doc_id == row.doc_id,
                                                               models.FinanceOutbox.status == "pending")).first()
    if waiting is None:
        row.status, row.attempts, row.last_error = "pending", 0, None
    else:
        row.status = "done"  # a newer change of the same claim is already waiting: it brings Finance in line
    db.commit()
    return {"detail": "Queued again"}


@router.post("/queue-existing")
def queue_existing(db: Session = Depends(get_db), user: models.User = Depends(require_permission(_ADMIN))):
    """Queues the claims saved before the link (pending, approved or paid) so Finance gets them too."""
    _own(db, user)
    n = 0
    for c in db.scalars(select(models.ExpenseClaim).where(models.ExpenseClaim.company_id == user.company_id, models.ExpenseClaim.status.in_(("pending", "approved")))):
        waiting = db.execute(select(models.FinanceOutbox.id).where(models.FinanceOutbox.company_id == c.company_id, models.FinanceOutbox.doc_id == c.id,
                                                                   models.FinanceOutbox.status == "pending")).first()
        if waiting is None:
            db.add(models.FinanceOutbox(id=uuid.uuid4(), company_id=c.company_id, doctype="expense_claim", doc_id=c.id, status="pending"))
            n += 1
    db.commit()
    return {"queued": n}


@router.get("/gl-accounts")
def gl_accounts(search: str = "", root_type: str = "", db: Session = Depends(get_db), user: models.User = Depends(require_permission_any(_ADMIN, _SETTINGS))):
    """Finance's posting accounts, for the GL pickers (Expense Category GL account, Employee payable account, the settings' bank / advance / tax accounts).
    root_type: expense | liability | asset ..."""
    cfg = _own(db, user)
    try:
        q = "?limit=200" + (f"&search={urllib.request.quote(search)}" if search else "")
        d = Finance.of(cfg).call("GET", f"/api/fin/accounts{q}")
    except (LinkError, Retry) as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    items = (d.get("data") or {}).get("items", []) if isinstance(d.get("data"), dict) else (d.get("data") or [])
    return {"items": [{"id": a["id"], "code": a.get("code"), "name": a.get("name"), "rootType": a.get("root_type")} for a in items
                      if not a.get("is_group") and a.get("is_active", True) and (not root_type or a.get("root_type") == root_type)]}


_ASSETS = "asset_management"


def _fixed_asset(a: dict) -> dict:
    return {"id": a["id"], "code": a.get("asset_code"), "name": a.get("name"), "status": a.get("status"), "grossValue": a.get("gross_value"),
            "purchaseDate": a.get("purchase_date"), "custodianEmployeeId": a.get("custodian_employee_id")}


@router.get("/fixed-assets")
def fixed_assets(search: str = "", db: Session = Depends(get_db), user: models.User = Depends(require_permission(_ASSETS))):
    """Finance's fixed-asset register, for the Asset form's picker. Each item says which HRMS asset (if any) is already linked to it."""
    cfg = _own(db, user)
    try:
        d = Finance.of(cfg).call("GET", "/api/fin/fixed-assets?limit=200" + (f"&search={urllib.request.quote(search)}" if search else ""))
    except (LinkError, Retry) as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    linked = {str(r[0]): r[1] for r in db.execute(select(models.AssetInventoryItem.finance_asset_id, models.AssetInventoryItem.asset_tag)
                                                  .where(models.AssetInventoryItem.company_id == user.company_id, models.AssetInventoryItem.finance_asset_id.is_not(None)))}
    items = ((d.get("data") or {}).get("items") or []) if isinstance(d.get("data"), dict) else []
    names = _employee_names(db, [a.get("custodian_employee_id") for a in items])
    return {"items": [{**_fixed_asset(a), "custodianName": names.get(str(a.get("custodian_employee_id"))), "linkedAssetTag": linked.get(str(a["id"]))}
                      for a in items if a.get("is_active", True)]}


def _employee_names(db: Session, ids) -> dict[str, str]:
    """Finance stores the holder as the employee's id: on the shared tenant that is HRMS's own employee."""
    ids = [uuid.UUID(str(i)) for i in ids if i]
    if not ids:
        return {}
    return {str(e.id): " ".join(x for x in (e.first_name, e.last_name) if x) + (f" ({e.employee_code})" if e.employee_code else "")
            for e in db.scalars(select(models.Employee).where(models.Employee.id.in_(ids)))}


@router.get("/fixed-assets/{finance_asset_id}")
def fixed_asset(finance_asset_id: uuid.UUID, db: Session = Depends(get_db), user: models.User = Depends(require_permission(_ASSETS))):
    """One Finance fixed asset with its net book value on each depreciation book (Finance's numbers; HRMS stores none of them)."""
    fin = Finance.of(_own(db, user))
    try:
        a = (fin.call("GET", f"/api/fin/fixed-assets/{finance_asset_id}").get("data") or {})
        books = fin.call("GET", f"/api/fin/fixed-assets/{finance_asset_id}/nbv-by-book").get("data") or []
    except (LinkError, Retry) as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {**_fixed_asset(a), "custodianName": _employee_names(db, [a.get("custodian_employee_id")]).get(str(a.get("custodian_employee_id"))), "books": [{"book": b.get("book_name"), "postsToGl": b.get("posts_to_gl"), "accumulatedDepreciation": b.get("accumulated_depreciation"),
                                          "netBookValue": b.get("nbv")} for b in books]}


class PayableAccountIn(BaseModel):
    payable_account_id: uuid.UUID | None = None


@router.get("/employees/{employee_id}/payable-account")
def get_employee_payable(employee_id: uuid.UUID, db: Session = Depends(get_db), user: models.User = Depends(require_permission(_ADMIN))):
    """The Employee Master's Finance payable account, and whether this company posts to Finance (the screen shows the picker only then)."""
    emp = db.get(models.Employee, employee_id)
    if emp is None or emp.company_id != user.company_id:
        raise HTTPException(status_code=404, detail="Employee not found")
    return {"employee_id": str(emp.id), "payable_account_id": str(emp.payable_account_id) if emp.payable_account_id else None,
            "linked": _linked(db, user) is not None}


@router.put("/employees/{employee_id}/payable-account")
def set_employee_payable(employee_id: uuid.UUID, body: PayableAccountIn, db: Session = Depends(get_db), user: models.User = Depends(require_permission(_ADMIN))):
    """The Employee Master's Finance payable account (what the company owes this employee)."""
    emp = db.get(models.Employee, employee_id)
    if emp is None or emp.company_id != user.company_id:
        raise HTTPException(status_code=404, detail="Employee not found")
    emp.payable_account_id = body.payable_account_id
    db.commit()
    return {"employee_id": str(emp.id), "payable_account_id": str(emp.payable_account_id) if emp.payable_account_id else None}


# ================================================================ settings: each company links its own Finance (Administration > Integrations)
class LinkSettingsIn(BaseModel):
    enabled: bool = True
    finance_url: str
    finance_tenant: str | None = None
    api_key: str | None = None  # only when entering / replacing it; never returned
    journal_name: str | None = "Expense Claims"
    bank_account_id: uuid.UUID | None = None
    advance_account_id: uuid.UUID | None = None
    claim_tax_account_id: uuid.UUID | None = None
    business_unit_id: uuid.UUID | None = None


class LinkTestIn(BaseModel):
    finance_url: str | None = None
    finance_tenant: str | None = None
    api_key: str | None = None


def _clean_url(raw: str) -> str:
    url = (raw or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(url)
    local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
    if parsed.scheme not in ("https", "http") or not parsed.hostname or (parsed.scheme == "http" and not local) or parsed.path not in ("", "/"):
        raise HTTPException(status_code=422, detail="Finance address: https://your-finance-host (http only for localhost), with no path")
    return url


def _settings_out(db: Session, user: models.User) -> dict:
    row = db.get(models.FinanceLinkSettings, user.company_id) if _has_settings_table(db) else None
    env = _env_config()
    from .tenant_email.encryption import is_configured

    base = {"encryptionReady": is_configured(), "environmentLinked": env is not None and env.company_id == str(user.company_id)}
    if row is None:
        return {**base, "configured": False, "enabled": False}
    return {**base, "configured": True, "enabled": row.enabled, "financeUrl": row.finance_url, "financeTenant": row.finance_tenant, "keySet": bool(row.api_key_encrypted),
            "keyHint": row.key_hint, "journalName": row.journal_name, "bankAccountId": _str(row.bank_account_id), "advanceAccountId": _str(row.advance_account_id),
            "claimTaxAccountId": _str(row.claim_tax_account_id), "businessUnitId": _str(row.business_unit_id), "financeCompanyName": row.finance_company_name,
            "financeBaseCurrency": row.finance_base_currency, "hrmsBaseCurrency": _hrms_base(db, user.company_id),
            "verifiedAt": row.verified_at.isoformat() if row.verified_at else None, "updatedAt": row.updated_at.isoformat() if row.updated_at else None}


def _hrms_base(db: Session, company_id) -> str:
    from . import crud

    return crud.company_base_currency(db, company_id)


def _check_connection(db: Session, user: models.User, url: str, key: str, tenant: str) -> dict:
    """Reaches Finance with the key and checks it belongs to this same company there (HRMS and Finance share the tenant: employees, accounts and assets are the
    same records). Raises 422 with what to fix."""
    try:
        me = (Finance(url=url, key=key, tenant=tenant).call("GET", "/api/core/companies/me").get("data") or {})
    except LinkError as e:
        raise HTTPException(status_code=422, detail=f"Finance refused the key: {e}. Check the key and the tenant.") from e
    except Retry as e:
        raise HTTPException(status_code=422, detail=f"Finance could not be reached at {url}: {e}") from e
    if str(me.get("id")) != str(user.company_id):
        raise HTTPException(status_code=422, detail=f"This key belongs to '{me.get('name') or me.get('id')}' in Finance, not to this company. Create the key in this company's Finance.")
    return me


@router.get("/settings")
def get_link_settings(db: Session = Depends(get_db), user: models.User = Depends(require_permission(_SETTINGS))):
    """This company's IMPACGO Finance connection (never the key: only its last characters)."""
    return _settings_out(db, user)


@router.post("/settings/test")
def test_link_settings(body: LinkTestIn, db: Session = Depends(get_db), user: models.User = Depends(require_permission(_SETTINGS))):
    """Tries the given address / key (or the saved ones) without saving anything."""
    from .database import get_session_tenant_slug
    from .tenant_email.encryption import EncryptionConfigError, decrypt_secret

    row = db.get(models.FinanceLinkSettings, user.company_id) if _has_settings_table(db) else None
    url = _clean_url(body.finance_url or (row.finance_url if row else ""))
    key = (body.api_key or "").strip()
    if not key and row and row.api_key_encrypted:
        try:
            key = decrypt_secret(row.api_key_encrypted)
        except EncryptionConfigError as e:
            raise HTTPException(status_code=422, detail=f"The saved key cannot be read ({e}): enter it again") from e
    if not key:
        raise HTTPException(status_code=422, detail="Enter the HRMS connector key created in Finance")
    tenant = (body.finance_tenant or (row.finance_tenant if row else None) or get_session_tenant_slug(db) or settings.tenant).strip()
    me = _check_connection(db, user, url, key, tenant)
    fin_base, hrms_base = (me.get("default_currency_code") or "").upper(), _hrms_base(db, user.company_id)
    note = (f" Note: Finance books in {fin_base} but HRMS pays payroll in {hrms_base}; claims post in Finance's {fin_base}." if fin_base and fin_base != hrms_base else "")
    return {"ok": True, "financeCompany": me.get("name"), "financeBaseCurrency": fin_base or None, "hrmsBaseCurrency": hrms_base,
            "message": f"Connected to {me.get('name')} in IMPACGO Finance.{note}"}


@router.put("/settings")
def save_link_settings(body: LinkSettingsIn, db: Session = Depends(get_db), user: models.User = Depends(require_permission(_SETTINGS))):
    """Saves the connection. Enabling it (or changing the address, tenant or key) first checks it works and that the key is this company's in Finance."""
    from . import crud
    from .database import get_session_tenant_slug
    from .tenant_email.encryption import EncryptionConfigError, decrypt_secret, encrypt_secret, is_configured

    if not _has_settings_table(db):
        raise HTTPException(status_code=409, detail="The Finance link tables are not installed for this tenant (db/2026_11_17_finance_expense_claims.sql)")
    if not is_configured():
        raise HTTPException(status_code=409, detail="TENANT_EMAIL_ENCRYPTION_KEY is not set on the HRMS server: the Finance key cannot be stored safely")
    row = db.get(models.FinanceLinkSettings, user.company_id)
    url = _clean_url(body.finance_url)
    tenant = (body.finance_tenant or "").strip() or None
    new_key = (body.api_key or "").strip() or None
    if new_key is None and not (row and row.api_key_encrypted):
        raise HTTPException(status_code=422, detail="Enter the HRMS connector key created in Finance")
    try:
        key = new_key or decrypt_secret(row.api_key_encrypted)
    except EncryptionConfigError as e:
        raise HTTPException(status_code=422, detail=f"The saved key cannot be read ({e}): enter it again") from e
    changed_connection = row is None or new_key is not None or row.finance_url != url or (row.finance_tenant or None) != tenant
    me = None
    if body.enabled and (changed_connection or not (row and row.enabled)):
        me = _check_connection(db, user, url, key, tenant or get_session_tenant_slug(db) or settings.tenant)
    elif body.enabled:
        try:  # unchanged connection: refresh Finance's company name and base currency, without blocking the save
            me = (Finance(url=url, key=key, tenant=tenant or get_session_tenant_slug(db) or settings.tenant).call("GET", "/api/core/companies/me").get("data") or None)
            if me is not None and str(me.get("id")) != str(user.company_id):
                me = None
        except (LinkError, Retry):
            me = None
    if row is None:
        row = models.FinanceLinkSettings(company_id=user.company_id)
        db.add(row)
    row.enabled, row.finance_url, row.finance_tenant = body.enabled, url, tenant
    if new_key is not None:
        row.api_key_encrypted, row.key_hint = encrypt_secret(new_key), new_key[-4:]
    row.journal_name = (body.journal_name or "").strip() or "Expense Claims"
    row.bank_account_id, row.advance_account_id = body.bank_account_id, body.advance_account_id
    row.claim_tax_account_id, row.business_unit_id = body.claim_tax_account_id, body.business_unit_id
    if me is not None:
        row.finance_company_name, row.verified_at = me.get("name"), datetime.datetime.now(datetime.timezone.utc)
        row.finance_base_currency = (me.get("default_currency_code") or "").upper()[:3] or None
    row.updated_by, row.updated_at = user.id, datetime.datetime.now(datetime.timezone.utc)
    crud.create_audit_log(db, user.company_id, user.id, "update", "finance_link_settings", user.company_id,
                          changes={"enabled": str(body.enabled), "finance_url": url, "key_replaced": str(new_key is not None)})
    db.commit()
    return _settings_out(db, user)


@router.delete("/settings", status_code=204)
def delete_link_settings(db: Session = Depends(get_db), user: models.User = Depends(require_permission(_SETTINGS))):
    """Disconnects: the saved key is deleted. Documents already in Finance stay there; new changes wait until the company is linked again."""
    from . import crud

    row = db.get(models.FinanceLinkSettings, user.company_id) if _has_settings_table(db) else None
    if row is not None:
        db.delete(row)
        crud.create_audit_log(db, user.company_id, user.id, "delete", "finance_link_settings", user.company_id)
        db.commit()


@router.get("/business-units")
def business_units(db: Session = Depends(get_db), user: models.User = Depends(require_permission(_SETTINGS))):
    """Finance's business units, for the settings' default business unit."""
    cfg = _own(db, user)
    try:
        d = Finance.of(cfg).call("GET", "/api/core/business-units?limit=200")
    except (LinkError, Retry) as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    data = d.get("data")
    items = data.get("items", []) if isinstance(data, dict) else (data or [])
    return {"items": [{"id": b["id"], "code": b.get("code"), "name": b.get("name")} for b in items if b.get("is_active", True)]}


# ================================================================ currencies & FX (from Finance; HRMS keeps no rates)
@router.get("/currencies")
def currencies(day: datetime.date | None = None, db: Session = Depends(get_db), user: models.User = Depends(_current_user())):
    """The currencies a claim may be entered in -- IMPACGO Finance's active currencies -- with Finance's base currency and each one's rate into it as of
    [day] (default today), for the claim form's "about ₹ ..." hint. 409 when the company is not linked (the form then keeps HRMS's own list)."""
    cfg = _own(db, user)
    fin = Finance.of(cfg)
    when = str(day or datetime.date.today())
    try:
        base = fin.base_currency()
        items = []
        for code, c in sorted(fin.currencies().items()):
            r = fin.rate(code, when)
            items.append({"code": code, "name": c.get("name"), "symbol": c.get("symbol"), "rate": str(r[0]) if r else None, "rateDate": r[1] if r else None})
    except (LinkError, Retry) as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"base": base, "hrmsBase": _hrms_base(db, user.company_id), "asOf": when, "items": items}
