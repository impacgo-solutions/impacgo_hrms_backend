"""Expense claims -> IMPACGO Finance: the journal a claim becomes, and how Finance's answers are read. No database and no network.
Run:  DATABASE_URL=postgresql://x:y@127.0.0.1:1/none python -m pytest -q --noconftest tests/test_finance_link.py"""
import io
import json
import os
import urllib.error
import uuid
from decimal import Decimal
D = Decimal

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://x:y@127.0.0.1:1/none")
from app import finance_link as fl  # noqa: E402

FLIGHT, HOTEL, TAXI, PAYABLE, ADV, TAX = (str(uuid.uuid4()) for _ in range(6))


def claim(**kw):
    c = {"id": uuid.uuid4(), "claim_no": "EXP-0007", "posting_date": "2026-11-17", "currency_code": "INR", "base_currency": "INR", "employee_payable": PAYABLE,
         "total_taxes": 0, "advance_amount": 0, "total_amount": 16000,
         "lines": [{"category": "Flight", "gl_account_id": FLIGHT, "amount": 10000}, {"category": "Hotel", "gl_account_id": HOTEL, "amount": 5000},
                   {"category": "Taxi", "gl_account_id": TAXI, "amount": 1000}]}
    c.update(kw)
    return c


def test_each_row_debits_its_categorys_gl_and_the_employees_payable_account_is_credited():
    j = fl.claim_journal(claim(), {})
    dr = [(l["account_id"], l["debit"]) for l in j["lines"] if Decimal(l["debit"])]
    cr = [(l["account_id"], l["credit"]) for l in j["lines"] if Decimal(l["credit"])]
    assert dr == [(FLIGHT, "10000.00"), (HOTEL, "5000.00"), (TAXI, "1000.00")]
    assert cr == [(PAYABLE, "16000.00")] and j["total"] == Decimal("16000.00")


def test_categories_may_share_one_gl_account():
    c = claim(lines=[{"category": "Taxi", "gl_account_id": FLIGHT, "amount": 400}, {"category": "Car", "gl_account_id": FLIGHT, "amount": 600}], total_amount=1000)
    assert [l["account_id"] for l in fl.claim_journal(c, {})["lines"]] == [FLIGHT, FLIGHT, PAYABLE]


def test_taxes_and_an_advance_take_their_accounts_and_the_payable_is_the_net():
    c = claim(total_taxes=500, advance_amount=2000, total_amount=14500)
    j = fl.claim_journal(c, {"claim_tax": TAX, "advance": ADV})
    acc = {l["account_id"]: (l["debit"], l["credit"]) for l in j["lines"]}
    assert acc[TAX] == ("500.00", "0") and acc[ADV] == ("0", "2000.00") and acc[PAYABLE] == ("0", "14500.00")


@pytest.mark.parametrize("change, words", [
    ({"lines": [{"category": "Flight", "gl_account_id": None, "amount": 16000}]}, "Expense category 'Flight' has no GL account"),
    ({"employee_payable": None}, "no payable account"),
    ({"total_taxes": 100, "total_amount": 16100}, "claim-tax GL account"),
    ({"advance_amount": 1000, "total_amount": 15000}, "employee-advance GL account"),
    ({"currency_code": "USD"}, "not the company's INR"),
    ({"total_amount": 15999}, "does not add up"),
])
def test_what_a_person_must_fix_is_named(change, words):
    with pytest.raises(fl.LinkError, match=words):
        fl.claim_journal(claim(**change), {})


def test_the_request_is_a_draft_while_pending_numbered_by_the_expense_claims_journal_and_its_key_follows_the_content():
    j = fl.claim_journal(claim(), {})
    b = fl.gateway_body(doctype="expense_claim", source_id="abc", source_no="EXP-0007", key="k", posting_date="2026-11-17", remarks="r", lines=j["lines"], draft=True,
                        journal_name="Expense Claims")
    assert b["draft"] is True and b["journal_name"] == "Expense Claims" and b["source_module"] == "hrms"
    assert "draft" not in fl.gateway_body(doctype="expense_claim", source_id="abc", source_no=None, key="k", posting_date="2026-11-17", remarks="r", lines=j["lines"])
    k1 = fl.content_key(j["lines"], "2026-11-17")
    assert k1 == fl.content_key(j["lines"], "2026-11-17") and k1 != fl.content_key(fl.claim_journal(claim(lines=[{"category": "Flight", "gl_account_id": FLIGHT, "amount": 16000}]), {})["lines"], "2026-11-17")


