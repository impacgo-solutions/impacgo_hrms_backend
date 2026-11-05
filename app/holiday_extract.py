"""Reads a holiday-calendar document and extracts its holidays.

Supported: PDF with a text layer (pypdf, layout mode keeps table columns),
Word .docx (tables + paragraphs), Excel .xlsx (every sheet) and CSV / TXT.
Scanned (image-only) PDFs have no text to read -- that is reported, never
guessed.

Each document becomes rows of cells. A header row (Date / Day / Holiday /
Occasion / Branch / Location / Type ...) maps the columns; without one each
row is read heuristically. Per holiday: the date (many formats; a missing
year comes from the document), the weekday (always recomputed from the date;
a different weekday printed in the document is flagged), the name, the
applicable branch(es) matched against the company's real branches ("All" /
blank = all branches; one row per listed branch; unknown names flagged) and
whether it is optional / restricted. Nothing is invented: every row carries
the source text it came from.
"""

from __future__ import annotations

import csv
import datetime
import io
import re
import zipfile
from collections import Counter
from xml.etree import ElementTree as ET

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"], start=1)}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})
MONTHS["sept"] = 9
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_WEEKDAY_RE = re.compile(r"\b(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(day|sday|nesday|rsday|urday)?\b\.?", re.I)
_MONTH = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DATE_PATTERNS = [
    ("ymd", re.compile(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b")),
    ("dmy", re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{4}|\d{2})\b")),
    ("d_mon_y", re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?[\s\-./,]*(?:of\s+)?{_MONTH}\.?[\s\-./,]*(\d{{4}})?\b", re.I)),
    ("mon_d_y", re.compile(rf"\b{_MONTH}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s*(\d{{4}}))?\b", re.I)),
]
_OPTIONAL_RE = re.compile(r"\b(optional|restricted)\b|\bRH\b|\(\s*O\s*\)", re.I)
_ALL_RE = re.compile(r"^\s*(all|all\s+(branches|locations|offices|sites)|pan\s*india|company[\s-]*wide|everyone|global)\s*$", re.I)
_SERIAL_RE = re.compile(r"^\s*(s\.?\s*no\.?|sl\.?\s*no\.?|#)?\s*\d{1,3}\s*[.)\-:]?\s+")
_HEADER_KEYS = {
    "date": re.compile(r"^\s*(date|holiday\s*date|dates?)\s*$", re.I),
    "day": re.compile(r"^\s*(day|weekday|day\s+of\s+week)\s*$", re.I),
    "name": re.compile(r"^\s*(holiday|holidays|holiday\s*name|occasion|festival|name|description|particulars|event)\s*$", re.I),
    "branch": re.compile(r"^\s*((applicable|valid)(\s+(to|for|in|at))?\s*)?(branch(es)?|locations?|regions?|offices?|sites?|states?|cit(y|ies))?\s*$|^\s*applicable(\s+to)?\s*$", re.I),
    "type": re.compile(r"^\s*(type|category|holiday\s*type|optional|nature)\s*$", re.I),
    "serial": re.compile(r"^\s*(s\.?\s*no\.?|sl\.?\s*no\.?|no\.?|#|sr\.?\s*no\.?)\s*$", re.I),
}


class ExtractionError(Exception):
    """The document could not be read at all (message is user-facing)."""


# ── document -> rows of cells ───────────────────────────────────────────────

def document_rows(content: bytes, filename: str) -> list[list[str]]:
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
    if ext == "pdf":
        return _pdf_rows(content)
    if ext == "docx":
        return _docx_rows(content)
    if ext == "xlsx":
        return _xlsx_rows(content)
    if ext in ("csv", "txt"):
        text = content.decode("utf-8-sig", errors="replace")
        if ext == "csv":
            return [[c.strip() for c in row] for row in csv.reader(io.StringIO(text)) if any(c.strip() for c in row)]
        return [_split_line(line) for line in text.splitlines() if line.strip()]
    raise ExtractionError("Upload a PDF, Word (.docx), Excel (.xlsx) or CSV holiday calendar.")


def _split_line(line: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"\t|\s{2,}|\s\|\s|\|", line) if p.strip()]
    return parts or [line.strip()]


def _pdf_rows(content: bytes) -> list[list[str]]:
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as exc:
                raise ExtractionError("This PDF is password-protected -- upload an unlocked copy.") from exc
        rows: list[list[str]] = []
        for page in reader.pages:
            try:
                text = page.extract_text(extraction_mode="layout") or ""
            except Exception:
                text = page.extract_text() or ""
            rows.extend(_split_line(line) for line in text.splitlines() if line.strip())
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError("The PDF could not be read -- it may be damaged.") from exc
    if not rows:
        raise ExtractionError("No text found in this PDF -- it looks like a scanned image. Upload the original "
                              "PDF, a Word/Excel file or a CSV instead.")
    return rows


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx_rows(content: bytes) -> list[list[str]]:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            root = ET.fromstring(z.read("word/document.xml"))
    except Exception as exc:
        raise ExtractionError("The Word document could not be read.") from exc

    def text_of(el) -> str:
        return "".join(t.text or "" for t in el.iter(f"{_W}t")).strip()

    rows: list[list[str]] = []
    body = root.find(f"{_W}body")
    for child in (body if body is not None else []):
        if child.tag == f"{_W}tbl":
            for tr in child.iter(f"{_W}tr"):
                cells = [text_of(tc) for tc in tr.findall(f"{_W}tc")]
                if any(cells):
                    rows.append(cells)
        elif child.tag == f"{_W}p":
            t = text_of(child)
            if t:
                rows.append(_split_line(t))
    return rows


_X = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _xlsx_rows(content: bytes) -> list[list[str]]:
    try:
        z = zipfile.ZipFile(io.BytesIO(content))
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(f"{_X}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{_X}t")))
        date_styles = _xlsx_date_styles(z)
        rows: list[list[str]] = []
        for name in sorted(n for n in z.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")):
            for row in ET.fromstring(z.read(name)).iter(f"{_X}row"):
                cells: dict[int, str] = {}
                for c in row.findall(f"{_X}c"):
                    ref, kind, style = c.get("r", ""), c.get("t"), c.get("s")
                    col = _col_index(ref)
                    v = c.find(f"{_X}v")
                    if kind == "s" and v is not None:
                        value = shared[int(v.text)]
                    elif kind == "inlineStr":
                        value = "".join(t.text or "" for t in c.iter(f"{_X}t"))
                    elif v is not None and v.text is not None:
                        value = v.text
                        if style is not None and int(style) in date_styles:
                            try:
                                value = (datetime.date(1899, 12, 30) + datetime.timedelta(days=int(float(value)))).isoformat()
                            except (ValueError, OverflowError):
                                pass
                    else:
                        value = ""
                    cells[col] = value.strip()
                if any(cells.values()):
                    rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
        return rows
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError("The Excel file could not be read.") from exc


def _col_index(ref: str) -> int:
    letters = "".join(ch for ch in ref if ch.isalpha())
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch.upper()) - 64)
    return max(n - 1, 0)


