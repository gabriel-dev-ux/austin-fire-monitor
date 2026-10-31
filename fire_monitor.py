#!/usr/bin/env python3
"""Austin fire, Reddit and news alerts. Standard-library Python; no paid APIs."""
import argparse
import copy
import hashlib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
STATE_FILE = Path(os.environ.get("FIRE_STATE_FILE", str(ROOT / "state.json")))
USER_AGENT = "austin-fire-monitor/2.0 (+https://github.com/gabriel-dev-ux/austin-fire-monitor)"
LOCAL_TZ = ZoneInfo("America/Chicago")
ATOM = {"a": "http://www.w3.org/2005/Atom"}


def log(message):
    print(datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


def iso(now=None):
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def parse_time(value):
    value = str(value or "")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        result = parsedate_to_datetime(value)
    if result.tzinfo is None:
        raise ValueError("Missing timezone in source timestamp")
    return result.astimezone(timezone.utc)


def normalize(value):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", html.unescape(str(value)).lower())).strip()


def fingerprint(*parts):
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:32]


def escape(value):
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def request(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as response:
        return response.read()


def load_state(path=STATE_FILE):
    if not path.exists():
        return {"version": 1, "sources": {}, "seen": {}, "pending": {}}
    # Fail visibly rather than silently resetting a damaged deduplication file.
    state = json.loads(path.read_text())
    if not isinstance(state, dict) or state.get("version") != 1:
        raise ValueError("Unsupported monitor state")
    for key in ("sources", "seen", "pending"):
        if not isinstance(state.get(key), dict):
            raise ValueError("Invalid state section: " + key)
    return state


def save_state(state, path=STATE_FILE):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def load_config():
    cfg = json.loads((ROOT / "config.json").read_text())
    cfg["signal_re"] = re.compile(cfg["signal_pattern"], re.I)
    cfg["exclude_re"] = re.compile(cfg["exclude_pattern"], re.I)
    return cfg


def fire_url(cfg):
    query = {"$where": "traffic_report_status='ACTIVE' AND agency='FIRE'",
             "$order": "published_date DESC", "$limit": "5000"}
    return cfg["fire_api"] + "?" + urllib.parse.urlencode(query)


def news_url(cfg):
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode({
        "q": cfg["news_query"], "hl": "en-US", "gl": "US", "ceid": "US:en"})


def parse_fire(body, cfg):
    rows = json.loads(body)
    if not isinstance(rows, list):
        raise ValueError("Fire API did not return a list")
    if len(rows) >= 5000:
        raise ValueError("Fire query reached its cap; review pagination before continuing")
    items = []
    for row in rows:
        required = ("traffic_report_id", "published_date", "issue_reported", "traffic_report_status", "agency")
        if not all(key in row for key in required):
            raise ValueError("Official fire-feed schema changed")
        if row["traffic_report_status"] != "ACTIVE" or row["agency"] != "FIRE":
            continue
        issue = row["issue_reported"]
        serious = any(re.search(pattern, issue, re.I) for pattern in cfg["fire_patterns"])
        items.append({"id": fingerprint("fire", row["traffic_report_id"], normalize(issue)),
            "title": issue, "published": row["published_date"], "address": row.get("address", ""),
            "link": cfg["fire_page"], "publisher": "Austin Fire dispatch", "qualifies": serious,
            "label": "OFFICIAL DISPATCH — PRELIMINARY"})
    return items


def parse_reddit(body, cfg):
    root = ET.fromstring(body)
    if root.tag != "{" + ATOM["a"] + "}feed":
        raise ValueError("Reddit did not return an Atom feed")
    items = []
    for entry in root.findall("a:entry", ATOM):
        title = html.unescape(entry.findtext("a:title", "", ATOM)).strip()
        links = entry.findall("a:link", ATOM)
        link = next((x.get("href", "") for x in links if x.get("rel", "alternate") == "alternate"), "")
        if (urllib.parse.urlparse(link).hostname or "").lower() not in ("www.reddit.com", "reddit.com"):
            continue
        identifier = entry.findtext("a:id", link, ATOM)
        items.append({"id": fingerprint("reddit", identifier), "title": title,
            "published": entry.findtext("a:published", None, ATOM) or entry.findtext("a:updated", "", ATOM),
            "link": link, "publisher": "r/Austin", "qualifies": bool(cfg["signal_re"].search(title)) and not cfg["exclude_re"].search(title),
            "label": "UNVERIFIED REDDIT TIP"})
    return items


def publisher_allowed(url, cfg):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in cfg["publisher_domains"])


def parse_news(body, cfg):
    root = ET.fromstring(body)
    if root.find("channel") is None:
        raise ValueError("Google News did not return an RSS channel")
    items = []
    for item in root.findall("./channel/item"):
        source = item.find("source")
        publisher = html.unescape(source.text or "") if source is not None else ""
        publisher_url = source.get("url", "") if source is not None else ""
        title = html.unescape(item.findtext("title") or "").strip()
        suffix = " - " + publisher
        if publisher and title.lower().endswith(suffix.lower()):
            title = title[:-len(suffix)].strip()
        local = any(re.search(r"\b" + re.escape(term) + r"\b", title, re.I) for term in cfg["local_terms"])
        published = item.findtext("pubDate") or ""
        link = item.findtext("link") or ""
        if urllib.parse.urlparse(link).scheme != "https":
            continue
        # The headline and date suppress exact syndicated duplicates across publishers.
        try:
            date_key = parse_time(published).date().isoformat()
        except (ValueError, TypeError, OverflowError):
            date_key = "invalid"
        items.append({"id": fingerprint("news", normalize(title), date_key), "title": title,
            "published": published, "link": link, "publisher": publisher,
            "qualifies": bool(local and publisher_allowed(publisher_url, cfg) and cfg["signal_re"].search(title) and not cfg["exclude_re"].search(title)),
            "label": "LOCAL NEWS REPORT"})
    return items


