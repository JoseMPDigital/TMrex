"""Check whether domain names for a proposed mark are registered, using DNS and RDAP. Prints JSON.

  python3 -I search/domain_check.py bluewave [--tlds com,net,io,ai,co,app] [--names blue-wave,getbluewave]

For each name and TLD:
  - DNS: does the domain resolve? A domain that resolves is registered and probably in use.
  - RDAP, the structured successor to WHOIS: is it registered, and if so with which registrar, since
    when, and with what status? The RDAP server for each TLD comes from IANA's bootstrap file, cached
    in data/rdap_dns.json for a week.

"registered" is what RDAP says. RDAP "not found" means likely unregistered, not guaranteed available:
premium and reserved names also return not found, so confirm with a registrar before relying on it.
Some country-code TLDs have no RDAP server; those report "unknown" with the DNS result only.
Needs network access to data.iana.org and each TLD's RDAP server.
"""
import argparse
import json
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"
BOOTSTRAP_CACHE = ROOT / "data" / "rdap_dns.json"
CACHE_DAYS = 7
DEFAULT_TLDS = ["com", "net", "org", "io", "ai", "co", "app", "us"]
HEADERS = {"Accept": "application/rdap+json", "User-Agent": "tmrex-domain-check/1.0"}


def bootstrap():
    """TLD -> RDAP base URL, from IANA's bootstrap file."""
    if BOOTSTRAP_CACHE.exists() and time.time() - BOOTSTRAP_CACHE.stat().st_mtime < CACHE_DAYS * 86400:
        data = json.loads(BOOTSTRAP_CACHE.read_text())
    else:
        with urllib.request.urlopen(urllib.request.Request(BOOTSTRAP_URL, headers=HEADERS), timeout=30) as resp:
            data = json.load(resp)
        BOOTSTRAP_CACHE.parent.mkdir(parents=True, exist_ok=True)
        BOOTSTRAP_CACHE.write_text(json.dumps(data))
    servers = {}
    for tlds, urls in data["services"]:
        url = next((u for u in urls if u.startswith("https")), urls[0])
        for tld in tlds:
            servers[tld.lower()] = url if url.endswith("/") else url + "/"
    return servers


def resolves(domain):
    try:
        socket.getaddrinfo(domain, None)
        return True
    except socket.gaierror:
        return False


def rdap(base, domain):
    url = f"{base}domain/{domain}"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=20) as resp:
                return parse_rdap(json.load(resp))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {"rdap": "not found"}
            if exc.code == 429 and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            return {"rdap": "error", "error": f"HTTP {exc.code}"}
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if attempt < 2:
                time.sleep(2)
                continue
            return {"rdap": "error", "error": str(exc)}


def parse_rdap(data):
    events = {e.get("eventAction"): (e.get("eventDate") or "")[:10] for e in data.get("events", [])}
    registrar = None
    for ent in data.get("entities", []):
        if "registrar" in (ent.get("roles") or []):
            vcard = (ent.get("vcardArray") or [None, []])[1]
            registrar = next((v[3] for v in vcard if v and v[0] == "fn"), None) or ent.get("handle")
    return {
        "rdap": "registered",
        "registrar": registrar,
        "registered_on": events.get("registration"),
        "expires_on": events.get("expiration"),
        "status": data.get("status", []),
        "nameservers": [ns.get("ldhName", "").lower() for ns in data.get("nameservers", [])],
    }


def check(domain, servers):
    tld = domain.rsplit(".", 1)[1]
    result = {"domain": domain, "resolves": resolves(domain)}
    base = servers.get(tld)
    result.update(rdap(base, domain) if base else {"rdap": "unknown", "error": f"no RDAP server listed for .{tld}"})
    if result["rdap"] == "registered":
        result["summary"] = "registered" + (", resolves" if result["resolves"] else ", doesn't resolve")
    elif result["rdap"] == "not found":
        result["summary"] = "likely available" if not result["resolves"] else "check: RDAP not found but it resolves"
    else:
        result["summary"] = "registered (resolves)" if result["resolves"] else "unknown"
    return result


def label(name):
    """A mark to a domain label: lowercase letters, digits and hyphens."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]", "", name.casefold().replace("&", "and").replace(" ", ""))).strip("-")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("mark", help="the mark; spaces and punctuation are removed (BLUE WAVE -> bluewave)")
    p.add_argument("--tlds", default=",".join(DEFAULT_TLDS), help="comma-separated TLDs")
    p.add_argument("--names", default="", help="extra labels to check, comma-separated (blue-wave,getbluewave)")
    args = p.parse_args()
    names = list(dict.fromkeys(n for n in [label(args.mark)] + [label(x) for x in args.names.split(",")] if n))
    tlds = [t.strip().lstrip(".").lower() for t in args.tlds.split(",") if t.strip()]
    if not names:
        sys.exit("no usable domain label in the mark")
    try:
        servers = bootstrap()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        servers = {}
        print(f"warning: couldn't load the RDAP bootstrap ({exc}); DNS results only", file=sys.stderr)
    domains = [f"{n}.{t}" for n in names for t in tlds]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda d: check(d, servers), domains))
    print(json.dumps({"checked_on": time.strftime("%Y-%m-%d"), "results": results,
                      "note": "RDAP 'not found' means likely unregistered, not guaranteed available (premium and "
                              "reserved names also return not found). Confirm with a registrar."}, indent=2))


if __name__ == "__main__":
    main()
