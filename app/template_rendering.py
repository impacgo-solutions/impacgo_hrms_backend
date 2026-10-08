"""Shared HTML+CSS document-template rendering engine -- the common core
behind every "Documents > Templates" template feature in this app (Payslip
Template, Offer Letter Template, and any future one). Extracted out of
payslip_html_renderer.py so every template type genuinely shares ONE
placeholder-substitution/print-CSS/PDF pipeline instead of each duplicating
its own near-identical copy -- exactly the "share everything the same way"
requirement the Offer Letter Template feature was built to satisfy.

Placeholder substitution is a plain, fixed-vocabulary find-and-replace
(never a real templating engine like Jinja2) so an admin authoring
{{employee_name}} or {{candidate_name}} can never smuggle in arbitrary
Python/expression evaluation -- the substitution surface is exactly
whatever keys the caller's own build_*_placeholders() function returns,
nothing else. Every substituted value is HTML-escaped before insertion.

render_template_html is the SINGLE rendering path every template type's
Live Preview endpoint and PDF generation both call, with the same
placeholder map either way, so preview and the downloaded PDF can never
drift apart on data OR layout. render_html_to_pdf converts the result to a
PDF using real headless Chromium via Playwright -- the same rendering
engine class the Live Preview's browser iframe already uses, so CSS Grid/
Flexbox, fonts, colors, and spacing all come out identical to what was
previewed, not approximated by a lightweight HTML-to-PDF library.
"""

import base64
import concurrent.futures
import html
import logging
import mimetypes
import queue
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit

from . import storage

logger = logging.getLogger(__name__)

# Matches {{ any_key }} / {{any_key}} -- whitespace-tolerant, single fixed
# token shape. Anything not in the placeholder map is left untouched rather
# than silently blanked, so an admin can immediately spot a typo'd
# placeholder in the live preview instead of it vanishing.
PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")

# M-34: admin-authored template HTML is cleaned with an ALLOWLIST sanitiser
# (nh3 / ammonia) on every render -- preview, PDF and email alike -- instead
# of the old regex blocklist (bypassable with entity-encoded javascript:,
# <object data=>, <meta http-equiv=refresh>, ...). Only the formatting tags
# and attributes document templates use survive; scripts, iframes, objects,
# embeds, forms, meta/base/link, event handlers and non-http(s)/mailto/tel
# URLs (data: only for <img src>) are dropped. validate_template_html still
# refuses obviously dangerous input at save time so the author gets a clear
# message instead of silently stripped markup.
import nh3  # noqa: E402

_ALLOWED_TAGS = {
    "a", "abbr", "address", "article", "aside", "b", "bdi", "bdo", "blockquote", "br", "caption",
    "center", "cite", "code", "col", "colgroup", "dd", "del", "details", "dfn", "div", "dl", "dt",
    "em", "figcaption", "figure", "font", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header",
    "hr", "i", "img", "ins", "kbd", "label", "li", "main", "mark", "nav", "ol", "p", "pre", "q",
    "s", "section", "small", "span", "strike", "strong", "sub", "summary", "sup", "table", "tbody",
    "td", "tfoot", "th", "thead", "time", "tr", "u", "ul", "wbr",
}
_GENERIC_ATTRS = {"class", "id", "style", "title", "align", "valign", "dir", "lang", "role",
                  "width", "height", "bgcolor", "border"}
_ALLOWED_ATTRS = {
    "*": _GENERIC_ATTRS,
    "a": {"href", "target", "name"},
    "img": {"src", "alt"},
    "table": {"cellpadding", "cellspacing", "summary"},
    "td": {"colspan", "rowspan", "nowrap"},
    "th": {"colspan", "rowspan", "scope", "nowrap"},
    "col": {"span"},
    "colgroup": {"span"},
    "font": {"color", "size", "face"},
    "ol": {"start", "type"},
    "ul": {"type"},
    "li": {"value"},
    "time": {"datetime"},
}
_ALLOWED_URL_SCHEMES = {"http", "https", "mailto", "tel", "data"}
# data: only as an inline image; every scheme check runs on the entity-
# decoded, whitespace/control-stripped value.
_DATA_IMAGE_RE = re.compile(r"^data:image/(png|jpe?g|gif|webp|bmp);", re.IGNORECASE)
_URL_ATTRS = {"href", "src"}
_UNSAFE_CSS_RE = re.compile(r"expression\s*\(|javascript\s*:|vbscript\s*:|behavior\s*:|-moz-binding", re.IGNORECASE)