def _opener(code=200, body=None, exc=None):
    def open_(req, timeout=30):
        if exc:
            raise exc
        if code >= 400:
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(json.dumps(body or {}).encode()))
        return io.BytesIO(json.dumps(body or {}).encode())
    return open_


def test_finances_refusal_is_for_a_person_and_an_outage_is_retried():
    fin = lambda **k: fl.Finance(url="https://f", key="k", tenant="finance", opener=_opener(**k))
    assert fin(body={"data": {"id": "t1"}}).call("POST", "/x", {})["data"]["id"] == "t1"
    with pytest.raises(fl.LinkError, match="Accounting period"):
        fin(code=422, body={"message": "rejected", "data": {"errors": [{"field": "posting_date", "error": "Accounting period 'Nov' is closed"}]}}).call("POST", "/x", {})
    with pytest.raises(fl.Retry):
        fin(code=503, body={"message": "maintenance"}).call("POST", "/x", {})
    with pytest.raises(fl.Retry):
        fin(exc=urllib.error.URLError("refused")).call("POST", "/x", {})


def test_the_expense_debits_carry_the_business_unit_the_payable_does_not():
    bu = str(uuid.uuid4())
    j = fl.claim_journal(claim(business_unit_id=bu, total_taxes=500, total_amount=16500), {"claim_tax": TAX})
    assert all(l.get("business_unit_id") == bu for l in j["lines"] if Decimal(l["debit"]))
    assert all("business_unit_id" not in l for l in j["lines"] if Decimal(l["credit"]))


# ---------------------------------------------------------------- asset holders (no ledger effect)
class _Asset:
    def __init__(self, linked=None, reported=None):
        self.id, self.finance_asset_id, self.finance_custodian_asset_id, self.finance_error = uuid.uuid4(), linked, reported, "old error"


class _Fin:
    def __init__(self, fail_404_on=None):
        self.calls, self.fail_404_on = [], fail_404_on

    def call(self, method, path, body=None):
        if self.fail_404_on and self.fail_404_on in path:
            raise fl.LinkError("Finance refused it (404): FixedAsset not found")
        self.calls.append((method, path, body))
        return {"data": {}}


def test_the_holder_of_a_linked_asset_is_reported_to_its_finance_fixed_asset(monkeypatch):
    emp = uuid.uuid4()
    monkeypatch.setattr(fl, "current_holder", lambda db, aid: emp)
    a, fin = _Asset(linked="fa-1"), _Fin()
    fl.sync_asset_holder(None, a, fin)
    assert fin.calls == [("PUT", "/api/fin/fixed-assets/fa-1/custodian", {"custodian_employee_id": str(emp), "source": "hrms"})]
    assert a.finance_custodian_asset_id == "fa-1" and a.finance_error is None


def test_a_returned_asset_has_nobody_and_relinking_clears_the_old_finance_asset_first(monkeypatch):
    monkeypatch.setattr(fl, "current_holder", lambda db, aid: None)
    a, fin = _Asset(linked="fa-1", reported="fa-1"), _Fin()
    fl.sync_asset_holder(None, a, fin)  # taken back: HRMS had reported the holder, so it clears it
    assert fin.calls == [("PUT", "/api/fin/fixed-assets/fa-1/custodian", {"custodian_employee_id": None, "source": "hrms"})]
    a, fin = _Asset(linked="fa-2", reported="fa-1"), _Fin()
    fl.sync_asset_holder(None, a, fin)  # relinked while nobody holds it: the old asset is cleared, the new one keeps the holder Finance records
    assert [(p, b["custodian_employee_id"]) for _, p, b in fin.calls] == [("/api/fin/fixed-assets/fa-1/custodian", None)]
    assert a.finance_custodian_asset_id is None


