"""The search_register tool used in the search condition (docs/search_tool.md).

One tool over the local USPTO register database (current through October 18, 2026). Each
application gets its own Session, which enforces two limits:
  - a viewing budget of MAX_RECORDS distinct records (counts of matches are free), and
  - MAX_TOOL_CALLS tool calls (a backstop against runaway loops; enforced by scripts/predict.py).
The application being predicted is always excluded, and so is every record with the same mark
(after normalization) and an owner in common with it: its sibling filings, whose status can reveal
how it was examined. The tool description states what the tool does and its limits, not how to use it.

Status is derived from each record's dates and status code as of October 18, 2026:
  dead       abandoned, cancelled, or expired
  registered has a registration number and isn't dead
  pending    everything else
"""
import json
import re
import sqlite3
import time
from pathlib import Path

sys_path_dir = Path(__file__).resolve().parent
import sys  # noqa: E402
if str(sys_path_dir) not in sys.path:
    sys.path.insert(0, str(sys_path_dir))
from normalize import normalize  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "local" / "uspto_full.db"
MAX_RECORDS = 50       # distinct records the model can view per application
MAX_TOOL_CALLS = 30    # tool calls per application (backstop)
COUNT_CAP = 10000      # counts above this are reported as "more than 10000"
SEARCH_TIMEOUT = 60    # seconds; a search running longer returns a timeout error
GOODS_CHARS = 4000     # goods and services text per record

# Statement type codes (USPTO Trademark Daily XML documentation), keyed by their first two letters.
STATEMENT_TYPES = {
    "PM": "pseudo-mark", "D0": "disclaimer", "D1": "disclaimer", "DS": "disclaimer", "DM": "description of mark",
    "TR": "translation", "TL": "transliteration", "TF": "Section 2(f) statement", "CC": "colors claimed",
    "CD": "color description", "CS": "certification statement", "CU": "concurrent use statement",
    "N0": "name, portrait, or consent statement", "LS": "lining or stippling statement",
    "AF": "in another form statement", "A0": "amendment to registration", "B0": "certificate of correction",
    "OR": "order restricting registration", "FN": "foreign registration information",
    "NR": "non-registration information", "IN": "interference statement", "MK": "mark overflow",
    "GS": "goods and services",
}
STATEMENT_LABELS = sorted(set(STATEMENT_TYPES.values()))
LEGAL_ENTITIES = {
    "01": "individual", "02": "partnership", "03": "corporation", "04": "joint ownership", "05": "joint venture",
    "06": "incorporated association", "07": "unincorporated association", "08": "trustee",
    "09": "joint stock company", "10": "limited liability joint stock company", "11": "company",
    "12": "foundation", "13": "limited partnership", "14": "state agency", "15": "federal agency",
    "16": "limited liability company", "17": "trust", "18": "estate", "19": "sole proprietorship",
    "98": "unknown", "99": "other",
}
# Every case-file-header field in the USPTO data (used to validate header_fields filters).
HEADER_FIELDS = sorted("""abandonment-date amend-to-register-date amended-to-44d-application-in amended-to-44e-application-in
amended-to-itu-application-in amended-to-use-application-in attorney-docket-number attorney-name cancellation-code
cancellation-date cancellation-pending-in certification-mark-in change-registration-in collective-membership-mark-in
collective-service-mark-in collective-trademark-in color-drawing-current-in color-drawing-filed-in concurrent-use-in
concurrent-use-proceeding-in current-location domestic-representative-name drawing-3d-current-in drawing-3d-filed-in
employee-name filed-as-use-application-in filing-basis-current-44d-in filing-basis-current-44e-in
filing-basis-current-66a-in filing-basis-filed-as-44d-in filing-basis-filed-as-44e-in filing-basis-filed-as-66a-in
filing-current-no-basis-in filing-date foreign-priority-in intent-to-use-current-in intent-to-use-in
interference-pending-in law-office-assigned-location-code location-date mark-drawing-code mark-identification
opposition-pending-in principal-register-amended-in published-concurrent-in published-for-opposition-date
registration-date renewal-date renewal-filed-in republished-12c-date section-12c-in section-15-acknowledged-in
section-15-filed-in section-2f-in section-2f-in-part-in section-8-accepted-in section-8-filed-in
section-8-partial-accept-in service-mark-in standard-characters-claimed-in status-code status-date
supplemental-register-amended-in supplemental-register-in trademark-in use-application-currently-in
without-basis-currently-in""".split())