def _xlsx_date_styles(z: zipfile.ZipFile) -> set[int]:
    """Indexes of cell styles that format numbers as dates."""
    if "xl/styles.xml" not in z.namelist():
        return set()
    root = ET.fromstring(z.read("xl/styles.xml"))
    custom = {int(f.get("numFmtId")): f.get("formatCode", "").lower()
              for f in root.iter(f"{_X}numFmt")}
    builtin_dates = set(range(14, 23)) | {45, 46, 47}
    out = set()
    xfs = root.find(f"{_X}cellXfs")
    for i, xf in enumerate(xfs.findall(f"{_X}xf") if xfs is not None else []):
        fid = int(xf.get("numFmtId", "0"))
        fmt = custom.get(fid, "")
        if fid in builtin_dates or (fmt and ("d" in fmt or "y" in fmt) and "h" not in fmt.replace("th", "")):
            out.add(i)
    return out


# ── rows -> holidays ───────────────────────────────────────────────────────

def _year_of(y: str | None, default_year: int) -> int:
    if not y:
        return default_year
    y_int = int(y)
    return 2000 + y_int if y_int < 100 else y_int


def find_dates(text: str, default_year: int) -> list[tuple[datetime.date, tuple[int, int], bool]]:
    """Every date in [text]: (date, span, had_explicit_year)."""
    found: list[tuple[datetime.date, tuple[int, int], bool]] = []
    taken: list[tuple[int, int]] = []
    for kind, rx in _DATE_PATTERNS:
        for m in rx.finditer(text):
            if any(a < m.end() and m.start() < b for a, b in taken):
                continue
            try:
                if kind == "ymd":
                    d, explicit = datetime.date(int(m[1]), int(m[2]), int(m[3])), True
                elif kind == "dmy":
                    d, explicit = datetime.date(_year_of(m[3], default_year), int(m[2]), int(m[1])), True
                elif kind == "d_mon_y":
                    d = datetime.date(_year_of(m[3], default_year), MONTHS[m[2].lower().rstrip(".")[:4].rstrip("t") if m[2].lower().startswith("sept") else m[2].lower()[:3]], int(m[1]))
                    explicit = bool(m[3])
                else:
                    d = datetime.date(_year_of(m[3], default_year), MONTHS[m[1].lower()[:3]], int(m[2]))
                    explicit = bool(m[3])
            except (ValueError, KeyError):
                continue
            found.append((d, m.span(), explicit))
            taken.append(m.span())
    return sorted(found, key=lambda x: x[1][0])