def test_linking_an_asset_hrms_has_not_assigned_leaves_the_holder_finance_records(monkeypatch):
    monkeypatch.setattr(fl, "current_holder", lambda db, aid: None)
    a, fin = _Asset(linked="fa-1"), _Fin()
    fl.sync_asset_holder(None, a, fin)
    assert fin.calls == [] and a.finance_custodian_asset_id is None and a.finance_error is None


def test_unlinking_clears_the_holder_reported_and_a_finance_asset_already_gone_is_nothing_to_clear(monkeypatch):
    monkeypatch.setattr(fl, "current_holder", lambda db, aid: uuid.uuid4())
    a, fin = _Asset(linked=None, reported="fa-1"), _Fin()
    fl.sync_asset_holder(None, a, fin)
    assert fin.calls == [("PUT", "/api/fin/fixed-assets/fa-1/custodian", {"custodian_employee_id": None, "source": "hrms"})] and a.finance_custodian_asset_id is None
    fl.report_holder(_Fin(fail_404_on="fa-9"), "fa-9", None)  # no error
    with pytest.raises(fl.LinkError, match="404"):
        fl.report_holder(_Fin(fail_404_on="fa-9"), "fa-9", uuid.uuid4())  # setting a holder on a missing asset is for a person to fix


# ---------------------------------------------------------------- per-company settings
@pytest.mark.parametrize("url, ok", [
    ("https://finance.customer.com", True), ("https://finance.customer.com/", True), ("http://127.0.0.1:8099", True), ("http://localhost:8099", True),
    ("http://finance.customer.com", False), ("ftp://finance.customer.com", False), ("https://finance.customer.com/api", False), ("finance.customer.com", False), ("", False),
])
def test_the_finance_address_must_be_https_except_on_this_machine(url, ok):
    from fastapi import HTTPException

    if ok:
        assert fl._clean_url(url) == url.rstrip("/")
    else:
        with pytest.raises(HTTPException):
            fl._clean_url(url)


def test_each_company_talks_to_its_own_finance_with_its_own_key():
    a = fl.LinkConfig(company_id="c1", url="https://fin-a.example", key="ka", tenant="tenant_a", bank="bank-a")
    b = fl.LinkConfig(company_id="c2", url="https://fin-b.example", key="kb", tenant="tenant_b")
    seen = []

    def opener(req, timeout=30):
        seen.append((req.full_url, req.get_header("X-api-key"), req.get_header("X-tenant")))
        return io.BytesIO(b'{"data": {}}')

    for cfg in (a, b):
        f = fl.Finance(cfg=cfg, opener=opener)
        f.call("GET", "/api/core/companies/me")
    assert seen == [("https://fin-a.example/api/core/companies/me", "ka", "tenant_a"), ("https://fin-b.example/api/core/companies/me", "kb", "tenant_b")]
    assert fl.Finance.of(a).cfg.bank == "bank-a"


# ---------------------------------------------------------------- currencies & FX: Finance's rates, none of HRMS's own
USD_ID = str(uuid.uuid4())


def test_a_claim_in_another_currency_posts_in_the_base_currency_at_finances_rate_and_keeps_the_original_on_each_line():
    c = claim(currency_code="USD", exchange_rate=D("83.5"), total_taxes=50, advance_amount=200, total_amount=15850)
    j = fl.claim_journal(c, {"claim_tax": TAX, "advance": ADV})
    acc = {l["account_id"]: l for l in j["lines"]}
    assert [l["debit"] for l in j["lines"][:3]] == ["835000.00", "417500.00", "83500.00"], "rows converted at Finance's rate"
    assert acc[TAX]["debit"] == "4175.00" and acc[ADV]["credit"] == "16700.00" and acc[PAYABLE]["credit"] == "1323475.00"
    assert all("currency_id" not in l for l in j["lines"]), "posted in the base currency: Finance's ledger holds exact amounts"
    assert j["lines"][0]["description"] == "Flight · USD 10000.00 @ 83.5" and "USD 15850.00 @ 83.5" in acc[PAYABLE]["description"]
    assert j["base_payable"] == D("1323475.00") and "business_unit_id" not in acc[PAYABLE]


def test_without_finances_rate_the_claim_is_not_posted_and_says_what_to_add():
    with pytest.raises(fl.LinkError, match="Currency & FX"):
        fl.claim_journal(claim(currency_code="USD"), {})