DEAD_CODES = {str(c) for c in list(range(600, 612)) + [614, 618, 622, 626] + list(range(710, 717)) + [900]}

TOOL_DEFINITION = {
    "name": "search_register",
    "description": (
        "Search the USPTO federal trademark register as of October 18, 2026. All given fields must match. "
        "Each search needs at least one of mark_query, goods_query, owner_query, statements_query, "
        "serial_numbers, or registration_numbers. The query fields support AND, OR, NOT (in capitals), "
        "parentheses, \"quoted phrases\", and * for prefix matching; words separated by spaces must all match. "
        f"Returns the number of matching records (reported as \"more than {COUNT_CAP}\" above that) and, "
        "if limit is above 0, the full records starting at offset, in serial-number order. A full record "
        "includes every field held for it: header fields (such as filing basis, attorney, and register), "
        "statements (such as translations, disclaimers, and pseudo-marks), classes with use dates, owners, "
        "goods and services, and any foreign priority, international registration, or prior registration data. "
        f"You can view up to {MAX_RECORDS} distinct records for this application; counts and records "
        f"you have already viewed don't use this limit. You can make up to {MAX_TOOL_CALLS} calls."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "mark_query": {"type": "string", "description": "Query on the mark text."},
            "goods_query": {"type": "string", "description": "Query on the goods and services."},
            "owner_query": {"type": "string", "description": "Query on the owner's name."},
            "statements_query": {"type": "string",
                                 "description": "Query on statements other than goods and services (translations, disclaimers, pseudo-marks, mark descriptions, and others)."},
            "statement_types": {"type": "array", "items": {"type": "string", "enum": STATEMENT_LABELS},
                                "description": "Restrict to records with statements of these types (with statements_query, the matching statement must be of one of these types)."},
            "header_fields": {"type": "object", "additionalProperties": {"type": "string"},
                              "description": "Header field values to match, as {field: text}; matching is case-insensitive and on any part of the value. Fields: " + ", ".join(HEADER_FIELDS) + "."},
            "serial_numbers": {"type": "array", "items": {"type": "string"}, "description": "Specific serial numbers."},
            "registration_numbers": {"type": "array", "items": {"type": "string"}, "description": "Specific registration numbers."},
            "classes": {"type": "array", "items": {"type": "string"},
                        "description": "International classes; matches records in any of them, e.g. [\"009\", \"042\"]."},
            "status": {"type": "array", "items": {"type": "string", "enum": ["registered", "pending", "dead"]},
                       "description": "Statuses to include. Default all."},
            "filed_after": {"type": "string", "description": "Filing date on or after (YYYY-MM-DD)."},
            "filed_before": {"type": "string", "description": "Filing date on or before (YYYY-MM-DD)."},
            "registered_after": {"type": "string", "description": "Registration date on or after (YYYY-MM-DD)."},
            "registered_before": {"type": "string", "description": "Registration date on or before (YYYY-MM-DD)."},
            "limit": {"type": "integer", "description": "Number of full records to return; 0 returns only the count."},
            "offset": {"type": "integer", "description": "Position in the results to start from. Default 0."},
        },
        "required": ["limit"],
    },
}

DEAD_SQL = ("(cf.status_code IN ({}) OR NULLIF(cf.cancellation_date, '') IS NOT NULL "
            "OR NULLIF(cf.abandonment_date, '') IS NOT NULL)").format(",".join(f"'{c}'" for c in sorted(DEAD_CODES)))
REG_SQL = "(cf.registration_number IS NOT NULL AND REPLACE(cf.registration_number, '0', '') != '')"
STATUS_SQL = f"CASE WHEN {DEAD_SQL} THEN 'dead' WHEN {REG_SQL} THEN 'registered' ELSE 'pending' END"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def connect():
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def _digits(values):
    return [re.sub(r"\D", "", str(v)) for v in (values or []) if re.sub(r"\D", "", str(v))]