def document_year(rows: list[list[str]], fallback: int) -> int:
    years = Counter(int(y) for row in rows for cell in row for y in re.findall(r"\b(20\d{2})\b", cell))
    return years.most_common(1)[0][0] if years else fallback


def _header_map(row: list[str]) -> dict[str, int] | None:
    mapping: dict[str, int] = {}
    for i, cell in enumerate(row):
        if not (cell or "").strip():
            continue
        for key, rx in _HEADER_KEYS.items():
            if key not in mapping and rx.match(cell or ""):
                mapping[key] = i
                break
    return mapping if "date" in mapping and ("name" in mapping or len(mapping) >= 3) else None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def match_branches(text: str, branches: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], list[str], bool]:
    """(matched (id, name) list, unmatched names, is_all) for a branch cell."""
    if not text or not text.strip() or _ALL_RE.match(text):
        return [], [], True
    parts = [p.strip() for p in re.split(r",|/|;|&|\band\b|\n", text) if p.strip()]
    matched, unmatched = [], []
    keys = [(bid, name, _norm(name), _norm(re.sub(r"\b(branch|office|location)\b", "", name, flags=re.I)))
            for bid, name in branches]
    for part in parts:
        if _ALL_RE.match(part):
            return [], [], True
        n = _norm(part)
        hit = next(((bid, name) for bid, name, full, short in keys
                    if n and (n == full or n == short or (short and (short in n or n in short)))), None)
        if hit is not None:
            if hit not in matched:
                matched.append(hit)
        else:
            unmatched.append(part)
    return matched, unmatched, not matched and not unmatched


# Holiday names that contain a weekday -- never stripped as "the day".
_WEEKDAY_HOLIDAYS = re.compile(
    r"\b(good|black|holy|easter|palm|ash|maundy|shrove|whit|super|cyber|boxing)\s+"
    r"(mon|tues|wednes|thurs|fri|satur|sun)day\b", re.I)


def _strip_weekdays(text: str) -> str:
    kept: list[tuple[str, str]] = []

    def protect(m: re.Match) -> str:
        kept.append((f"\x00{len(kept)}\x00", m.group(0)))
        return kept[-1][0]

    text = _WEEKDAY_HOLIDAYS.sub(protect, text)
    text = _WEEKDAY_RE.sub(" ", text)
    for token, original in kept:
        text = text.replace(token, original)
    return text


def _clean_name(text: str, *, strip_weekdays: bool = True) -> str:
    if strip_weekdays:
        text = _strip_weekdays(text)
    elif _WEEKDAY_RE.fullmatch(text.strip()):
        text = ""
    # Leftovers of a removed date range ("Pongal 13 Jan to 15 Jan").
    text = re.sub(r"\s(to|till|until|from)\s*(?=$|[|,;])", " ", text.strip() + " ", flags=re.I)
    text = re.sub(r"^\s*(from|on)\s+", "", text, flags=re.I)
    text = _OPTIONAL_RE.sub(" ", text)
    text = _SERIAL_RE.sub("", text)
    text = re.sub(r"\b(holiday|holidays)\s*[:\-]\s*", "", text, flags=re.I) if re.match(r"^\s*holiday\s*[:\-]", text, re.I) else text
    text = re.sub(r"[\s\-–—|:,.()]+$", "", re.sub(r"^[\s\-–—|:,.()]+", "", text))
    return re.sub(r"\s{2,}", " ", text).strip()[:120]