def _normalise_url(value: str) -> str:
    return re.sub(r"[\x00-\x20]+", "", html.unescape(value)).lower()


def _attribute_filter(tag: str, attr: str, value: str) -> str | None:
    if attr in _URL_ATTRS:
        v = _normalise_url(value)
        if v.startswith("data:") and not (tag == "img" and attr == "src" and _DATA_IMAGE_RE.match(v)):
            return None
    if attr == "style" and _UNSAFE_CSS_RE.search(html.unescape(value)):
        return None
    return value


def sanitize_template_html(html_body: str) -> str:
    """Allowlist-clean admin-authored template HTML (see comment above).
    {{placeholders}} are plain text / relative URLs and survive untouched."""
    if not html_body:
        return html_body or ""
    attrs = {k: set(v) for k, v in _ALLOWED_ATTRS.items()}
    return nh3.clean(
        html_body,
        tags=_ALLOWED_TAGS,
        clean_content_tags={"script", "style", "noscript", "iframe", "object", "embed", "template", "textarea", "select"},
        attributes=attrs,
        attribute_filter=_attribute_filter,
        url_schemes=_ALLOWED_URL_SCHEMES,
        generic_attribute_prefixes={"data-"},
        strip_comments=True,
        link_rel="noopener noreferrer",
    )


def sanitize_template_css(css_styles: str) -> str:
    """The template CSS goes inside <style>: it can never close that element
    ("<" is CSS-escaped) or carry script-bearing constructs."""
    css = (css_styles or "").replace("<", "\\3C ")
    return _UNSAFE_CSS_RE.sub("/* removed */", css)


# Save-time refusal with a clear message (render-time sanitising above is
# the actual enforcement). Matched against the raw text AND its
# entity-decoded, whitespace-stripped form so encodings don't slip through.
_DISALLOWED_HTML_PATTERNS = [
    re.compile(r"<\s*(script|iframe|object|embed|applet|frame|frameset|base|link|meta|form|svg|math)\b", re.IGNORECASE),
    _SCHEME_PATTERN := re.compile(r"(javascript|vbscript|livescript)\s*:", re.IGNORECASE),
    re.compile(r"data\s*:\s*text/html", re.IGNORECASE),
    re.compile(r"[\s\"'/]on[a-z]+\s*=", re.IGNORECASE),  # onclick=, onerror=, ...
    re.compile(r"expression\s*\(|-moz-binding|behavior\s*:", re.IGNORECASE),
    re.compile(r"<\s*/\s*style", re.IGNORECASE),
]

# A fully valid, 1x1 transparent PNG -- the fallback for a {{*_logo}}
# placeholder whenever the company hasn't uploaded a logo, so an admin's
# <img src="{{company_logo}}"> never renders a broken-image icon on either
# the Live Preview or the PDF, and never needs a fabricated placeholder logo.
# (The previous bytes here decoded to an OPAQUE BLACK pixel, not a
# transparent one -- an alpha=255 LA-mode PNG despite the name/docstring --
# which is why every payslip/offer-letter without an uploaded logo showed a
# solid black box instead of nothing. This one is genuine RGBA alpha=0.)
_TRANSPARENT_PIXEL_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABpfZF"
    "QAAAAABJRU5ErkJggg=="
)
TRANSPARENT_PIXEL_DATA_URL = f"data:image/png;base64,{_TRANSPARENT_PIXEL_PNG_B64}"

# Baseline print stylesheet injected BEFORE the admin's own css_styles, so
# their more-specific rules still win the cascade wherever they've
# customized something -- this only supplies sane A4/print defaults, never
# overrides an admin's intentional choice.
#
# - `@page { size: A4; margin: 0 }` is what actually keeps a normal-length
#   document to one page: the page itself has no browser-imposed margin,
#   so the template's own content controls its box sizing directly.
# - print-color-adjust (+ the -webkit- prefix Chromium still wants) forces
#   background colors/images to actually appear in the PDF -- browsers
#   omit them by default when printing unless told otherwise.
# - page-break-inside: avoid on table/tr and common section classes stops
#   a details block/salary table/signature block from splitting mid-row/
#   mid-section if the content ever does overflow onto a second page.
BASE_PRINT_CSS = """
@page { size: A4; margin: 0; }
html, body {
  margin: 0;
  padding: 0;
  -webkit-print-color-adjust: exact;
  print-color-adjust: exact;
  color-adjust: exact;
}
*, *::before, *::after {
  -webkit-print-color-adjust: exact;
  print-color-adjust: exact;
  color-adjust: exact;
  box-sizing: border-box;
}
img { max-width: 100%; }
table { border-collapse: collapse; page-break-inside: avoid; }
tr, td, th { page-break-inside: avoid; }
.payslip, .payslip-page, .offer-letter, .offer-letter-page,
.no-break, .avoid-break,
.employee-details, .payment-details, .salary-table, .net-pay,
.attendance-summary, .candidate-details, .offer-details,
.signature, .footer {
  page-break-inside: avoid;
}
"""