TOKEN_RE = re.compile(r'"[^"]*"|\(|\)|[^\s()"]+')


def _explicit_and(query):
    """Insert AND between adjacent terms, phrases, and groups, so "(a OR b) c" works.

    FTS5 accepts implicit AND only between plain terms; this makes "words separated by spaces must
    all match" hold everywhere, including next to parentheses. Operators must be in capitals.
    """
    out = []
    for tok in TOKEN_RE.findall(query):
        is_op = tok in ("AND", "OR", "NOT")
        starts_operand = not is_op and tok != ")"
        if out and starts_operand and (out[-1] not in ("AND", "OR", "NOT", "(")):
            out.append("AND")
        if starts_operand and tok != "(" and not tok.startswith('"'):
            # A bare word with punctuation (O'NEILL, A-1) becomes a phrase, which FTS5 can parse.
            star = tok.endswith("*")
            core = tok.rstrip("*")
            if core and not core.isalnum():
                tok = f'"{core}"' + ("*" if star else "")
        out.append(tok)
    return " ".join(out)


def _col(column, query):
    return f"{column} : ({_explicit_and(query)})"


class Session:
    """One application's searches: tracks the records viewed and enforces the viewing budget."""

    def __init__(self, db, exclude_serial, max_records=MAX_RECORDS):
        self.db, self.exclude, self.max_records = db, exclude_serial, max_records
        self.viewed = set()
        # Records with the same normalized mark and an owner in common with the predicted application are
        # hidden too. These SQL functions let the query test that for each candidate record.
        row = db.execute("SELECT mark_text, owner_name FROM case_files WHERE serial_number = ?",
                         (exclude_serial,)).fetchone() if exclude_serial else None
        self._mark = normalize(row[0]) if row and row[0] else None
        self._owners = {normalize(o) for o in (row[1] or "").split(";") if normalize(o)} if row else set()

    def _is_sibling(self, mark, owner):
        if not self._mark or not self._owners or not mark or normalize(mark) != self._mark:
            return 0
        return 1 if {normalize(o) for o in (owner or "").split(";")} & self._owners else 0

    def run(self, args):
        # Registered on each call, so several sessions can share one connection (as the tests do).
        self.db.create_function("tb_sibling", 2, self._is_sibling, deterministic=True)
        deadline = time.monotonic() + SEARCH_TIMEOUT
        self.db.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 100000)
        try:
            return self._search(args)
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc):
                return {"error": f"search timed out after {SEARCH_TIMEOUT} seconds"}
            return {"error": f"query could not be run: {exc}"}
        finally:
            self.db.set_progress_handler(None, 0)

    def _search(self, args):
        mq, gq, oq, sq = [(args.get(k) or "").strip() for k in ("mark_query", "goods_query", "owner_query", "statements_query")]
        serials, regs = _digits(args.get("serial_numbers")), _digits(args.get("registration_numbers"))
        if not (mq or gq or oq or sq or serials or regs):
            return {"error": "each search needs at least one of mark_query, goods_query, owner_query, "
                             "statements_query, serial_numbers, or registration_numbers"}
        labels = [x for x in (args.get("statement_types") or []) if x in STATEMENT_LABELS]
        if args.get("statement_types") and not labels:
            return {"error": "statement_types must be from: " + ", ".join(STATEMENT_LABELS)}
        type_codes = sorted(k for k, v in STATEMENT_TYPES.items() if v in labels)
        header_filters = args.get("header_fields") or {}
        if not isinstance(header_filters, dict):
            return {"error": "header_fields must be an object of {field: text}"}
        unknown = [f for f in header_filters if f not in HEADER_FIELDS]
        if unknown:
            return {"error": f"unknown header fields: {', '.join(unknown)}"}
        try:
            limit = max(0, int(args.get("limit", 0)))
            offset = max(0, int(args.get("offset", 0) or 0))
        except (TypeError, ValueError):
            return {"error": "limit and offset must be integers"}

        # Drive the query from an index, then filter.
        params = []
        if mq or oq:
            source = "mark_fts JOIN case_files cf ON cf.rowid = mark_fts.rowid"
            parts = ([_col("mark_text", mq)] if mq else []) + ([_col("owner_name", oq)] if oq else [])
            where = ["mark_fts MATCH ?"]
            params.append(" AND ".join(parts))
            if gq:
                where.append("EXISTS (SELECT 1 FROM goods_fts JOIN goods_services gs ON gs.rowid = goods_fts.rowid "
                             "WHERE goods_fts MATCH ? AND gs.serial_number = cf.serial_number)")
                params.append(_col("text", gq))
        elif gq:
            source = ("(SELECT DISTINCT gs.serial_number AS sn FROM goods_fts JOIN goods_services gs "
                      "ON gs.rowid = goods_fts.rowid WHERE goods_fts MATCH ?) g JOIN case_files cf ON cf.serial_number = g.sn")
            where = []
            params.append(_col("text", gq))
        elif sq:
            type_sql = f" AND SUBSTR(st.type_code, 1, 2) IN ({','.join('?' * len(type_codes))})" if type_codes else ""
            source = ("(SELECT DISTINCT st.serial_number AS sn FROM statements_fts JOIN statements st "
                      f"ON st.rowid = statements_fts.rowid WHERE statements_fts MATCH ?{type_sql}) s "
                      "JOIN case_files cf ON cf.serial_number = s.sn")
            where = []
            params.append(_col("text", sq))
            params += type_codes
            sq, type_codes = "", []  # already applied
        else:
            source, where = "case_files cf", []
        if sq:
            type_sql = f" AND SUBSTR(st.type_code, 1, 2) IN ({','.join('?' * len(type_codes))})" if type_codes else ""
            where.append("EXISTS (SELECT 1 FROM statements_fts JOIN statements st ON st.rowid = statements_fts.rowid "
                         f"WHERE statements_fts MATCH ? AND st.serial_number = cf.serial_number{type_sql})")
            params.append(_col("text", sq))
            params += type_codes
        elif type_codes:
            where.append(f"EXISTS (SELECT 1 FROM statements st WHERE st.serial_number = cf.serial_number "
                         f"AND SUBSTR(st.type_code, 1, 2) IN ({','.join('?' * len(type_codes))}))")
            params += type_codes
        for field, value in header_filters.items():
            where.append(f"LOWER(json_extract(cf.header_json, '$.\"{field}\"')) LIKE '%' || LOWER(?) || '%'")
            params.append(str(value))
        if serials:
            where.append(f"cf.serial_number IN ({','.join('?' * len(serials))})")
            params += serials
        if regs:
            where.append(f"cf.registration_number IN ({','.join('?' * len(regs))})")
            params += [r.zfill(7) for r in regs]
        where.append("cf.serial_number != ?")
        params.append(self.exclude)
        where.append("tb_sibling(cf.mark_text, cf.owner_name) = 0")
        classes = [c.zfill(3) for c in _digits(args.get("classes"))]
        if classes:
            where.append(f"EXISTS (SELECT 1 FROM classes c WHERE c.serial_number = cf.serial_number "
                         f"AND c.intl_class IN ({','.join('?' * len(classes))}))")
            params += classes
        for key, col, op in [("filed_after", "filing_date", ">="), ("filed_before", "filing_date", "<="),
                             ("registered_after", "registration_date", ">="), ("registered_before", "registration_date", "<=")]:
            v = (args.get(key) or "").strip()
            if v:
                if not DATE_RE.match(v):
                    return {"error": f"{key} must be a date in YYYY-MM-DD format"}
                where.append(f"cf.{col} {op} ?")
                params.append(v)
        statuses = [x for x in (args.get("status") or []) if x in ("registered", "pending", "dead")]
        if statuses:
            where.append(f"({STATUS_SQL}) IN ({','.join('?' * len(statuses))})")
            params += statuses
        cond = " AND ".join(where)

        total = self.db.execute(f"SELECT COUNT(*) FROM (SELECT 1 FROM {source} WHERE {cond} LIMIT {COUNT_CAP + 1})",
                                params).fetchone()[0]
        result = {"total_matches": total if total <= COUNT_CAP else f"more than {COUNT_CAP}"}
        if limit == 0:
            result["records_viewable_remaining"] = self.max_records - len(self.viewed)
            return result

        serial_rows = self.db.execute(f"SELECT cf.serial_number FROM {source} WHERE {cond} "
                                      f"ORDER BY cf.serial_number LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
        shown, budget_reached = [], False
        for (sn,) in serial_rows:
            if sn not in self.viewed:
                if len(self.viewed) >= self.max_records:
                    budget_reached = True
                    break
                self.viewed.add(sn)
            shown.append(sn)
        result.update(offset=offset, returned=len(shown), records=[self._record(sn) for sn in shown],
                      records_viewable_remaining=self.max_records - len(self.viewed))
        if budget_reached:
            result["note"] = f"viewing limit of {self.max_records} distinct records reached"
        return result

    def _record(self, sn):
        (reg, mark, code, sdate, fdate, rdate, adate, cdate, owner, header_json, extra_json) = self.db.execute(
            "SELECT registration_number, mark_text, status_code, status_date, filing_date, registration_date, "
            "abandonment_date, cancellation_date, owner_name, header_json, extra_json "
            "FROM case_files WHERE serial_number = ?", (sn,)).fetchone()
        dead = (code or "") in DEAD_CODES or bool(cdate) or bool(adate)
        regno = reg if reg and reg.strip("0") else None
        gs = " || ".join(f"[{c}] {t}" for c, t in self.db.execute(
            "SELECT intl_class, text FROM goods_services WHERE serial_number = ? ORDER BY intl_class", (sn,)) if t)
        statements = [dict(type=STATEMENT_TYPES.get(tc[:2], tc), type_code=tc, text=t) for tc, t in self.db.execute(
            "SELECT type_code, text FROM statements WHERE serial_number = ? ORDER BY type_code", (sn,)) if t]
        classifications = [dict(international_class=ic, us_classes=us, primary_class=pc, status_code=st,
                                status_date=sd, first_use_anywhere=fua, first_use_in_commerce=fuc)
                           for ic, us, pc, st, sd, fua, fuc in self.db.execute(
            "SELECT international_code, us_codes, primary_code, status_code, status_date, first_use_anywhere_date, "
            "first_use_in_commerce_date FROM classifications WHERE serial_number = ? ORDER BY international_code", (sn,))]
        owners = []
        for entry, ptype, name, entity, details in self.db.execute(
                "SELECT entry_number, party_type, party_name, legal_entity_type_code, details_json FROM owners "
                "WHERE serial_number = ? ORDER BY party_type, entry_number", (sn,)):
            o = dict(party_type=ptype, name=name, legal_entity=LEGAL_ENTITIES.get(entity or "", entity))
            if details:
                o.update(json.loads(details))
            owners.append(o)
        record = dict(serial_number=sn, registration_number=regno, mark=mark,
                      status="dead" if dead else ("registered" if regno else "pending"),
                      status_code=code, status_date=sdate, filing_date=fdate, registration_date=rdate,
                      abandonment_date=adate, cancellation_date=cdate, owner=owner,
                      classes=[c for (c,) in self.db.execute(
                          "SELECT intl_class FROM classes WHERE serial_number = ? ORDER BY intl_class", (sn,))],
                      goods_services=gs[:GOODS_CHARS] + ("…" if len(gs) > GOODS_CHARS else ""),
                      statements=statements, classifications=classifications, owners=owners,
                      header=json.loads(header_json) if header_json else {})
        if extra_json:
            record.update(json.loads(extra_json))
        return record


if __name__ == "__main__":
    # Try it from the command line: one or more searches share one Session, like one application.
    #   python3 -I scripts/search_register.py '<json args>' ['<json args>' ...] [--exclude SERIAL]
    import sys
    argv = sys.argv[1:]
    exclude = ""
    if "--exclude" in argv:
        i = argv.index("--exclude")
        exclude = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    session = Session(connect(), exclude)
    for a in argv:
        # Command-line convenience only: accept curly quotes pasted from a word processor.
        a = a.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
        try:
            args = json.loads(a)
        except json.JSONDecodeError as exc:
            print(f"Not valid JSON ({exc}). Check for a missing quote, comma, or brace.")
            continue
        print(json.dumps(session.run(args), indent=2))
