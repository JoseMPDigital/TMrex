"""Build data/uspto.db from scratch from USPTO Open Data Portal bulk files.

Loads the newest annual backfile (TRTYRAP: every case file through its end date), then every daily
applications file (TRTDXFAP) published after that date, one file at a time:
download -> verify size -> parse into SQLite -> delete the zip. Re-running skips files already recorded
in processed_files, so it resumes after an interruption. After this, sync_daily.py keeps it current.

Needs a free USPTO API key in .env (USPTO_API_KEY=...), several hours, and about 100 GB of free disk:
the finished database is about 80 GB. Run it with the Mac kept awake:
  caffeinate -i python3 -I sync/build_db.py

Based on TrademarkBench's scripts/tm_build.py.
"""
import datetime as dt
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))  # -I drops the script dir
import tm_parser  # noqa: E402
import sync_daily as sd  # noqa: E402

BACKFILE_RE = re.compile(r"-(\d{8})-\d+\.zip$")


def product_files(key, product, query=""):
    def fetch():
        req = urllib.request.Request(f"{sd.API}/{product}?includeFiles=true{query}", headers={"X-API-KEY": key})
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.load(resp)["bulkDataProductBag"][0]["productFileBag"]["fileDataBag"]
    return sd.with_retries(fetch, f"{product} file list")


def main():
    key = sd.api_key()
    if not sd.take_lock():
        sys.exit("another build or sync is running")
    sd.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    sd.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    backfiles = [f for f in product_files(key, "TRTYRAP") if BACKFILE_RE.search(f["fileName"])]
    if not backfiles:
        sys.exit("no annual backfile found in TRTYRAP")
    edition = max(BACKFILE_RE.search(f["fileName"]).group(1) for f in backfiles)
    backfile = sorted((f for f in backfiles if f"-{edition}-" in f["fileName"]), key=lambda f: f["fileName"])
    through = dt.datetime.strptime(edition, "%Y%m%d").date()
    daily_from = (through + dt.timedelta(days=1)).isoformat()
    daily = sorted((f for f in product_files(key, "TRTDXFAP",
                                             f"&fileDataFromDate={daily_from}&fileDataToDate={dt.date.today().isoformat()}")
                    if sd.file_date(f["fileName"]) and sd.file_date(f["fileName"]) > through),
                   key=lambda f: f["fileName"])
    queue = backfile + daily
    sd.log(f"backfile through {through}: {len(backfile)} files; then {len(daily)} daily files; "
           f"{sum(f['fileSize'] for f in queue) / 1e9:.1f} GB to download")

    db = tm_parser.connect(sd.DB_PATH)
    done = sd.loaded_files(db)
    start = time.time()
    loaded, failed = [], []
    for i, entry in enumerate(queue, 1):
        name = entry["fileName"]
        if name in done:
            continue
        dest = sd.DOWNLOAD_DIR / name
        sd.log(f"[{i}/{len(queue)}] {name} ({entry['fileSize'] / 1e6:.0f} MB)")
        try:
            if not dest.exists():
                sd.download(key, entry, dest)
            tm_parser.load_file(db, dest)
        except Exception as exc:  # keep going; a rerun retries it
            sd.log(f"  FAILED {name}: {exc}")
            failed.append(name)
            dest.unlink(missing_ok=True)
            continue
        dest.unlink()
        loaded.append(name)
        sd.log(f"  elapsed {(time.time() - start) / 60:.1f} min")

    if failed:
        db.close()
        sys.exit(f"INCOMPLETE: {len(failed)} files failed ({', '.join(failed)}). Run this again to retry them.")
    if loaded or not sd.STATUS_PATH.exists():
        tm_parser.build_search_index(db)
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    status = sd.write_status(db, loaded, failed)
    db.close()
    sd.log(f"COMPLETE: {status['case_files']:,} case files; daily files through {status['daily_files_through']}")


if __name__ == "__main__":
    main()