def validate_template_html(html_body: str, css_styles: str) -> str | None:
    """Returns a human-readable error message if html_body/css_styles
    contain disallowed constructs, else None. Called before every save,
    for every template type."""
    html_body, css_styles = html_body or "", css_styles or ""
    decoded = html.unescape(html_body)
    squashed = re.sub(r"[\x00-\x20]+", "", decoded)
    for pattern in _DISALLOWED_HTML_PATTERNS:
        # The whitespace-squashed form only matters for split URL schemes
        # ("java\tscript:"); other patterns would false-positive on it.
        texts = (html_body, decoded, css_styles) + ((squashed,) if pattern is _SCHEME_PATTERN else ())
        if any(pattern.search(text) for text in texts):
            return (
                "Scripts, iframes, objects/embeds, forms, meta/link/base tags, javascript: "
                "links and inline event handlers (onclick, onerror, etc.) aren't allowed "
                "in a document template."
            )
    return None


def num_to_words_indian(n: int) -> str:
    """Indian numbering system (Crore/Lakh/Thousand) number-to-words --
    used only for amounts already computed from real backend data (net
    pay, offered CTC, etc.); never applied to a fabricated number."""
    ones = [
        "", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
        "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
        "Seventeen", "Eighteen", "Nineteen",
    ]
    tens = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]

    def two_digits(n: int) -> str:
        if n < 20:
            return ones[n]
        t, o = divmod(n, 10)
        return f"{tens[t]} {ones[o]}".strip()

    def three_digits(n: int) -> str:
        if n >= 100:
            rest = n % 100
            head = f"{ones[n // 100]} Hundred"
            return f"{head} {two_digits(rest)}".strip() if rest else head
        return two_digits(n)

    if n == 0:
        return "Zero"
    parts = []
    crore, n = divmod(n, 1_00_00_000)
    if crore:
        parts.append(f"{three_digits(crore)} Crore" if crore >= 100 else f"{two_digits(crore)} Crore")
    lakh, n = divmod(n, 1_00_000)
    if lakh:
        parts.append(f"{two_digits(lakh)} Lakh")
    thousand, n = divmod(n, 1_000)
    if thousand:
        parts.append(f"{two_digits(thousand)} Thousand")
    if n:
        parts.append(three_digits(n))
    return " ".join(p for p in parts if p)


def amount_in_words(amount: float) -> str:
    rupees = int(amount)
    paise = round((amount - rupees) * 100)
    words = f"Rupees {num_to_words_indian(rupees)}"
    if paise:
        words += f" and {num_to_words_indian(paise)} Paise"
    return f"{words} Only"


def resolve_company_logo(company) -> str:
    """A placeholder like {{company_logo}} must always be a usable
    <img src="..."> value that never shows a broken-image icon:
    - No logo uploaded (logo_url is None/empty): a real, valid 1x1
      transparent PNG data URL, so an <img> tag referencing it renders
      nothing visible rather than a broken-image icon.
    - A locally-stored file (the same /media/<entity>/<id>/<file> convention
      storage.save_uploaded_file uses): embedded as a base64 data URL, since
      PDF/preview rendering has no HTTP base URL to resolve a relative path
      against.
    - An absolute http(s) URL: passed through as-is, and registered as the
      ONLY kind of remote request the PDF renderer will let Chromium make
      (an image GET of exactly this URL) -- every other network request
      from template HTML is blocked (P7 / SSRF, see render_html_to_pdf)."""
    logo_url = getattr(company, "logo_url", None)
    if not logo_url:
        return TRANSPARENT_PIXEL_DATA_URL
    if logo_url.startswith("http://") or logo_url.startswith("https://"):
        allow_remote_image(logo_url)
        return logo_url
    if logo_url.startswith("/media/"):
        try:
            path = storage.uploads_root() / logo_url.removeprefix("/media/")
            data = storage.winlong_path(path).read_bytes()
        except OSError:
            return TRANSPARENT_PIXEL_DATA_URL
        mime = mimetypes.guess_type(Path(logo_url).name)[0] or "image/png"
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    return TRANSPARENT_PIXEL_DATA_URL