def extract_holidays(rows: list[list[str]], branches: list[tuple[str, str]], fallback_year: int) -> list[dict]:
    """Holiday rows from document rows. branches: the company's (id, name)."""
    year = document_year(rows, fallback_year)
    doc_has_year = any(re.search(r"\b20\d{2}\b", c or "") for row in rows for c in row)
    no_year = [] if doc_has_year else [f"No year in the document -- {year} assumed."]
    header: dict[str, int] | None = None
    out: list[dict] = []
    seen: set[tuple] = set()

    def emit(date: datetime.date, name: str, branch_text: str, optional: bool, source: str,
             printed_day: str | None, warnings: list[str]):
        if not name:
            return
        matched, unmatched, is_all = match_branches(branch_text, branches)
        targets: list[tuple[str | None, str]] = [(None, "All Branches")] if is_all else list(matched)
        row_warnings = list(warnings)
        if unmatched:
            row_warnings.append(f"Branch not found: {', '.join(unmatched)} -- choose the branch.")
            if not matched:
                targets = [(None, "All Branches")]
        if printed_day:
            real = WEEKDAYS[date.weekday()]
            if not real.startswith(printed_day.lower()[:3]):
                row_warnings.append(f"The document says {printed_day.title()}, but {date:%d %b %Y} is a {real.title()}.")
        for branch_id, branch_name in targets:
            key = (date, name.lower(), branch_id)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "date": date.isoformat(), "day": date.strftime("%A"), "name": name,
                "branch_id": branch_id, "branch_name": branch_name, "is_optional": optional,
                "source_text": source[:300], "warnings": row_warnings,
            })

    for cells in rows:
        cells = [c or "" for c in cells]
        joined = " | ".join(c for c in cells if c)
        maybe_header = _header_map(cells)
        if maybe_header is not None and not find_dates(joined, year):
            header = maybe_header
            continue
        if header is not None and len(cells) > max(header.values()) - 1 and header["date"] < len(cells):
            date_cell = cells[header["date"]]
            dates = find_dates(date_cell, year) or find_dates(joined, year)
            if not dates:
                continue
            name_cell = cells[header["name"]] if "name" in header and header["name"] < len(cells) else ""
            if not name_cell:
                rest = [c for i, c in enumerate(cells) if i not in header.values() and not find_dates(c, year)]
                name_cell = " ".join(rest)
            branch_cell = cells[header["branch"]] if "branch" in header and header["branch"] < len(cells) else ""
            type_cell = cells[header["type"]] if "type" in header and header["type"] < len(cells) else ""
            day_cell = cells[header["day"]] if "day" in header and header["day"] < len(cells) else ""
            printed = _WEEKDAY_RE.search(day_cell or date_cell)
            optional = bool(_OPTIONAL_RE.search(type_cell) or _OPTIONAL_RE.search(name_cell))
            name = _clean_name(name_cell, strip_weekdays="name" not in header)
            for d in _expand(dates, date_cell):
                emit(d, name, branch_cell, optional, joined, printed.group(0) if printed and len(dates) == 1 else None,
                     [] if dates[0][2] else no_year)
            continue
        # No header: read the row as free text.
        dates = find_dates(joined, year)
        if not dates:
            continue
        text = joined
        for _d, (a, b), _e in reversed(dates):
            text = text[:a] + " " + text[b:]
        printed = _WEEKDAY_RE.search(joined)
        optional = bool(_OPTIONAL_RE.search(joined))
        branch_text = ""
        segments = [s.strip() for s in text.split("|") if s.strip()]
        if len(segments) >= 2:
            matched, _u, _a = match_branches(segments[-1], branches)
            if matched or _ALL_RE.match(segments[-1]):
                branch_text = segments[-1]
                segments = segments[:-1]
        else:
            for _bid, bname in branches:
                if re.search(re.escape(bname), text, re.I):
                    branch_text = bname
                    text = re.sub(re.escape(bname), " ", text, flags=re.I)
            segments = [text]
        name = _clean_name(" ".join(s for s in segments if not _WEEKDAY_RE.fullmatch(s.strip())))
        for d in _expand(dates, joined):
            emit(d, name, branch_text, optional, joined, printed.group(0) if printed and len(dates) == 1 else None,
                 [] if dates[0][2] else no_year)
    return sorted(out, key=lambda r: (r["date"], r["name"].lower()))


def _expand(dates, text: str) -> list[datetime.date]:
    """Two dates joined by "to" / a dash are a range (max 10 days); otherwise
    every date listed in the row."""
    if len(dates) == 2:
        (d1, (_a, e1), _x), (d2, (s2, _b), _y) = dates
        between = text[e1:s2]
        if re.fullmatch(r"\s*(to|till|until|–|—|-)\s*", between, re.I) and d1 < d2 and (d2 - d1).days <= 10:
            return [d1 + datetime.timedelta(days=i) for i in range((d2 - d1).days + 1)]
    return [d for d, _s, _e in dates]