def fetch_sources(cfg):
    jobs = {
        "fire": (fire_url(cfg), parse_fire),
        "reddit": (cfg["reddit_feed"], parse_reddit),
        "news": (news_url(cfg), parse_news),
    }
    results, errors = {}, {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(lambda u=url, p=parser: p(request(u), cfg)): name
                   for name, (url, parser) in jobs.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except (OSError, ValueError, TypeError, ET.ParseError) as exc:
                errors[name] = str(exc)
    return results, errors


def reconcile(state, results, errors, cfg, now=None, seed=False):
    now = now or datetime.now(timezone.utc)
    if seed:
        state["pending"] = {}
    for name, error in errors.items():
        state["sources"].setdefault(name, {})["last_error"] = error
        log(name + " unavailable; existing state retained: " + error)
    for name, items in results.items():
        prior = state["sources"].setdefault(name, {})
        initialized = prior.get("initialized", False) and not seed
        max_age = cfg["fire_max_age_hours"] if name == "fire" else cfg["max_age_hours"]
        for item in items:
            try:
                published = parse_time(item["published"])
            except (ValueError, TypeError, OverflowError):
                continue
            if not now - timedelta(hours=max_age) <= published <= now + timedelta(minutes=10):
                continue
            key = item["id"]
            if key in state["seen"]:
                continue
            state["seen"][key] = iso(now)
            if initialized and item["qualifies"]:
                state["pending"][key] = {"event": item, "delivered": [], "queued_at": iso(now)}
        state["sources"][name] = {"initialized": True, "last_success": iso(now), "records": len(items)}
        if not initialized:
            log(name + ": seeded current results without backfill")
    cutoff = now - timedelta(days=cfg["state_retention_days"])
    state["seen"] = {k:v for k,v in state["seen"].items() if parse_time(v) >= cutoff}
    # Old delivery failures must not turn into breaking alerts days later.
    for key, pending in list(state["pending"].items()):
        if parse_time(pending["queued_at"]) < now - timedelta(hours=cfg["max_age_hours"]):
            log("Expired undelivered alert: " + key)
            del state["pending"][key]
    return state


def format_alert(event):
    when = parse_time(event["published"]).astimezone(LOCAL_TZ).strftime("%b %d, %I:%M %p %Z")
    address = "\n" + escape(event["address"]) if event.get("address") else ""
    return "*{}*\n*{}*{}\n{} · <{}|{}>".format(
        event["label"], escape(event["title"]), address, when, event["link"], escape(event["publisher"]))


def post_slack(text, token, recipient):
    payload = json.dumps({"channel": recipient, "text": text, "unfurl_links": False}).encode()
    req = urllib.request.Request("https://slack.com/api/chat.postMessage", data=payload,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=30) as response:
        result = json.load(response)
    if not result.get("ok"):
        raise RuntimeError("Slack delivery failed: " + str(result.get("error", "unknown")))


def deliver(state, token, recipients, path=STATE_FILE):
    for key, pending in list(state["pending"].items())[:20]:
        for recipient in recipients:
            recipient_key = fingerprint("recipient", recipient)
            if recipient_key in pending["delivered"]:
                continue
            try:
                post_slack(format_alert(pending["event"]), token, recipient)
            except (OSError, ValueError, RuntimeError) as exc:
                log(str(exc) + "; pending alert retained for retry")
                save_state(state, path)
                return False
            pending["delivered"].append(recipient_key)
            save_state(state, path)
        del state["pending"][key]
        save_state(state, path)
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="preview without Slack access or state writes")
    parser.add_argument("--seed-only", action="store_true", help="checkpoint current feeds silently before activating a restored deployment")
    parser.add_argument("--check-sources", action="store_true", help="return failure if any feed is unavailable")
    args = parser.parse_args(argv)
    if args.dry_run and args.seed_only:
        parser.error("Choose either --dry-run or --seed-only")
    cfg = load_config()
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    recipients = list(dict.fromkeys(x for x in re.split(r"[,\s]+", os.environ.get("SLACK_USER_IDS", "")) if re.fullmatch(r"[UW][A-Z0-9]+", x)))
    if not (args.dry_run or args.seed_only) and (not token.startswith("xoxb-") or not recipients):
        log("ERROR: configure SLACK_BOT_TOKEN and SLACK_USER_IDS")
        return 2
    try:
        original = load_state()
        results, errors = fetch_sources(cfg)
        state = reconcile(copy.deepcopy(original), results, errors, cfg, seed=args.seed_only)
        if (args.seed_only or args.check_sources) and errors:
            log("Source validation failed; baseline not saved")
            return 1
        if not results:
            return 1
        if args.dry_run:
            for pending in state["pending"].values():
                log("dry-run: " + format_alert(pending["event"]))
        else:
            save_state(state)
            if not args.seed_only and not deliver(state, token, recipients):
                return 1
        log("complete: {} healthy source(s), {} unavailable, {} queued alert(s)".format(len(results), len(errors), len(state["pending"])))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        log("ERROR: " + str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
