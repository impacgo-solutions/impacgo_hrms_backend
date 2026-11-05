"""Standard (ISO 4217) currencies a project budget can be entered in -- the
Create / Edit Project "Currency" field. A budget is stored as its amount plus
this three-letter code (pm_project_budgets.currency_code); nothing is ever
converted between currencies. The frontend keeps the same list in
lib/core/utils/currencies.dart."""

from __future__ import annotations

# code -> (name, symbol)
CURRENCIES: dict[str, tuple[str, str]] = {
    "INR": ("Indian Rupee", "₹"),
    "USD": ("US Dollar", "$"),
    "EUR": ("Euro", "€"),
    "GBP": ("British Pound", "£"),
    "AED": ("UAE Dirham", "AED"),
    "SAR": ("Saudi Riyal", "SAR"),
    "QAR": ("Qatari Riyal", "QAR"),
    "KWD": ("Kuwaiti Dinar", "KWD"),
    "OMR": ("Omani Rial", "OMR"),
    "BHD": ("Bahraini Dinar", "BHD"),
    "SGD": ("Singapore Dollar", "S$"),
    "MYR": ("Malaysian Ringgit", "RM"),
    "THB": ("Thai Baht", "฿"),
    "IDR": ("Indonesian Rupiah", "Rp"),
    "PHP": ("Philippine Peso", "₱"),
    "VND": ("Vietnamese Dong", "₫"),
    "JPY": ("Japanese Yen", "¥"),
    "CNY": ("Chinese Yuan", "CN¥"),
    "HKD": ("Hong Kong Dollar", "HK$"),
    "KRW": ("South Korean Won", "₩"),
    "AUD": ("Australian Dollar", "A$"),
    "NZD": ("New Zealand Dollar", "NZ$"),
    "CAD": ("Canadian Dollar", "C$"),
    "CHF": ("Swiss Franc", "CHF"),
    "SEK": ("Swedish Krona", "kr"),
    "NOK": ("Norwegian Krone", "kr"),
    "DKK": ("Danish Krone", "kr"),
    "ZAR": ("South African Rand", "R"),
    "BRL": ("Brazilian Real", "R$"),
    "MXN": ("Mexican Peso", "MX$"),
    "LKR": ("Sri Lankan Rupee", "Rs"),
    "NPR": ("Nepalese Rupee", "Rs"),
    "BDT": ("Bangladeshi Taka", "৳"),
    "PKR": ("Pakistani Rupee", "Rs"),
}


def normalize_code(code: str | None) -> str | None:
    """'usd ' -> 'USD'; None / blank -> None; ValueError for an unknown code."""
    if code is None or not str(code).strip():
        return None
    c = str(code).strip().upper()
    if c not in CURRENCIES:
        raise ValueError(f"'{code}' is not a supported currency.")
    return c


def symbol(code: str | None) -> str:
    return CURRENCIES.get((code or "").upper(), ("", code or ""))[1]


def format_amount(amount: float | None, code: str | None) -> str:
    """'$12,500' / '€9,800' / 'AED 4,000'; '—' with no amount. With no
    currency recorded the plain number is shown (no currency is assumed)."""
    if amount is None:
        return "—"
    number = f"{amount:,.2f}" if amount % 1 else f"{amount:,.0f}"
    if not code:
        return number
    sym = symbol(code)
    return f"{sym} {number}" if sym.isalpha() else f"{sym}{number}"