def test_the_base_amount_is_rounded_like_finance_rounds_a_converted_line():
    assert fl.base_amount("100.00", D("83.5")) == D("8350.00")
    assert fl.base_amount("33.335", D("1")) == D("33.34") and fl.base_amount("10.005", D("1")) == D("10.00")  # half-even, Decimal's default, as Finance
    assert fl.base_amount("16000", None) == D("16000.00")


def _fx_finance(rates: dict, base="INR", currencies=("INR", "USD", "EUR")):
    ids = {c: f"id-{c}" for c in currencies}

    def opener(req, timeout=30):
        url = req.full_url
        if url.endswith("/api/core/companies/me"):
            return io.BytesIO(json.dumps({"data": {"default_currency_code": base}}).encode())
        if "/api/core/currencies?" in url:
            return io.BytesIO(json.dumps({"data": {"items": [{"id": i, "code": c, "name": c, "is_active": True} for c, i in ids.items()]}}).encode())
        if "/exchange-rates?" in url:
            q = dict(p.split("=") for p in url.split("?", 1)[1].split("&"))
            pair = (q["from_currency_id"][3:], q["to_currency_id"][3:])
            if pair in rates:
                return io.BytesIO(json.dumps({"data": {"rate": rates[pair], "effective_date": "2026-11-06"}}).encode())
            raise urllib.error.HTTPError(url, 404, "x", {}, io.BytesIO(b'{"message": "ExchangeRate not found"}'))
        raise AssertionError(url)

    return fl.Finance(cfg=fl.LinkConfig(company_id="c", url="https://f", key="k", tenant="t"), opener=opener)


def test_finances_rate_is_read_directly_or_from_the_inverse_pair_and_a_missing_one_is_named():
    fin = _fx_finance({("USD", "INR"): "83.5", ("INR", "EUR"): "0.0125"})
    assert fin.rate("USD", "2026-11-17") == (D("83.5"), "2026-11-06")
    assert fin.rate("EUR", "2026-11-17") == (D("80.00000000"), "2026-11-06"), "only INR->EUR is recorded: Finance uses its inverse"
    assert fin.rate("INR", "2026-11-17") == (D(1), "2026-11-17")
    fx = fl._claim_fx(fin, "USD", "2026-11-17")
    assert fx["currency_id"] == "id-USD" and fx["exchange_rate"] == D("83.5") and fx["base_currency"] == "INR"
    with pytest.raises(fl.LinkError, match="not set up in IMPACGO Finance"):
        fl._claim_fx(fin, "JPY", "2026-11-17")
    with pytest.raises(fl.LinkError, match="no GBP to INR exchange rate"):
        fl._claim_fx(_fx_finance({}, currencies=("INR", "GBP")), "GBP", "2026-11-17")
    assert fl._claim_fx(fin, "INR", "2026-11-17") == {"base_currency": "INR"}


def test_conversion_rounding_is_taken_by_the_payable_so_the_journal_balances_exactly_and_the_payment_pays_exactly_that():
    # 420.00 + 265.50 + 33.35 USD at 83.5: the rows convert to 35,070.00 + 22,169.25 + 2,784.72 (33.35 x 83.5 = 2,784.725, half-even) = 60,023.97, while
    # 718.85 x 83.5 = 60,023.975 would round to .98: the employee is owed what the rows convert to, and the journal balances to the paisa
    c = claim(currency_code="USD", exchange_rate=D("83.5"), total_amount=D("718.85"),
              lines=[{"category": "Flight", "gl_account_id": FLIGHT, "amount": D("420.00")}, {"category": "Hotel", "gl_account_id": HOTEL, "amount": D("265.50")},
                     {"category": "Taxi", "gl_account_id": TAXI, "amount": D("33.35")}])
    j = fl.claim_journal(c, {})
    assert [l["debit"] for l in j["lines"][:3]] == ["35070.00", "22169.25", "2784.72"]
    assert j["lines"][-1]["account_id"] == PAYABLE and j["lines"][-1]["credit"] == "60023.97" and len(j["lines"]) == 4
    assert sum(D(l["debit"]) - D(l["credit"]) for l in j["lines"]) == 0
    assert j["base_payable"] == D("60023.97") == j["total"]
