"""Keep data/uspto.db current with USPTO's daily trademark application files (TRTDXFAP).

The database starts as an APFS clone of TrademarkBench's register (local/uspto_full.db, current
through 2026-10-18), so it already has the 2025 backfile and every 2026 daily file through then.
Each run asks the Open Data Portal for daily files from a week before the newest one loaded
(to catch files posted late), loads every file not yet in processed_files, oldest first, then
rebuilds the full-text search indexes. A run with nothing new exits in a few seconds, so it is
safe to run as often as you like.

For each serial number the record with the latest transaction date wins (tm_parser.write_batch),
so a file loaded out of order can't overwrite newer data.

Usage: python3 -I sync/sync_daily.py [--dry-run]
--dry-run lists the files that would be loaded and changes nothing.
Writes data/sync_status.json, which the clearance skill reads to report how current the register is.
Exits non-zero if any file failed to load; the next run retries it.
"""
import datetime as dt
import fcntl
import http.client
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
HERE = SCRIPT_DIR.parent  # project root: .env, data/, logs/
sys.path.insert(0, str(SCRIPT_DIR))  # -I drops the script dir; tm_parser lives next to this file
import tm_parser  # noqa: E402

DB_PATH = HERE / "data" / "uspto.db"
DOWNLOAD_DIR = HERE / "data" / "downloads"
STATUS_PATH = HERE / "data" / "sync_status.json"
LOCK_PATH = HERE / "data" / ".sync.lock"
API = "https://api.uspto.gov/api/v1/datasets/products"
LOOKBACK_DAYS = 7
DAILY_RE = re.compile(r"^apc(\d{6})\.zip$")


def log(msg):
    print(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def api_key():
    for line in (HERE / ".env").read_text().splitlines():
        if line.startswith("USPTO_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"')
    sys.exit("USPTO_API_KEY not found in .env")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def with_retries(fn, what, attempts=6):
    for attempt in range(attempts):
        try:
            return fn()
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
            wait = 60 if getattr(exc, "code", None) == 429 else 10 * 2 ** attempt
            log(f"  {what}: {exc} - retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"giving up on {what}")


def file_list(key, from_date, to_date):
    query = f"includeFiles=true&fileDataFromDate={from_date}&fileDataToDate={to_date}"

    def fetch():
        req = urllib.request.Request(f"{API}/TRTDXFAP?{query}", headers={"X-API-KEY": key})
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.load(resp)["bulkDataProductBag"][0]["productFileBag"]["fileDataBag"]
    return with_retries(fetch, "TRTDXFAP file list")


def download(key, entry, dest):
    """Fetch the signed URL with the API key, then download it without sending the key."""
    def fetch():
        opener = urllib.request.build_opener(NoRedirect)
        req = urllib.request.Request(entry["fileDownloadURI"], headers={"X-API-KEY": key})
        try:
            opener.open(req, timeout=120)
            raise ValueError("expected a redirect to the file")
        except urllib.error.HTTPError as exc:
            if exc.code not in (301, 302, 303, 307, 308):
                raise
            location = exc.headers["Location"]
        part = dest.with_suffix(".part")
        with urllib.request.urlopen(location, timeout=300) as resp, open(part, "wb") as out:
            while chunk := resp.read(1 << 20):
                out.write(chunk)
        if part.stat().st_size != entry["fileSize"]:
            part.unlink()
            raise ValueError(f"size mismatch for {entry['fileName']}")
        part.rename(dest)
    with_retries(fetch, f"download {entry['fileName']}")


def file_date(name):
    """apc261018.zip -> date(2026, 10, 18); None for anything else."""
    m = DAILY_RE.match(name)
    return dt.datetime.strptime(m.group(1), "%y%m%d").date() if m else None


def loaded_files(db):
    return {row[0].split("/")[0] for row in db.execute("SELECT source_file FROM processed_files")}


def write_status(db, loaded_now, failed):
    dates = [d for d in map(file_date, loaded_files(db)) if d]
    try:
        previous = json.loads(STATUS_PATH.read_text())
    except (OSError, ValueError):
        previous = {}
    if loaded_now or "case_files" not in previous:
        # Both are full scans of 14 million rows (about 10 minutes), so only redo them when something changed.
        total, latest = db.execute("SELECT count(*), max(transaction_date) FROM case_files").fetchone()
    else:
        total, latest = previous["case_files"], previous["latest_transaction_date"]
    status = {
        "daily_files_through": max(dates).isoformat() if dates else None,
        "latest_transaction_date": latest,
        "case_files": total,
        "last_run": dt.datetime.now().isoformat(timespec="seconds"),
        "loaded_this_run": loaded_now,
        "failed_this_run": failed,
    }
    STATUS_PATH.write_text(json.dumps(status, indent=2) + "\n")
    return status


_lock = None


def take_lock():
    """One writer at a time: sync_daily.py and build_db.py share this lock. Released when the process exits."""
    global _lock
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    _lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def main():
    dry_run = "--dry-run" in sys.argv[1:]
    if not DB_PATH.exists():
        sys.exit(f"{DB_PATH} not found: run setup.sh, then sync/build_db.py if setup asks for it")
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    if not take_lock():
        log("another sync or build is running; exiting")
        return

    db = tm_parser.connect(DB_PATH)
    done = loaded_files(db)
    dates = [d for d in map(file_date, done) if d]
    if not dates:
        sys.exit("no daily files recorded in processed_files: the database doesn't look like a TrademarkBench clone")
    newest = max(dates)
    today = dt.date.today()
    key = api_key()
    entries = file_list(key, (newest - dt.timedelta(days=LOOKBACK_DAYS)).isoformat(), today.isoformat())
    queue = sorted((e for e in entries if file_date(e["fileName"]) and e["fileName"] not in done),
                   key=lambda e: e["fileName"])
    log(f"newest file loaded: {newest}; {len(queue)} new daily file(s) available")
    if dry_run:
        for e in queue:
            log(f"  would load {e['fileName']} ({e['fileSize'] / 1e6:.1f} MB)")
        return

    loaded_now, failed = [], []
    for e in queue:
        name = e["fileName"]
        dest = DOWNLOAD_DIR / name
        log(f"{name} ({e['fileSize'] / 1e6:.1f} MB)")
        try:
            if not dest.exists():
                download(key, e, dest)
            tm_parser.load_file(db, dest)
        except Exception as exc:  # keep going; the transaction-date rule makes later files safe to load
            log(f"  FAILED {name}: {exc}")
            failed.append(name)
            dest.unlink(missing_ok=True)
            continue
        dest.unlink()
        loaded_now.append(name)

    if loaded_now:
        # The FTS tables are external-content indexes, and INSERT OR REPLACE gives an updated case a new
        # rowid, so a full rebuild is the only way to keep them exact. It takes about 12 minutes.
        tm_parser.build_search_index(db)
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    status = write_status(db, loaded_now, failed)
    db.close()
    log(f"done: {len(loaded_now)} loaded, {len(failed)} failed; daily files through "
        f"{status['daily_files_through']}, {status['case_files']:,} case files")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