def substitute_placeholders(
    html_body: str, placeholders: dict[str, str], verbatim_keys: tuple[str, ...] = ()
) -> str:
    """Substitutes every {{key}} token with its HTML-escaped value (unknown
    keys are left as-is) -- the substitution core shared by
    render_template_html (full documents: payslip/offer-letter/exit-letter)
    and email_service.render_email (an email's content fragment, which gets
    wrapped in layout.html rather than its own <html> document, so it calls
    this directly instead of render_template_html).

    verbatim_keys names placeholders whose value is already-escaped HTML
    markup (e.g. a pre-rendered table-rows fragment) rather than plain
    text, inserted as-is instead of being escaped again."""

    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in placeholders:
            return match.group(0)
        value = placeholders[key]
        if key in verbatim_keys:
            return value
        return html.escape(str(value))

    # M-34: the admin-authored template is allowlist-sanitised BEFORE
    # substitution; substituted values are escaped (or trusted server HTML
    # for verbatim_keys).
    return PLACEHOLDER_RE.sub(replace, sanitize_template_html(html_body))


def render_template_html(
    html_body: str, css_styles: str, placeholders: dict[str, str], verbatim_keys: tuple[str, ...] = ()
) -> str:
    """Wraps substitute_placeholders(...) in a full HTML document with the
    baseline print CSS + the template's own CSS inlined -- the single
    rendering path shared by every template type's Live Preview endpoint
    and PDF generation, so preview and the downloaded document can never
    drift apart on data OR layout."""
    body = substitute_placeholders(html_body, placeholders, verbatim_keys)
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        f"<style>{BASE_PRINT_CSS}\n{sanitize_template_css(css_styles)}</style></head>"
        f"<body>{body}</body></html>"
    )


# ---------------------------------------------------------------------------
# PDF rendering (FUNC-01 / P7)
#
# Previously every PDF launched (and tore down) a brand-new headless
# Chromium -- ~1-2 s and 100+ MB per document -- with unrestricted network
# access, so admin-authored template HTML could make the server's browser
# fetch arbitrary URLs (SSRF). Now:
#   * ONE browser is started lazily and reused. Playwright's sync API is
#     bound to the thread that created it, so the browser lives on a single
#     dedicated worker thread; request threads hand it render jobs through
#     a queue and wait on a Future (thread-safe for any number of callers).
#     A crashed/disconnected browser is relaunched on the next job.
#   * Every render gets a fresh, isolated browser context (no cookies/
#     cache shared between documents), JavaScript disabled.
#   * Network is deny-by-default: data:/about:/blob: pass, http(s) is only
#     allowed for image GETs of a registered company-logo URL (see
#     resolve_company_logo) or image/font/stylesheet GETs from a host listed
#     in PDF_ALLOWED_ASSET_HOSTS (comma-separated env var). Everything else
#     is aborted.
# ---------------------------------------------------------------------------
_ALLOWED_REMOTE_IMAGES: set[str] = set()
_ALLOWED_REMOTE_IMAGES_MAX = 256
_RENDER_TIMEOUT_S = 60


def allow_remote_image(url: str) -> None:
    if len(_ALLOWED_REMOTE_IMAGES) < _ALLOWED_REMOTE_IMAGES_MAX:
        _ALLOWED_REMOTE_IMAGES.add(url)


def _allowed_asset_hosts() -> set[str]:
    from .config import settings

    raw = settings.pdf_allowed_asset_hosts or ""
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _is_request_allowed(url: str, method: str, resource_type: str) -> bool:
    scheme = url.split(":", 1)[0].lower()
    if scheme in ("data", "about", "blob"):
        return True
    if scheme not in ("http", "https") or method.upper() != "GET":
        return False
    if resource_type == "image" and url in _ALLOWED_REMOTE_IMAGES:
        return True
    host = (urlsplit(url).hostname or "").lower()
    return resource_type in ("image", "font", "stylesheet") and host in _allowed_asset_hosts()


