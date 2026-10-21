"""Stream-parse USPTO trademark applications XML (daily or annual backfile) into SQLite.

Usage: python3 -I tm_parser.py [--filter] DB_PATH INPUT [INPUT ...]
INPUT may be an .xml file or a .zip containing one or more .xml files.
Files are applied in the order given; for each serial number the record with the
latest transaction-date wins, so pass the backfile first and daily files after it.

By default every case-file is kept. With --filter, only standard-character marks in
at least one of TARGET_CLASSES are kept (all classes and goods/services are stored).
After loading, the full-text search indexes are rebuilt.
"""
import json
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

TARGET_CLASSES = {"009", "035", "041", "042"}
BATCH = 5000

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
CREATE TABLE IF NOT EXISTS case_files (
    serial_number      TEXT PRIMARY KEY,
    registration_number TEXT,
    mark_text          TEXT,
    standard_characters INTEGER,
    mark_drawing_code  TEXT,
    status_code        TEXT,
    status_date        TEXT,
    filing_date        TEXT,
    registration_date  TEXT,
    abandonment_date   TEXT,
    cancellation_date  TEXT,
    cancellation_code  TEXT,
    owner_name         TEXT,
    transaction_date   TEXT,
    source_file        TEXT,
    header_json        TEXT,   -- every non-empty case-file-header field, as {"field": "value"}
    extra_json         TEXT    -- correspondent, international registration, foreign applications,
                               -- prior registrations, Madrid filings: {"section": [{"field": "value"}]}
);
CREATE TABLE IF NOT EXISTS classes (
    serial_number TEXT NOT NULL,
    intl_class    TEXT NOT NULL,
    PRIMARY KEY (serial_number, intl_class)
);
CREATE TABLE IF NOT EXISTS goods_services (
    serial_number TEXT NOT NULL,
    intl_class    TEXT NOT NULL,
    type_code     TEXT NOT NULL,
    text          TEXT,
    PRIMARY KEY (serial_number, type_code)
);
CREATE TABLE IF NOT EXISTS owners (
    serial_number TEXT NOT NULL,
    entry_number  INTEGER,
    party_type    TEXT,
    party_name    TEXT,
    legal_entity_type_code TEXT,
    details_json  TEXT     -- every other non-empty owner field
);
CREATE TABLE IF NOT EXISTS statements (     -- every statement except goods and services
    serial_number TEXT NOT NULL,
    type_code     TEXT NOT NULL,
    text          TEXT,
    PRIMARY KEY (serial_number, type_code)
);
CREATE TABLE IF NOT EXISTS classifications (
    serial_number TEXT NOT NULL,
    international_code TEXT,
    us_codes      TEXT,
    primary_code  TEXT,
    status_code   TEXT,
    status_date   TEXT,
    first_use_anywhere_date TEXT,
    first_use_in_commerce_date TEXT
);
CREATE INDEX IF NOT EXISTS classifications_serial ON classifications (serial_number);
CREATE INDEX IF NOT EXISTS owners_serial ON owners (serial_number);
CREATE INDEX IF NOT EXISTS classes_class ON classes (intl_class);
CREATE INDEX IF NOT EXISTS case_files_reg ON case_files (registration_number);
CREATE TABLE IF NOT EXISTS processed_files (
    source_file  TEXT PRIMARY KEY,
    case_files   INTEGER,
    kept         INTEGER,
    processed_at TEXT
);
-- Full-text search over mark text and owner, and over goods/services text.
-- External-content tables: rebuilt by build_search_index() after loading.
CREATE VIRTUAL TABLE IF NOT EXISTS mark_fts USING fts5(
    mark_text, owner_name, content='case_files', content_rowid='rowid'
);
CREATE VIRTUAL TABLE IF NOT EXISTS goods_fts USING fts5(
    text, content='goods_services', content_rowid='rowid'
);
CREATE VIRTUAL TABLE IF NOT EXISTS statements_fts USING fts5(
    text, content='statements', content_rowid='rowid'
);
"""


def text(el, path):
    found = el.find(path)
    if found is None or found.text is None:
        return None
    value = found.text.strip()
    return value or None


def date(el, path):
    value = text(el, path)
    return f"{value[:4]}-{value[4:6]}-{value[6:8]}" if value and len(value) == 8 else value


def as_int(value):
    return int(value) if value and value.isdigit() else 0


def parse_case(cf, filtered=False):
    """Return a dict for a case-file, or None if it lacks a header or fails the filter."""
    header = cf.find("case-file-header")
    if header is None:
        return None
    std_flag = text(header, "standard-characters-claimed-in")
    classes = {c.text.strip() for c in cf.iterfind("classifications/classification/international-code") if c.text}
    if filtered and (std_flag != "T" or not classes & TARGET_CLASSES):
        return None

    goods, statements = [], []
    for st in cf.iterfind("case-file-statements/case-file-statement"):
        code = text(st, "type-code") or ""
        # GS + 3-digit class + sequence digit, e.g. GS0091 = class 009
        if code.startswith("GS") and code[2:5].isdigit():
            goods.append((code[2:5], code, text(st, "text")))
        elif code:
            statements.append((code, text(st, "text")))

    header_fields = {h.tag: h.text.strip() for h in header if h.text and h.text.strip()}

    classifications = []
    for c in cf.iterfind("classifications/classification"):
        classifications.append((
            text(c, "international-code"),
            ",".join(u.text.strip() for u in c.iterfind("us-code") if u.text and u.text.strip()) or None,
            text(c, "primary-code"), text(c, "status-code"), date(c, "status-date"),
            date(c, "first-use-anywhere-date"), date(c, "first-use-in-commerce-date")))

    extra = {}
    for section in ("correspondent", "international-registration", "foreign-applications",
                    "prior-registration-applications", "madrid-international-filing-requests"):
        el = cf.find(section)
        if el is None:
            continue
        entries = list(el) if len(el) and all(len(ch) for ch in el) else [el]
        rows = []
        for entry in entries:
            leaf = {x.tag: x.text.strip() for x in entry.iter() if len(x) == 0 and x.text and x.text.strip()}
            if leaf:
                rows.append(leaf)
        if rows:
            extra[section] = rows

    owners = []
    for ow in cf.iterfind("case-file-owners/case-file-owner"):
        entry = text(ow, "entry-number")
        details = {x.tag: x.text.strip() for x in ow.iter() if len(x) == 0 and x.text and x.text.strip()
                   and x.tag not in ("entry-number", "party-type", "party-name", "legal-entity-type-code")}
        owners.append((int(entry) if entry and entry.isdigit() else None, text(ow, "party-type"), text(ow, "party-name"),
                       text(ow, "legal-entity-type-code"), json.dumps(details, ensure_ascii=False) if details else None))
    # Current owner(s): every owner at the highest party-type (latest stage of prosecution),
    # so joint applicants are all kept.
    top_type = max((as_int(o[1]) for o in owners), default=None)
    current_owners = [o[2] for o in sorted(owners, key=lambda o: o[0] or 0) if o[2] and as_int(o[1]) == top_type]

    reg = text(cf, "registration-number")
    return {
        "serial_number": text(cf, "serial-number"),
        "registration_number": None if not reg or set(reg) == {"0"} else reg,
        "mark_text": text(header, "mark-identification"),
        "standard_characters": {"T": 1, "F": 0}.get(std_flag),
        "mark_drawing_code": text(header, "mark-drawing-code"),
        "status_code": text(header, "status-code"),
        "status_date": date(header, "status-date"),
        "filing_date": date(header, "filing-date"),
        "registration_date": date(header, "registration-date"),
        "abandonment_date": date(header, "abandonment-date"),
        "cancellation_date": date(header, "cancellation-date"),
        "cancellation_code": text(header, "cancellation-code"),
        "owner_name": "; ".join(current_owners) or None,
        "transaction_date": date(cf, "transaction-date"),
        "classes": sorted(classes),
        "goods": goods,
        "owners": owners,
        "statements": statements,
        "classifications": classifications,
        "header_json": json.dumps(header_fields, ensure_ascii=False),
        "extra_json": json.dumps(extra, ensure_ascii=False) if extra else None,
    }


def write_batch(db, rows, source):
    cur = db.cursor()
    for r in rows:
        existing = cur.execute(
            "SELECT transaction_date FROM case_files WHERE serial_number = ?", (r["serial_number"],)
        ).fetchone()
        if existing and existing[0] and r["transaction_date"] and existing[0] > r["transaction_date"]:
            continue  # already have a newer version of this case
        sn = r["serial_number"]
        if existing:
            for table in ("classes", "goods_services", "owners", "statements", "classifications"):
                cur.execute(f"DELETE FROM {table} WHERE serial_number = ?", (sn,))
        cur.execute(
            """INSERT OR REPLACE INTO case_files VALUES
               (:serial_number, :registration_number, :mark_text, :standard_characters, :mark_drawing_code,
                :status_code, :status_date, :filing_date, :registration_date, :abandonment_date,
                :cancellation_date, :cancellation_code, :owner_name, :transaction_date, :source,
                :header_json, :extra_json)""",
            {**r, "source": source},
        )
        cur.executemany("INSERT OR IGNORE INTO classes VALUES (?, ?)", [(sn, c) for c in r["classes"]])
        cur.executemany("INSERT OR REPLACE INTO goods_services VALUES (?, ?, ?, ?)", [(sn, *g) for g in r["goods"]])
        cur.executemany("INSERT INTO owners VALUES (?, ?, ?, ?, ?, ?)", [(sn, *o) for o in r["owners"]])
        cur.executemany("INSERT OR REPLACE INTO statements VALUES (?, ?, ?)", [(sn, *x) for x in r["statements"]])
        cur.executemany("INSERT INTO classifications VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        [(sn, *x) for x in r["classifications"]])
    db.commit()


def process_stream(db, stream, source, filtered=False):
    total = kept = 0
    rows = []
    stack = []
    for event, el in ET.iterparse(stream, events=("start", "end")):
        if event == "start":
            stack.append(el)
            continue
        stack.pop()
        if el.tag != "case-file":
            continue
        total += 1
        row = parse_case(el, filtered)
        if row:
            rows.append(row)
            kept += 1
        if len(rows) >= BATCH:
            write_batch(db, rows, source)
            rows = []
        # keep memory flat: detach the processed case-file from its parent
        if stack:
            stack[-1].remove(el)
    write_batch(db, rows, source)
    db.execute(
        "INSERT OR REPLACE INTO processed_files VALUES (?, ?, ?, datetime('now'))", (source, total, kept)
    )
    db.commit()
    return total, kept


def load_file(db, path, filtered=False):
    """Load one .xml or .zip file; prints and returns (total, kept)."""
    path = Path(path).expanduser()
    total = kept = 0
    start = time.time()
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.lower().endswith(".xml"):
                    with zf.open(name) as stream:
                        t, k = process_stream(db, stream, f"{path.name}/{name}", filtered)
                    total, kept = total + t, kept + k
    else:
        with open(path, "rb") as stream:
            total, kept = process_stream(db, stream, path.name, filtered)
    print(f"{path.name}: {total} case-files, kept {kept} ({time.time() - start:.1f}s)", flush=True)
    return total, kept


def build_search_index(db):
    start = time.time()
    db.execute("INSERT INTO mark_fts(mark_fts) VALUES ('rebuild')")
    db.execute("INSERT INTO goods_fts(goods_fts) VALUES ('rebuild')")
    db.execute("INSERT INTO statements_fts(statements_fts) VALUES ('rebuild')")
    db.commit()
    print(f"search indexes rebuilt ({time.time() - start:.1f}s)", flush=True)


def connect(db_path):
    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA)
    return db


def main():
    args = sys.argv[1:]
    filtered = "--filter" in args
    args = [a for a in args if a != "--filter"]
    if len(args) < 2:
        sys.exit(__doc__)
    db = connect(args[0])
    for arg in args[1:]:
        load_file(db, arg, filtered)
    build_search_index(db)
    db.close()


if __name__ == "__main__":
    main()