def _route_handler(route) -> None:
    req = route.request
    if _is_request_allowed(req.url, req.method, req.resource_type):
        route.continue_()
    else:
        logger.warning("PDF render: blocked %s %s (%s)", req.method, req.url[:200], req.resource_type)
        route.abort("blockedbyclient")


class _PdfRenderWorker:
    def __init__(self) -> None:
        self._jobs: "queue.Queue[tuple[str, dict | None, concurrent.futures.Future]]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="pdf-render", daemon=True)
                self._thread.start()

    def submit(self, rendered_html: str, pdf_options: dict | None = None) -> "concurrent.futures.Future[bytes]":
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._ensure_thread()
        self._jobs.put((rendered_html, pdf_options, fut))
        return fut

    def _run(self) -> None:
        from playwright.sync_api import sync_playwright

        pw = None
        browser = None
        try:
            while True:
                rendered_html, pdf_options, fut = self._jobs.get()
                if not fut.set_running_or_notify_cancel():
                    continue
                try:
                    if pw is None:
                        pw = sync_playwright().start()
                    if browser is None or not browser.is_connected():
                        browser = pw.chromium.launch()
                    fut.set_result(self._render(browser, rendered_html, pdf_options))
                except BaseException as exc:  # noqa: BLE001 - surfaced to the caller
                    fut.set_exception(exc)
                    # Drop a broken browser so the next job relaunches it.
                    try:
                        if browser is not None and not browser.is_connected():
                            browser = None
                    except Exception:
                        browser = None
        finally:  # pragma: no cover - daemon thread teardown
            try:
                if browser is not None:
                    browser.close()
                if pw is not None:
                    pw.stop()
            except Exception:
                pass

    @staticmethod
    def _render(browser, rendered_html: str, pdf_options: dict | None = None) -> bytes:
        context = browser.new_context(java_script_enabled=False)
        try:
            context.route("**/*", _route_handler)
            page = context.new_page()
            page.set_content(rendered_html, wait_until="load", timeout=_RENDER_TIMEOUT_S * 1000)
            return page.pdf(
                **{
                    "format": "A4",
                    "print_background": True,
                    "prefer_css_page_size": True,
                    **(pdf_options or {}),
                }
            )
        finally:
            context.close()


_worker = _PdfRenderWorker()


def render_html_to_pdf(rendered_html: str, pdf_options: dict | None = None) -> bytes:
    """Converts already-rendered HTML (see render_template_html) to a PDF
    using real headless Chromium via Playwright -- the same rendering
    engine class as the Live Preview iframe, so Grid/Flexbox, fonts,
    colors, and spacing all come out identical to what was previewed.
    prefer_css_page_size honors this module's own @page{size:A4;margin:0}
    (or the admin's override of it); print_background is required for any
    background colors/images in the template to actually appear.

    Runs on the shared, sandboxed render worker above (reused browser,
    fresh context per document, network deny-by-default). pdf_options are
    extra Playwright page.pdf() keyword arguments (e.g. a page-number
    footer_template) layered over those defaults."""
    return _worker.submit(rendered_html, pdf_options).result(timeout=_RENDER_TIMEOUT_S + 30)


def check_pdf_browser() -> bool:
    """Startup self-test (FUNC-01): renders a trivial document through the
    real pipeline. Never raises -- logs a loud, actionable error instead so
    a missing Chromium build is caught at deploy time rather than as a 500
    on the first payslip download."""
    try:
        pdf = render_html_to_pdf("<!DOCTYPE html><html><body>ok</body></html>")
        if not pdf.startswith(b"%PDF"):
            raise RuntimeError("renderer returned non-PDF output")
        logger.info("PDF renderer OK (headless Chromium launched, %d-byte test PDF)", len(pdf))
        return True
    except Exception as exc:  # noqa: BLE001
        first_line = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        logger.error(
            "\n" + "=" * 78 + "\n"
            "PDF RENDERING IS BROKEN -- payslip and offer-letter PDF downloads will fail (500).\n"
            "Headless Chromium for the installed Playwright version could not be launched:\n"
            "  %s\n"
            "Fix (from backend/, inside the venv):\n"
            "  python -m playwright install chromium        (Linux servers: add --with-deps)\n"
            + "=" * 78,
            first_line,
        )
        return False


def start_pdf_browser_check_in_background() -> None:
    """Runs check_pdf_browser off the event loop so app startup is never
    blocked or crashed by it (also warms the shared browser)."""
    threading.Thread(target=check_pdf_browser, name="pdf-render-check", daemon=True).start()
