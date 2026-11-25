#!/usr/bin/env python3
"""AutoLink Messenger: a LinkedIn outreach assistant.

Imports your LinkedIn connections export, segments it, drafts personalized
messages for a campaign (template-based, optionally refined by Claude), and
walks you through sending them one at a time: the message is copied to your
clipboard and the person's profile opens in your browser, you paste and press
Send in LinkedIn yourself, and the tool records the result.

Two ways to send:
  send      assisted - you press Send in LinkedIn yourself (no automation)
  autosend  fully automated - drives your logged-in browser and clicks Send,
            with a daily cap, pacing between messages, and a hard stop on any
            LinkedIn security check. Automated messaging is against LinkedIn's
            User Agreement and can get your account restricted.
Run `python outreach.py --help` for usage.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import datetime as dt
import importlib.util
import io
import json
import os
import random
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import unquote, urlparse

HERE = Path(__file__).resolve().parent


def load_env_file(path: Path = HERE / ".env"):
    """Read KEY=VALUE lines from a .env next to this file (gitignored), without overriding
    the real environment. Keeps the API key out of shell history and the web app's command."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.partition("=")[::2]
        k, v = k.strip(), v.strip().strip("'\"")
        if v and (not os.environ.get(k) or os.environ[k].endswith("...")):  # "..." = unfilled placeholder
            os.environ[k] = v


load_env_file()

# Next to this file by default, so the tool finds your data whatever folder you start it from.
DEFAULT_DB = os.environ.get("OUTREACH_DB") or str(HERE / "outreach.db")
DEFAULT_DAILY_CAP = 20    # messages per rolling 24h, across campaigns
LINKEDIN_MAX_CHARS = 8000
NOTE_CHARS = 200          # a connection request's note on a free account (LinkedIn's box says the real limit)
NOTE_CHARS_PREMIUM = 300
DEFAULT_INVITE_CAP = 15   # connection requests per rolling 24h; LinkedIn allows roughly 100 a week
ACCEPT_LOOKBACK = 100  # how many of your newest connections to look through for people who accepted
AI_MODEL = os.environ.get("OUTREACH_AI_MODEL") or "claude-opus-5-5"

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY,
    key TEXT UNIQUE NOT NULL,
    first_name TEXT, last_name TEXT, url TEXT, email TEXT,
    company TEXT, position TEXT, connected_on TEXT,
    do_not_contact INTEGER NOT NULL DEFAULT 0,
    tags TEXT,  -- ",founder,travel," as set by `tag`; NULL until the person has been sorted
    prospect INTEGER NOT NULL DEFAULT 0  -- 1: not a connection yet; 2: invited without a note, awaiting their answer
);
CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    template TEXT NOT NULL,
    brief TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',  -- draft | sent | skipped | replied | failed
    sent_at TEXT,
    error TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (campaign_id, contact_id)
);
CREATE TABLE IF NOT EXISTS profiles (  -- what `enrich` read from each person's LinkedIn profile
    contact_id INTEGER PRIMARY KEY REFERENCES contacts(id),
    headline TEXT, location TEXT, about TEXT, experience TEXT, education TEXT,
    fetched_at TEXT NOT NULL
);
"""

STATUSES = ("draft", "sent", "skipped", "replied", "failed")


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


SCHEMA_VERSION = 1  # raise it whenever migrate() learns something new


def connect(path: str) -> sqlite3.Connection:
    # The web page reads while a sending run writes: wait for the other side instead of failing.
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    if db.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        migrate(db)
    return db


def migrate(db):
    """Create the tables and bring an older database up to date. Runs once per database (the
    version is stored in it), so opening one costs nothing: the web page opens it on every refresh."""
    db.executescript(SCHEMA)
    cols = {r[1] for r in db.execute("PRAGMA table_info(messages)")}
    for col in ("error",        # databases created before autosend existed
                "ai_at",        # ...and before `personalize`
                "base_body",    # the template text, before Claude rewrote it
                "reply",        # what `replies` saw them answer
                "thread",       # the conversation already there when the message was sent
                "kind",         # 'invite' when it went out as the note on a connection request
                "invited_at"):  # when a connection request went to this person, with or without a note
        if col not in cols:
            db.execute(f"ALTER TABLE messages ADD COLUMN {col} TEXT")
            if col == "base_body":
                db.execute("UPDATE messages SET base_body=body")
    contact_cols = {r[1] for r in db.execute("PRAGMA table_info(contacts)")}
    if "tags" not in contact_cols:
        db.execute("ALTER TABLE contacts ADD COLUMN tags TEXT")
    if "prospect" not in contact_cols:
        db.execute("ALTER TABLE contacts ADD COLUMN prospect INTEGER NOT NULL DEFAULT 0")
    merge_duplicate_contacts(db)
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    db.commit()


def contact_key(url: str, first: str = "", last: str = "") -> str:
    """One key per person: their profile link, decoded (LinkedIn writes "élise" in one place and
    "%C3%A9lise" in another), or their name when there's no link."""
    return unquote(url or "").rstrip("/").lower() or f"name:{first} {last}".lower()


def merge_duplicate_contacts(db):
    """Folds contacts stored twice under differently encoded links into one, so nobody gets a
    campaign's message twice. The row that was already messaged is kept; the other is marked
    do-not-contact and loses its unsent drafts. Only databases from before contact_key() decoded
    links can hold such pairs, so this runs once, from migrate()."""
    groups = {}
    for c in db.execute("SELECT id, key, do_not_contact FROM contacts WHERE key NOT LIKE 'merged:%'"):
        groups.setdefault(contact_key(c["key"]) if not c["key"].startswith("name:") else c["key"], []).append(c)
    for key, rows in groups.items():
        if len(rows) < 2:
            if rows[0]["key"] != key:  # stored before keys were decoded
                db.execute("UPDATE contacts SET key=? WHERE id=?", (key, rows[0]["id"]))
            continue
        messaged = {r[0] for r in db.execute(
            f"SELECT DISTINCT contact_id FROM messages WHERE status IN ('sent','replied') AND contact_id IN "
            f"({','.join('?' * len(rows))})", [r["id"] for r in rows])}
        keep = min(rows, key=lambda r: (r["id"] not in messaged, r["id"]))
        for r in rows:
            if r["id"] != keep["id"]:
                db.execute("UPDATE contacts SET key=?, do_not_contact=1 WHERE id=?", (f"merged:{r['id']}", r["id"]))
                db.execute("DELETE FROM messages WHERE contact_id=? AND status='draft'", (r["id"],))
        db.execute("UPDATE contacts SET key=?, do_not_contact=? WHERE id=?",
                   (key, max(r["do_not_contact"] for r in rows), keep["id"]))
    db.commit()


def parse_ids(text) -> list[int]:
    """'12, 40,' -> [12, 40]."""
    try:
        return [int(x) for x in re.split(r"[\s,]+", str(text or "")) if x]
    except ValueError:
        raise SystemExit(f"Expected contact ids separated by commas (like 12,40), got {text!r}.")


def get_campaign(db, name: str):
    """The campaign row, or a clear exit that lists the campaigns that do exist."""
    camp = db.execute("SELECT * FROM campaigns WHERE name=?", (name,)).fetchone()
    if camp is None:
        names = [r[0] for r in db.execute("SELECT name FROM campaigns ORDER BY id DESC")]
        raise SystemExit(f"No campaign named '{name}'. "
                         + (f"Your campaigns: {', '.join(names)}." if names else "Create one with `draft` first."))
    return camp


def stop_flag() -> threading.Event:
    """Set when webapp.py stops this process with SIGTERM. Only a flag: interrupting a
    browser call mid-flight can leave Playwright unable to close the browser cleanly."""
    stop = threading.Event()
    with contextlib.suppress(ValueError):  # only possible from the main thread
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
    return stop


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------

def parse_date(value: str) -> str | None:
    value = (value or "").strip()
    for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return dt.datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def read_connections_csv(path: str) -> list[dict]:
    """Read LinkedIn's Connections.csv, skipping its 'Notes:' preamble."""
    text = Path(path).read_text(encoding="utf-8-sig")
    lines = text.splitlines(keepends=True)
    start = next((i for i, l in enumerate(lines) if l.lstrip().startswith("First Name")), None)
    if start is None:
        raise SystemExit(f"{path}: no 'First Name' header row found - is this LinkedIn's Connections.csv?")
    rows = []
    for r in csv.DictReader(io.StringIO("".join(lines[start:]))):
        r = {(k or "").strip(): (v or "").strip() for k, v in r.items()}
        if not (r.get("First Name") or r.get("Last Name")):
            continue
        rows.append({
            "first_name": r.get("First Name", ""),
            "last_name": r.get("Last Name", ""),
            "url": r.get("URL", ""),
            "email": r.get("Email Address", ""),
            "company": r.get("Company", ""),
            "position": r.get("Position", ""),
            "connected_on": parse_date(r.get("Connected On", "")),
        })
    return rows


CONTACT_FIELDS = ("first_name", "last_name", "url", "email", "company", "position", "connected_on")


def save_contacts(db, rows, fill_blanks_only=False, prospect=False) -> tuple[int, int]:
    """Insert or update contacts by profile URL. fill_blanks_only keeps existing values
    (used for the scraped list, which is less complete than LinkedIn's CSV export).

    prospect=True adds people you aren't connected to yet; someone already known keeps their
    standing. Importing connections clears it, so an accepted invitation becomes a connection."""
    if fill_blanks_only or prospect:
        sets = ", ".join(f"{c}=COALESCE(NULLIF({c}, ''), excluded.{c})" for c in CONTACT_FIELDS)
    else:
        sets = ", ".join(f"{c}=excluded.{c}" for c in CONTACT_FIELDS)
    if not prospect:
        sets += ", prospect=0"
    added = updated = 0
    for r in rows:
        key = contact_key(r["url"], r["first_name"], r["last_name"])
        exists = db.execute("SELECT 1 FROM contacts WHERE key=?", (key,)).fetchone()
        db.execute(
            f"""INSERT INTO contacts (key, {", ".join(CONTACT_FIELDS)}, prospect) VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET {sets}""",
            (key, *(r[c] for c in CONTACT_FIELDS), int(prospect)),
        )
        updated += bool(exists)
        added += not exists
    db.commit()
    return added, updated


def cmd_import(db, args):
    rows = read_connections_csv(args.csv)
    added, updated = save_contacts(db, rows)
    print(f"Imported {len(rows)} connections ({added} new, {updated} updated).")


PROFILE_URL = re.compile(r"(?:(https?)://)?([a-z0-9.:-]+)/in/([^\s,;?#/]+)[^\s,;]*", re.I)


def parse_prospects(text: str) -> list[dict]:
    """People to connect with, one per line: a LinkedIn profile link, optionally with their
    name, company and title on the same line, separated by commas or tabs."""
    rows = {}
    for line in text.splitlines():
        m = PROFILE_URL.search(line)
        if not m:
            continue
        scheme, host, slug = m.groups()
        # every spelling of a LinkedIn link (no scheme, country subdomain, tracking query) is one person
        url = (f"https://www.linkedin.com/in/{slug}" if host.lower().endswith("linkedin.com")
               else f"{scheme or 'https'}://{host}/in/{slug}")
        rest = [x.strip() for x in re.split(r"[,;\t]", line[:m.start()] + "," + line[m.end():]) if x.strip()]
        first, _, last = (rest[0] if rest else "").partition(" ")
        rows.setdefault(url.lower(), {
            "first_name": first, "last_name": last.strip(), "url": url, "email": "",
            "company": rest[1] if len(rest) > 1 else "", "position": rest[2] if len(rest) > 2 else "",
            "connected_on": None})
    return list(rows.values())


def cmd_add_prospects(db, args, page_factory=None, sleep=time.sleep):
    """Add people you aren't connected to yet. A campaign drafted for them (`draft --prospects`)
    goes out as connection requests, each message as the note."""
    import linkedin_browser as lb
    text = "\n".join(args.urls or [])
    if getattr(args, "file", None):
        text += "\n" + Path(args.file).read_text(encoding="utf-8-sig")
    rows = parse_prospects(text)
    if not rows:
        raise SystemExit("No LinkedIn profile links found (they look like https://www.linkedin.com/in/someone).")
    known = {r["key"]: r for r in db.execute("SELECT key, prospect FROM contacts")}
    new = [r for r in rows if contact_key(r["url"]) not in known]
    connected = [r for r in rows if contact_key(r["url"]) in known and not known[contact_key(r["url"])]["prospect"]]
    nameless = [r for r in new if not r["first_name"]]
    if nameless:  # only a link: read their name and headline from the profile
        if page_factory is None:
            def page_factory():
                return lb.browser(args.profile_dir, headless=args.headless, channel=args.channel)
        with page_factory() as page:
            lb.ensure_logged_in(page, lb.feed_url_for(nameless[0]["url"]))
            for i, r in enumerate(nameless):
                if i:
                    sleep(random.uniform(2, 4))
                try:
                    card = lb.read_card(page, r["url"])
                except lb.StopRun as e:
                    print(f"STOPPED: {e}")
                    break
                except Exception as e:
                    print(f"  ! {r['url']}: couldn't read the profile ({type(e).__name__})")
                    continue
                first, _, last = card["name"].partition(" ")
                r.update(first_name=first, last_name=last.strip(), position=r["position"] or card["headline"])
                if "1st" in card["degree"]:
                    new.remove(r)
                    connected.append(r)
                    save_contacts(db, [r], fill_blanks_only=True)
                print(f"  {card['name']} ({card['degree'].strip('· ') or 'degree unknown'})")
    ready = [r for r in new if r["first_name"]]
    added, _ = save_contacts(db, ready, prospect=True)
    print(f"Added {added} {'person' if added == 1 else 'people'} to reach with a connection request"
          + (f"; {len(connected)} already connected to you" if connected else "")
          + (f"; {len(rows) - len(new) - len(connected)} already on the list" if len(rows) - len(new) - len(connected) else "")
          + (f"; {len(new) - len(ready)} skipped (no name found)" if len(new) - len(ready) else "") + ".")
    if added:
        print("Next: draft --prospects <campaign> --template <a short note>, then autosend.")


def cmd_import_linkedin(db, args, page_factory=None, should_stop=lambda: False, sleep=time.sleep):
    import linkedin_browser as lb
    if page_factory is None:
        def page_factory():
            return lb.browser(args.profile_dir, headless=args.headless, channel=args.channel)
    # Each batch is saved as soon as it's read, so a stop, a crash or a kill loses nothing.
    stats = {"n": 0, "added": 0, "updated": 0}

    def save(batch):
        rows = [{**{k: c[k] for k in ("first_name", "last_name", "url", "company", "position")},
                 "email": "", "connected_on": parse_date(c["connected_on"])} for c in batch]
        added, updated = save_contacts(db, rows, fill_blanks_only=True)
        stats["n"] += len(rows)
        stats["added"] += added
        stats["updated"] += updated

    stop = stop_flag()
    print("Reading your connections list on LinkedIn (read-only, scrolls through it)...")
    try:
        with page_factory() as page:
            lb.collect_connections(page, url=args.url or lb.CONNECTIONS_URL, max_contacts=args.max,
                                   sleep=sleep, should_stop=lambda: stop.is_set() or should_stop(), on_batch=save)
    except KeyboardInterrupt:
        stop.set()
    finally:
        if stop.is_set():
            print("Stopped.")
        if stats["n"]:
            print(f"Imported {stats['n']} connections from LinkedIn ({stats['added']} new, "
                  f"{stats['updated']} already known). Title and company come from each person's "
                  "headline, so they're approximate.")


# --------------------------------------------------------------------------
# Segmenting
# --------------------------------------------------------------------------

def add_filter_args(p):
    g = p.add_argument_group("filters (repeatable flags are OR-ed; matching is case-insensitive substring)")
    g.add_argument("--company", action="append", default=[], help="company contains")
    g.add_argument("--title", action="append", default=[], help="position/title contains")
    g.add_argument("--exclude-company", action="append", default=[])
    g.add_argument("--exclude-title", action="append", default=[])
    g.add_argument("--tag", action="append", default=[], help="has this tag (see `tag`)")
    g.add_argument("--exclude-tag", action="append", default=[])
    g.add_argument("--since", help="connected on or after YYYY-MM-DD")
    g.add_argument("--before", help="connected before YYYY-MM-DD")
    g.add_argument("--ids", help="comma-separated contact ids")
    g.add_argument("--limit", type=int)
    g.add_argument("--prospects", action="store_true",
                   help="people you aren't connected to yet (see add-prospects) instead of your connections")


def select_contacts(db, args, extra_where: str = "", extra_params=()):
    # Connections and not-yet-connections never mix: one gets a message, the other a short note.
    where, params = ["do_not_contact = 0", "prospect = ?"], [int(bool(getattr(args, "prospects", False)))]

    def any_like(col, values, negate=False):
        if not values:
            return
        clause = " OR ".join(f"LOWER(COALESCE({col},'')) LIKE ?" for _ in values)
        where.append(f"NOT ({clause})" if negate else f"({clause})")
        params.extend(f"%{v.lower()}%" for v in values)

    any_like("company", args.company)
    any_like("position", args.title)
    any_like("company", args.exclude_company, negate=True)
    any_like("position", args.exclude_title, negate=True)
    for values, negate in ((getattr(args, "tag", []), False), (getattr(args, "exclude_tag", []), True)):
        if values:
            clause = " OR ".join("COALESCE(tags,'') LIKE ?" for _ in values)
            where.append(f"NOT ({clause})" if negate else f"({clause})")
            params.extend(f"%,{v.strip().lower()},%" for v in values)
    if args.since:
        where.append("connected_on >= ?")
        params.append(args.since)
    if args.before:
        where.append("connected_on < ?")
        params.append(args.before)
    if args.ids:
        ids = parse_ids(args.ids)
        where.append(f"id IN ({','.join('?' * len(ids))})")
        params.extend(ids)
    if extra_where:
        where.append(extra_where)
        params.extend(extra_params)
    sql = f"SELECT * FROM contacts WHERE {' AND '.join(where)} ORDER BY connected_on DESC, id"
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"
    return db.execute(sql, params).fetchall()


def cmd_list(db, args):
    rows = select_contacts(db, args)
    for c in rows:
        name = f"{c['first_name']} {c['last_name']}"
        print(f"{c['id']:>5}  {name[:26]:<26}  {c['position'][:40]:<40}  "
              f"{c['company'][:28]:<28}  {c['connected_on'] or ''}  {' '.join(tags_of(c))}")
    print(f"\n{len(rows)} contact(s) match.")


def cmd_dnc(db, args):
    ids = parse_ids(args.ids)
    db.executemany("UPDATE contacts SET do_not_contact=? WHERE id=?", [(0 if args.undo else 1, i) for i in ids])
    db.commit()
    print(f"{'Cleared' if args.undo else 'Set'} do-not-contact on {len(ids)} contact(s).")


# --------------------------------------------------------------------------
# Tagging: sorting people into groups with Claude
# --------------------------------------------------------------------------
# Keyword filters misfire ("remote" matches remote patient monitoring). `tag` has Claude read
# each headline in context and assign the groups you define, 25 people per call. Tags are
# stored as ",a,b," so a filter can match a whole tag.

TAG_BATCH = 25


def tags_of(contact) -> list[str]:
    return [t for t in (contact["tags"] or "").split(",") if t]


def tags_value(tags) -> str:
    return "," + ",".join(dict.fromkeys(tags)) + "," if tags else ","


def parse_tag_spec(spec: str) -> list[tuple[str, str]]:
    """'founder: started a company\ntravel: works in travel or hospitality' -> [(name, description)].
    One tag per line, or a comma-separated list of names when there are no descriptions."""
    parts = spec.splitlines() if "\n" in spec.strip() or ":" in spec else spec.split(",")
    out = []
    for part in parts:
        name, _, desc = part.partition(":")
        name = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-")
        if name and name not in [n for n, _ in out]:
            out.append((name, desc.strip()))
    return out


def tagging_prompt(tags, people) -> str:
    groups = "\n".join(f"- {n}: {d}" if d else f"- {n}" for n, d in tags)
    rows = "\n".join(f"{p['id']}: {p['first_name']} {p['last_name']} | {p['position'] or '-'} | {p['company'] or '-'}"
                      for p in people)
    return ("Sort these LinkedIn contacts into groups from their headline and company.\n\n"
            f"Groups:\n{groups}\n\n"
            "Rules: read each headline in context, not by keyword (someone in remote patient monitoring is "
            "not a remote worker; a student of finance is not an investor). A person may belong to several "
            "groups, or to none when nothing fits. Use only the group names above, exactly as written.\n\n"
            f"Contacts (id: name | headline | company):\n{rows}\n\n"
            'Reply with JSON only: an object mapping every id to a list of group names, '
            'e.g. {"12": ["founder"], "13": []}.')


def parse_tag_reply(reply: str, allowed, ids) -> dict:
    m = re.search(r"\{.*\}", reply or "", re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        data = {}
    out = {}
    for k, v in data.items():
        try:
            cid = int(k)
        except (TypeError, ValueError):
            continue
        if cid not in ids:
            continue
        v = [v] if isinstance(v, str) else (v or [])
        out[cid] = [t for t in dict.fromkeys(str(x).strip().lower() for x in v) if t in allowed]
    return out


def cmd_tag(db, args, ai=None):
    tags = parse_tag_spec(args.tags or "")
    if not tags:
        raise SystemExit("Pass --tags 'founder: started a company' (one per line, or a comma-separated list).")
    allowed = {n for n, _ in tags}
    people = select_contacts(db, args, "" if args.redo else "tags IS NULL")
    if args.max:
        people = people[:args.max]
    if not people:
        print("Everyone matching is tagged already (use --redo to sort them again).")
        return
    ai = ai or ai_backend()
    stop = stop_flag()
    print(f"Sorting {len(people)} people into {', '.join(allowed)} with {ai.name}...")
    counts, done = {n: 0 for n, _ in tags}, 0
    for i in range(0, len(people), TAG_BATCH):
        if stop.is_set():
            print("Stopped.")
            break
        batch = people[i:i + TAG_BATCH]
        ids = {p["id"] for p in batch}
        try:
            result = parse_tag_reply(ai.generate(tagging_prompt(tags, batch)), allowed, ids)
        except Exception as e:
            print(f"  x people {batch[0]['id']}-{batch[-1]['id']}: {type(e).__name__}: {e}")
            if "authentication" in type(e).__name__.lower() or "401" in str(e):
                print("STOPPED: Claude rejected the API key. Check ANTHROPIC_API_KEY.")
                break
            continue
        if not result:
            print(f"  x people {batch[0]['id']}-{batch[-1]['id']}: Claude's answer wasn't usable; skipped")
            continue
        for p in batch:  # an id Claude left out gets no groups, so it isn't asked about again
            chosen = result.get(p["id"], [])
            db.execute("UPDATE contacts SET tags=? WHERE id=?", (tags_value(chosen), p["id"]))
            for t in chosen:
                counts[t] += 1
        db.commit()
        done += len(batch)
        print(f"  {done}/{len(people)}", end="\r")
    summary = ", ".join(f"{n} {c}" for n, c in counts.items())
    print(f"Sorted {done} of {len(people)} people: {summary}. "
          "Filter with --tag NAME; send to the most important first with draft --priority.")


def prioritize(contacts, priority) -> list:
    """Stable-sort contacts by the first of their tags that appears in `priority` (a comma list);
    untagged or unmatched people come last."""
    order = [t.strip().lower() for t in (priority or "").split(",") if t.strip()]
    if not order:
        return list(contacts)
    rank = {t: i for i, t in enumerate(order)}
    return sorted(contacts, key=lambda c: min((rank[t] for t in tags_of(c) if t in rank), default=len(order)))


# --------------------------------------------------------------------------
# Drafting
# --------------------------------------------------------------------------

PLACEHOLDER = re.compile(r"\{(\w+)(?:\|([^}]*))?\}")


def render(template: str, contact, sender: dict) -> str:
    """Fill {field} / {field|fallback} placeholders from the contact and sender."""
    values = {k: (contact[k] or "") for k in contact.keys()}
    values.update({f"sender_{k}": v for k, v in sender.items()})
    values["first_name"] = values.get("first_name", "").split(" ")[0].title() if values.get("first_name") else ""

    def sub(m):
        val = values.get(m.group(1))
        if val is None and m.group(2) is None:
            raise KeyError(f"Unknown placeholder {{{m.group(1)}}}")
        return val or (m.group(2) or "")

    return PLACEHOLDER.sub(sub, template).strip()


# --- AI personalization ------------------------------------------------------------
# Two ways to reach Claude: the API (ANTHROPIC_API_KEY + `pip install anthropic`, billed per
# token) or the Claude Code CLI already logged in on this computer (uses that plan's usage).

class ClaudeAPI:
    name = "the Claude API"

    def __init__(self):
        import anthropic  # ImportError if not installed
        self.client = anthropic.Anthropic()

    def generate(self, prompt: str) -> str | None:
        resp = self.client.beta.messages.create(
            model=AI_MODEL,
            max_tokens=8000,  # a ceiling, not a target: thinking and the answer share it
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": prompt}],
        )
        if resp.stop_reason == "refusal":
            return None
        return "".join(b.text for b in resp.content if b.type == "text").strip()


class ClaudeCLI:
    name = "Claude Code"

    def __init__(self):
        self.exe = shutil.which("claude")
        if not self.exe:
            raise FileNotFoundError("claude")

    def generate(self, prompt: str) -> str | None:
        r = subprocess.run([self.exe, "-p", "Follow the instructions in the input.", "--output-format", "text",
                            "--model", AI_MODEL, "--bare", "--no-session-persistence"],
                           input=prompt, capture_output=True, text=True, timeout=240, cwd=tempfile.gettempdir())
        if r.returncode:
            detail = (r.stderr or r.stdout).strip().splitlines()
            raise RuntimeError(detail[-1] if detail else f"claude exited with {r.returncode}")
        return r.stdout.strip()


def ai_backend():
    """The Claude API when ANTHROPIC_API_KEY is set, else the Claude Code CLI if installed."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return ClaudeAPI()
        except ImportError:
            raise SystemExit("ANTHROPIC_API_KEY is set but the SDK is missing: pip install anthropic")
    if shutil.which("claude"):
        return ClaudeCLI()
    raise SystemExit("Personalizing needs Claude. Either set ANTHROPIC_API_KEY (and pip install anthropic), "
                     "or install Claude Code (https://claude.com/claude-code) and run `claude login`.")


def ai_backend_name() -> str | None:
    """Which backend ai_backend() would pick, without creating it (the web page asks on every refresh)."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ClaudeAPI.name if importlib.util.find_spec("anthropic") else None
    return ClaudeCLI.name if shutil.which("claude") else None


def recipient_facts(contact, profile=None) -> list[str]:
    facts = [f"Name: {contact['first_name']} {contact['last_name']}",
             f"Headline: {(profile and profile['headline']) or contact['position'] or 'unknown'}",
             f"Company: {contact['company'] or 'unknown'}"]
    if profile:
        for label, key in (("Location", "location"), ("About (their own words)", "about"),
                           ("Experience", "experience"), ("Education", "education")):
            if profile[key]:
                facts.append(f"{label}:\n{profile[key]}")
    return facts


RECENT_OPENERS = 8  # how many earlier messages the rewrite sees, so it doesn't repeat their hooks
# Notes starting with this mean "don't send; the person is waiting on you in the conversation".
HELD = "held for you"


def opener(body: str) -> str:
    """The first real line of a message: what comes after the greeting."""
    lines = [ln.strip() for ln in body.split("\n") if ln.strip()]
    if len(lines) > 1 and len(lines[0].split()) <= 3:  # "Hi Jane," is the greeting, not the hook
        lines = lines[1:]
    hook = ""
    for sentence in re.split(r"(?<=[.!?:])\s+", lines[0] if lines else ""):
        hook = f"{hook} {sentence}".strip()
        if len(hook) > 90:  # the first sentence or two: the hook, not the product description
            break
    return hook[:220]


def recent_openers(db, campaign_id, exclude_id=None, n: int = RECENT_OPENERS) -> list[str]:
    """Distinct openers of the campaign's latest rewritten messages, newest first. The message
    being rewritten is left out, so a redo isn't steered away from its own best hook."""
    rows = db.execute("SELECT body FROM messages WHERE campaign_id=? AND ai_at IS NOT NULL AND id IS NOT ? "
                      "ORDER BY COALESCE(sent_at, ai_at) DESC, id DESC LIMIT ?", (campaign_id, exclude_id, n)).fetchall()
    seen, out = set(), []
    for o in (opener(r["body"]) for r in rows):
        if o and o not in seen:
            seen.add(o)
            out.append(o)
    return out


THREAD_MESSAGES = 8  # how much of an existing conversation the rewrite sees
THREAD_CHARS = 400   # per message


def thread_text(thread, contact) -> str:
    """An existing LinkedIn conversation as 'You: ...' / 'Jane: ...' lines, oldest first, with
    LinkedIn's date heading ("[Oct 6]", "[Today]") on its own line whenever the day changes."""
    lines, day = [], ""
    for m in (thread or [])[-THREAD_MESSAGES:]:
        text = " ".join(m.get("text", "").split())[:THREAD_CHARS]
        if not text:
            continue
        if m.get("day") and m["day"] != day:
            day = m["day"]
            lines.append(f"[{day.title()}]")
        lines.append(f"{contact['first_name'] if is_theirs(m, contact) else 'You'}: {text}")
    return "\n".join(lines)


def is_theirs(m, contact) -> bool:
    """Whether a message in a conversation was written by the contact (and not by you)."""
    who = (m.get("who") or "").strip().lower()
    first = (contact["first_name"] or "").strip().lower()
    full = f"{contact['first_name']} {contact['last_name']}".strip().lower()
    return bool(who) and (who == full or who.split()[0] == first)


def their_last_words(thread, contact, limit: int = 2000) -> str:
    """What the contact wrote since your last message: the full reply, not the inbox preview."""
    run = []
    for m in reversed(thread or []):
        if not is_theirs(m, contact):
            break
        run.append(m.get("text", "").strip())
    return "\n".join(t for t in reversed(run) if t)[:limit]


def personalization_prompt(base: str, contact, brief: str, profile=None, recent=(), thread: str = "",
                           max_chars: int = 0) -> str:
    """`max_chars` is set for the note on a connection request, which LinkedIn caps in characters."""
    earlier = ""
    if thread:
        today = dt.date.today()
        earlier += ("Messages already exchanged with this person on LinkedIn, oldest first (today is "
                    f"{today:%A, %B} {today.day}, {today.year}; dates in brackets say when they were sent):\n"
                    + thread + "\n\nThe new message continues that conversation, so its first line after the greeting "
                    "must pick up from it in a few words (what you last talked about, or what they last did), "
                    "even when the topic was different. Don't introduce yourself as a stranger or repeat what "
                    "they already know. If their last message is unanswered, acknowledge it in a few natural "
                    "words (\"thanks for the invite\"), but never answer for the sender: don't accept, decline "
                    "or confirm plans, meetings, times, places or offers, don't promise anything, and don't "
                    "talk about the conversation itself (no \"separate thread\", no \"I'll reply to that "
                    "separately\"). If this ask was already made, make the message a short, natural follow-up "
                    "instead.\n\nOne exception: if their last message is waiting on the sender personally (a "
                    "question, or a meeting or plan they proposed, from someone the sender knows), a pitch "
                    "would ignore them, so reply with exactly HOLD and nothing else. A sales pitch or an "
                    "event invite isn't waiting on the sender, and neither is anything from more than about "
                    "three weeks ago: old plans, invitations and events are over, so never treat them as "
                    "current.\n\n")
    if recent:
        earlier += ("Openers already used for other people in this campaign. Don't reuse their angle, "
                    "wording or sentence shape; this person's opening line should fit only them:\n- "
                    + "\n- ".join(recent) + "\n\n")
    return (
        ("You help someone write the note on a LinkedIn connection request to a person they are not "
         "connected to yet.\n\n" if max_chars else
         "You help someone send a short LinkedIn message to one of their existing connections.\n\n") +
        f"Campaign goal:\n{brief}\n\n"
        "Recipient, from their LinkedIn profile. This is everything known about them:\n"
        + "\n".join(recipient_facts(contact, profile)) + "\n\n"
        f"Draft message:\n{base}\n\n" + earlier +
        "Rewrite the draft so it reads as written for this person:\n"
        "- Keep the draft's structure, voice, core ask, links and sign-off.\n"
        + (f"- At most {max_chars} characters, spaces and line breaks included: LinkedIn's note box takes "
           f"no more. The draft is {len(base)}. Aim for {int(max_chars * 0.9)} or fewer; cut words to make "
           "room for the personal detail.\n" if max_chars else
           f"- Keep the length: the draft is {len(base.split())} words, and the result must not be longer. "
           "If you add a personal sentence, cut or shorten another one.\n") +
        "- Change only what makes it personal: open on something specific from their experience, "
        "about text or education, and tie the ask to it where that is natural. One or two concrete "
        "details beat a list.\n"
        "- Use only the facts above. Never invent shared history, mutual contacts or anything not "
        "stated. If the profile says little, change little.\n"
        "- Don't copy the headline or string job titles together; turn profile text into natural phrases.\n"
        "- No compliments that could apply to anyone, nothing that sounds automated. No flattery or "
        "jokes about the person; if a detail could read as odd or backhanded, leave it out.\n"
        "Reply with the message text only: no preamble, labels or quotes."
    )


# --- Checking a rewrite before it goes out ---------------------------------------
# A rewrite that drops a link, changes the sign-off or claims something the profile doesn't
# say is worse than the plain template. The cheap checks below run on every rewrite; when they
# pass, one more short Claude call looks for invented facts. Anything wrong gets one retry
# with the problems spelled out, then the template is sent instead.

MAX_GROWTH = 1.15  # a personalized message may be at most this much longer than the template
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+|\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|ai|co|app|dev|me|so|"
                    r"xyz|info|eu|uk|de|fr|es|it|nl|ca|us|au|in|ch|se|no|dk|fi|pt|be|at|pl|tn|ly|to|tv)"
                    r"(?:/[^\s<>\"')\]]*)?", re.I)
PREAMBLE_RE = re.compile(r"(?i)^(here('s| is)\b|sure\b|subject:|\"|\u201c|```)")


def rewrite_problems(base: str, text: str, contact=None, max_chars: int = 0) -> list[str]:
    """What a rewrite changed that it must not have. Empty list means it passed."""
    problems = []
    limit = int(len(base.split()) * MAX_GROWTH)
    if max_chars:
        if len(text) > max_chars:
            problems.append(f"it is {len(text)} characters; LinkedIn's note box takes {max_chars}")
    elif len(text.split()) > limit:
        problems.append(f"it is {len(text.split())} words; the limit is {limit}")
    for url in dict.fromkeys(u.rstrip(".,") for u in URL_RE.findall(base)):
        if url not in text:
            problems.append(f"the link {url} is missing or changed; it must appear exactly as in the draft")
    base_lines = [ln.strip() for ln in base.splitlines() if ln.strip()]
    text_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not text_lines:
        return ["it is empty"]
    if len(base_lines) > 1 and len(base_lines[-1].split()) <= 5 and base_lines[-1] != text_lines[-1]:
        problems.append(f"the sign-off must stay exactly {base_lines[-1]!r}")
    first = (contact["first_name"] or "").split(" ")[0] if contact is not None and contact["first_name"] else ""
    first = re.sub(r"[^\w'-]", "", first)  # names come with emoji and symbols; greet by the letters
    if first and first.lower() not in text_lines[0].lower():
        problems.append(f"the first line must greet {first}, as the draft does")
    if PLACEHOLDER.search(text) or re.search(r"\[[^\]\n]{1,40}\]", text):
        problems.append("it contains an unfilled placeholder")
    if PREAMBLE_RE.match(text):
        problems.append("it starts with a preamble, a subject line or quotes instead of the message itself")
    return problems


def grounding_prompt(text: str, contact, profile=None, thread: str = "") -> str:
    known = "\n".join(recipient_facts(contact, profile))
    if thread:
        known += f"\n\nMessages already exchanged with them (also known facts):\n{thread}"
    return ("A message is about to be sent to a LinkedIn connection. Below is everything the sender knows "
            "about the recipient, then the message.\n\nKnown facts:\n"
            + known + f"\n\nMessage:\n{text}\n\n"
            "List every statement the message makes about the recipient (their work, past, places, studies, "
            "achievements, opinions, or their relationship with the sender) that the known facts do not "
            "support. A fair paraphrase of a fact is supported. Ignore what the message says about the sender, "
            "their product and the ask, except commitments: also list any line where the message accepts, "
            "declines or confirms a plan, meeting, time, place or offer, or promises something on the "
            "sender's behalf. Reply with exactly NONE if there is nothing to list; otherwise one statement "
            "per line, nothing else.")


def unsupported_claims(ai, text: str, contact, profile=None, thread: str = "") -> list[str]:
    reply = (ai.generate(grounding_prompt(text, contact, profile, thread)) or "").strip()
    if not reply or reply.upper().startswith("NONE"):
        return []
    return [ln.strip("-*\u2022 ").strip() for ln in reply.splitlines() if ln.strip()][:5]


def check_rewrite(ai, base: str, text: str, contact, profile=None, thread: str = "", max_chars: int = 0) -> list[str]:
    problems = rewrite_problems(base, text, contact, max_chars)
    if not problems:  # the grounding check costs a call, so only when the free checks pass
        problems = [f"it says something the profile doesn't support, or commits you: {c}"
                    for c in unsupported_claims(ai, text, contact, profile, thread)]
    return problems


def personalize(ai, base: str, contact, brief: str, profile=None, recent=(), thread: str = "",
                max_chars: int = 0) -> tuple[str, str]:
    """Have Claude tailor the rendered draft and check the result. `recent` holds openers of
    messages already written in this campaign, so each new one finds its own angle; `thread`
    is the conversation already had with this person, so the message continues it.

    Returns (text, note): note is empty when the rewrite passed, otherwise why the
    template text is being used instead.
    """
    prompt = personalization_prompt(base, contact, brief, profile, recent, thread, max_chars)
    text = (ai.generate(prompt) or "").strip()
    if thread and text.upper().rstrip(".") == "HOLD":
        return base, f"{HELD}: they're waiting on your reply in your LinkedIn conversation"
    if not text:
        return base, "Claude declined; using the template text"
    problems = check_rewrite(ai, base, text, contact, profile, thread, max_chars)
    if not problems:
        return text, ""
    retry = (ai.generate(
        f"{prompt}\n\nYour previous version is below. Problems with it:\n- " + "\n- ".join(problems)
        + "\n\nFix every problem. Keep the single best personal detail and the ask; everything the draft "
        f"had that must not change stays exactly as it was.\n\n{text}") or "").strip()
    if retry:
        problems = check_rewrite(ai, base, retry, contact, profile, thread, max_chars)
        if not problems:
            return retry, ""
    if thread:  # the template ignores what you've already said to each other, so don't send it
        return base, f"{HELD}: no rewrite that fits your conversation passed the checks (" + "; ".join(problems) + ")"
    return base, "using the template text; the rewrite " + "; ".join(problems)


def draft_candidates(db, args):
    """Contacts matching the filters who aren't in this campaign and weren't messaged recently."""
    cutoff = (dt.datetime.now() - dt.timedelta(days=args.cooldown_days)).isoformat(timespec="seconds")
    extra = ("id NOT IN (SELECT contact_id FROM messages m JOIN campaigns c ON c.id=m.campaign_id "
             "WHERE c.name=?) AND id NOT IN (SELECT contact_id FROM messages WHERE sent_at >= ?)")
    return select_contacts(db, args, extra, (args.campaign, cutoff))


def sender_values(args) -> dict:
    sender = {"name": args.sender_name or os.environ.get("OUTREACH_SENDER_NAME", "")}
    for kv in args.var:
        k, sep, v = kv.partition("=")
        if not sep:
            raise SystemExit(f"--var expects key=value, got {kv!r}")
        sender[k.strip()] = v.strip()
    return sender


def cmd_draft(db, args):
    template = getattr(args, "template_text", None) or (
        Path(args.template).read_text(encoding="utf-8") if args.template else None)
    camp = db.execute("SELECT * FROM campaigns WHERE name=?", (args.campaign,)).fetchone()
    if camp is None:
        if template is None:
            raise SystemExit("New campaign: pass --template FILE.")
        if not args.dry_run:
            db.execute("INSERT INTO campaigns (name, template, brief, created_at) VALUES (?,?,?,?)",
                       (args.campaign, template, args.brief, now()))
            camp = db.execute("SELECT * FROM campaigns WHERE name=?", (args.campaign,)).fetchone()
    template = template or camp["template"]
    brief = args.brief or (camp["brief"] if camp else None) or ""

    contacts = prioritize(draft_candidates(db, args), getattr(args, "priority", None))
    if not contacts:
        print("No new contacts match these filters.")
        return

    client = None
    if args.ai:
        if not brief:
            raise SystemExit("--ai needs --brief describing the goal of the campaign.")
        client = ai_backend()

    sender = sender_values(args)
    shown = 0
    for i, c in enumerate(contacts, 1):
        try:
            body = render(template, c, sender)
        except KeyError as e:
            raise SystemExit(f"{e.args[0]} - pass it with --var name=value (sender_* placeholders) "
                             "or give it a fallback like {name|default}.")
        base = body
        if client is not None:
            try:
                profile = db.execute("SELECT * FROM profiles WHERE contact_id=?", (c["id"],)).fetchone()
                body, note = personalize(client, body, c, brief, profile, recent_openers(db, camp["id"] if camp else None),
                                         max_chars=NOTE_CHARS if c["prospect"] else 0)
                if note:
                    print(f"  ! #{c['id']}: {note}")
            except Exception as e:  # keep the template draft rather than abort the batch
                print(f"  ! AI personalization failed for #{c['id']} ({type(e).__name__}: {e}); using template.")
        if c["prospect"] and len(body) > NOTE_CHARS:
            print(f"  ! #{c['id']}: the note is {len(body)} characters; a connection request takes {NOTE_CHARS} "
                  f"({NOTE_CHARS_PREMIUM} with Premium).")
        elif len(body) > LINKEDIN_MAX_CHARS:
            print(f"  ! #{c['id']}: message is {len(body)} chars (LinkedIn limit {LINKEDIN_MAX_CHARS}).")
        if args.dry_run:
            if shown < 3:
                print(f"--- #{c['id']} {c['first_name']} {c['last_name']} ---\n{body}\n")
                shown += 1
            continue
        db.execute("INSERT OR IGNORE INTO messages (campaign_id, contact_id, body, base_body, ai_at, updated_at) "
                   "VALUES (?,?,?,?,?,?)", (camp["id"], c["id"], body, base, now() if client is not None else None, now()))
        if client is not None:
            print(f"  drafted {i}/{len(contacts)}", end="\r")
    if args.dry_run:
        print(f"(dry run) {len(contacts)} message(s) would be drafted; nothing saved.")
        return
    db.commit()
    print(f"Drafted {len(contacts)} message(s) in campaign '{args.campaign}'. "
          f"Review with: outreach.py review {args.campaign}")


# --------------------------------------------------------------------------
# Making messages personal: read each profile, then have Claude rewrite the draft
# --------------------------------------------------------------------------

def enrich_targets(db, args):
    """Contacts to read profiles for: a campaign's waiting messages, or the filters."""
    if getattr(args, "campaign", None):
        ids = [r["contact_id"] for r in campaign_messages(db, args.campaign, "draft")]
        if getattr(args, "ids", None):
            wanted = set(parse_ids(args.ids))
            ids = [i for i in ids if i in wanted]
        return [db.execute("SELECT * FROM contacts WHERE id=?", (i,)).fetchone() for i in ids]
    return select_contacts(db, args)


def cmd_enrich(db, args, page_factory=None, should_stop=lambda: False, sleep=time.sleep):
    import linkedin_browser as lb
    if page_factory is None:
        def page_factory():
            return lb.browser(args.profile_dir, headless=args.headless, channel=args.channel)
    cutoff = (dt.datetime.now() - dt.timedelta(days=args.max_age_days)).isoformat(timespec="seconds")
    targets = []
    for c in enrich_targets(db, args):
        have = db.execute("SELECT fetched_at FROM profiles WHERE contact_id=?", (c["id"],)).fetchone()
        if have and have[0] >= cutoff and not args.refresh:
            continue
        targets.append(c)
    if not targets:
        print("Every profile is already read (use --refresh to read them again).")
        return 0
    stop = stop_flag()
    print(f"Reading {len(targets)} profile(s) on LinkedIn (3 pages each, paced)...")
    done = 0
    with page_factory() as page:
        for i, c in enumerate(targets, 1):
            if stop.is_set() or should_stop():
                print("Stopped.")
                break
            who = f"{c['first_name']} {c['last_name']}"
            if not c["url"]:
                print(f"  x {who}: no profile URL")
                continue
            if not done:
                lb.ensure_logged_in(page, lb.feed_url_for(c["url"]), cancelled=lambda: stop.is_set() or should_stop())
            try:
                p = lb.read_profile(page, c["url"], sleep=sleep)
            except lb.StopRun as e:
                print(f"STOPPED: {e}")
                break
            except Exception as e:
                print(f"  x {who}: {type(e).__name__}: {e}")
                continue
            db.execute("INSERT OR REPLACE INTO profiles (contact_id, headline, location, about, experience, "
                       "education, fetched_at) VALUES (?,?,?,?,?,?,?)",
                       (c["id"], p["headline"], p["location"], p["about"], p["experience"], p["education"], now()))
            db.commit()
            done += 1
            print(f"  {i}/{len(targets)} {who}: {len(p['about']) + len(p['experience']) + len(p['education'])} "
                  "characters of profile text")
            if i < len(targets):
                sleep(random.uniform(args.min_delay, args.max_delay))
    print(f"Read {done} profile(s).")
    return done


def ensure_profile(db, m, page, sleep=time.sleep):
    """Read this person's profile into the database if it isn't there yet."""
    import linkedin_browser as lb
    if m["url"] and not db.execute("SELECT 1 FROM profiles WHERE contact_id=?", (m["contact_id"],)).fetchone():
        p = lb.read_profile(page, m["url"], sleep=sleep)
        db.execute("INSERT OR REPLACE INTO profiles (contact_id, headline, location, about, experience, "
                   "education, fetched_at) VALUES (?,?,?,?,?,?,?)",
                   (m["contact_id"], p["headline"], p["location"], p["about"], p["experience"], p["education"], now()))
        db.commit()


def personalize_message(db, ai, m, brief: str, page=None, sleep=time.sleep, thread=None,
                        max_chars: int | None = None) -> tuple[str, str]:
    """Rewrite one waiting message from the person's profile and save it. With a `page`, the
    profile is read first when it isn't in the database yet. The rewrite sees the openers of the
    campaign's latest messages so it doesn't repeat them, and `thread` (the conversation already
    open with this person, from LinkedInSender.open) so it continues rather than restarts it.
    Returns (body, note): the note says why the template text was kept, or is empty."""
    if page is not None:
        ensure_profile(db, m, page, sleep)
    profile = db.execute("SELECT * FROM profiles WHERE contact_id=?", (m["contact_id"],)).fetchone()
    history = thread_text(thread, m) if thread else ""
    if max_chars is None:  # a connection request's note: LinkedIn's box says the real limit when it's open
        max_chars = NOTE_CHARS if m["prospect"] else 0
    body, note = personalize(ai, m["base_body"] or m["body"], m, brief, profile,
                             recent_openers(db, m["campaign_id"], m["id"]), history, max_chars)
    db.execute("UPDATE messages SET body=?, ai_at=?, thread=COALESCE(?, thread), updated_at=? WHERE id=?",
               (body, now(), history or None, now(), m["id"]))
    db.commit()
    return body, note


def cmd_personalize(db, args, ai=None, page_factory=None, should_stop=lambda: False, sleep=time.sleep):
    camp = get_campaign(db, args.campaign)
    brief = (args.brief or camp["brief"] or "").strip()
    if not brief:
        raise SystemExit("Pass --brief 'what this campaign is for' (it's saved on the campaign for next time).")
    if brief != (camp["brief"] or ""):
        db.execute("UPDATE campaigns SET brief=? WHERE id=?", (brief, camp["id"]))
        db.commit()
    msgs = [m for m in campaign_messages(db, args.campaign, "draft") if args.redo or not m["ai_at"]]
    if args.max:
        msgs = msgs[:args.max]
    if not msgs:
        print("Nothing to personalize: every waiting message is done (use --redo to do them again).")
        return
    ai = ai or ai_backend()  # fail before touching the browser if Claude isn't reachable
    if args.enrich:
        args.ids = ",".join(str(m["contact_id"]) for m in msgs)  # only these people's profiles
        cmd_enrich(db, args, page_factory=page_factory, should_stop=should_stop, sleep=sleep)
    stop = stop_flag()
    print(f"Personalizing {len(msgs)} message(s) with {ai.name}...")
    done = 0
    for m in msgs:
        if stop.is_set() or should_stop():
            print("Stopped.")
            break
        who = f"{m['first_name']} {m['last_name']}"
        has_profile = db.execute("SELECT 1 FROM profiles WHERE contact_id=?", (m["contact_id"],)).fetchone()
        try:  # always starts from the template text, so a redo doesn't build on the last rewrite
            _, note = personalize_message(db, ai, m, brief)
        except Exception as e:  # keep the template draft rather than abort the batch
            print(f"  x {who}: {type(e).__name__}: {e}")
            if "authentication" in type(e).__name__.lower() or "401" in str(e):
                print("STOPPED: Claude rejected the API key. Check ANTHROPIC_API_KEY (an `export` in the "
                      "terminal takes precedence over the .env file).")
                break
            continue
        done += 1
        print(f"  {'!' if note else 'ok'} {who}" + (f": {note}" if note else "")
              + ("" if has_profile else " (profile not read yet: used name and headline only)"))
    print(f"Personalized {done} of {len(msgs)} message(s). Read them before sending: "
          f"outreach.py review {args.campaign}")


# --------------------------------------------------------------------------
# Removing drafts / campaigns
# --------------------------------------------------------------------------

def cmd_delete_drafts(db, args):
    """Remove a campaign's waiting (and skipped/failed) messages; sent and replied ones stay."""
    camp = get_campaign(db, args.campaign)
    n = db.execute("DELETE FROM messages WHERE campaign_id=? AND status IN ('draft','skipped','failed')",
                   (camp["id"],)).rowcount
    db.commit()
    print(f"Removed {n} unsent message(s) from '{args.campaign}'.")


def cmd_delete_campaign(db, args):
    camp = get_campaign(db, args.campaign)
    sent = db.execute("SELECT COUNT(*) FROM messages WHERE campaign_id=? AND status IN ('sent','replied')",
                      (camp["id"],)).fetchone()[0]
    if sent and not args.force:
        raise SystemExit(f"'{args.campaign}' has {sent} sent message(s); their record would be lost. "
                         "Use `delete-drafts` to keep them, or --force to delete everything.")
    db.execute("DELETE FROM messages WHERE campaign_id=?", (camp["id"],))
    db.execute("DELETE FROM campaigns WHERE id=?", (camp["id"],))
    db.commit()
    print(f"Deleted campaign '{args.campaign}'.")


# --------------------------------------------------------------------------
# Replies: read the inbox and mark who answered
# --------------------------------------------------------------------------

def parse_inbox_when(label: str, today: dt.date | None = None) -> dt.date | None:
    """LinkedIn's conversation-list timestamps: '4:46 PM' (today), 'Nov 3', 'Nov 3, 2025'."""
    today = today or dt.date.today()
    label = (label or "").strip()
    if re.match(r"^\d{1,2}:\d{2}\s*[AP]M$", label, re.I):
        return today
    for fmt in ("%b %d, %Y", "%b %d"):
        with contextlib.suppress(ValueError):
            d = dt.datetime.strptime(label, fmt).date()
            return d if fmt.endswith("%Y") else d.replace(year=today.year if d.replace(year=today.year) <= today
                                                               else today.year - 1)
    return None


FULL_REPLIES = 20  # conversations opened per scan to read a reply in full


def site(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def plain_name(name: str) -> str:
    """Lower-case name without the credentials LinkedIn appends ("Jane Doe, PhD" -> "jane doe")."""
    return name.split(",")[0].strip().lower()


def cmd_replies(db, args, page_factory=None, inbox=None):
    """Scan the inbox; anyone we messaged whose latest message in the thread is theirs,
    on or after the day we wrote, is marked replied."""
    import linkedin_browser as lb
    waiting = db.execute("SELECT m.*, c.first_name, c.last_name, c.url FROM messages m "
                         "JOIN contacts c ON c.id=m.contact_id WHERE m.status='sent'").fetchall()
    if not waiting:
        print("No sent messages waiting for a reply.")
        return 0
    def record(inbox, full_reply=lambda m: ""):
        by_name = {}
        for conv in inbox:
            by_name.setdefault(plain_name(conv["name"]), conv)
        found = 0
        for m in waiting:
            conv = by_name.get(plain_name(f"{m['first_name']} {m['last_name']}"))
            if not conv or conv["from_me"]:
                continue
            when = parse_inbox_when(conv["when"])
            if when and m["sent_at"] and when < dt.date.fromisoformat(m["sent_at"][:10]):
                continue  # an older conversation, before our message
            reply = full_reply(m) or conv["preview"][:500]
            db.execute("UPDATE messages SET status='replied', reply=?, updated_at=? WHERE id=?",
                       (reply, now(), m["id"]))
            db.commit()
            found += 1
            print(f"  replied: {m['first_name']} {m['last_name']}: {' '.join(reply.split())[:80]}")
        return found

    if inbox is not None:
        found = record(inbox)
    else:
        if page_factory is None:
            def page_factory():
                return lb.browser(args.profile_dir, headless=args.headless, channel=args.channel)
        with page_factory() as page:
            inbox_url = args.url or lb.MESSAGING_URL
            lb.ensure_logged_in(page, inbox_url)
            inbox = lb.read_inbox(page, inbox_url, limit=args.limit)
            sender, opened = lb.LinkedInSender(page), []

            def full_reply(m):
                # The inbox shows one cut-off line, so open the conversation for the whole reply.
                # Only on the inbox's own site (tests against a local mock never touch LinkedIn).
                if len(opened) >= FULL_REPLIES or not m["url"] or site(m["url"]) != site(inbox_url):
                    return ""
                opened.append(m["id"])
                try:
                    page.wait_for_timeout(random.uniform(2000, 5000) if len(opened) > 1 else 0)
                    return their_last_words(sender.open(m["url"], name=f"{m['first_name']} {m['last_name']}"), m)
                except lb.StopRun:
                    raise
                except Exception:
                    return ""
            found = record(inbox, full_reply)
    print(f"Checked {len(inbox)} conversation(s): {found} new repl{'y' if found == 1 else 'ies'}, "
          f"{len(waiting) - found} still waiting.")
    return found


# --------------------------------------------------------------------------
# Review / export / status
# --------------------------------------------------------------------------

_CAMPAIGN_MESSAGES = ("FROM messages m JOIN campaigns k ON k.id=m.campaign_id JOIN contacts c ON c.id=m.contact_id "
                      "WHERE k.name=? AND c.do_not_contact=0")


def campaign_messages(db, name, status=None, limit=None):
    sql = f"SELECT m.*, c.first_name, c.last_name, c.url, c.company, c.position, c.prospect {_CAMPAIGN_MESSAGES}"
    params = [name]
    if status:
        sql += " AND m.status=?"
        params.append(status)
    sql += " ORDER BY m.id"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return db.execute(sql, params).fetchall()


def count_messages(db, name, status=None) -> int:
    """How many messages campaign_messages would return, without loading them."""
    sql, params = f"SELECT COUNT(*) {_CAMPAIGN_MESSAGES}", [name]
    if status:
        sql += " AND m.status=?"
        params.append(status)
    return db.execute(sql, params).fetchone()[0]


def message_row(db, message_id):
    """One message as campaign_messages returns it, read fresh from the database."""
    return db.execute("SELECT m.*, c.first_name, c.last_name, c.url, c.company, c.position, c.prospect "
                      "FROM messages m JOIN contacts c ON c.id=m.contact_id WHERE m.id=?", (message_id,)).fetchone()


def cmd_review(db, args):
    get_campaign(db, args.campaign)
    rows = campaign_messages(db, args.campaign, args.status)
    for m in rows:
        print(f"=== contact #{m['contact_id']}  {m['first_name']} {m['last_name']}  "
              f"[{m['status']}]  {m['position']} @ {m['company']}\n{m['body']}\n")
    print(f"{len(rows)} message(s).")


def cmd_export(db, args):
    get_campaign(db, args.campaign)
    rows = campaign_messages(db, args.campaign, args.status)
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["contact_id", "first_name", "last_name", "url", "company", "position", "status", "message"])
        for m in rows:
            w.writerow([m["contact_id"], m["first_name"], m["last_name"], m["url"], m["company"],
                        m["position"], m["status"], m["body"]])
    print(f"Wrote {len(rows)} row(s) to {args.out}.")


def cmd_import_drafts(db, args):
    """Replace draft bodies from a CSV with contact_id + message columns (e.g. an edited export)."""
    camp = get_campaign(db, args.campaign)
    updated = skipped = 0
    with open(args.csv, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            body = (r.get("message") or "").strip()
            if not body or not (r.get("contact_id") or "").strip().isdigit():
                skipped += 1
                continue
            cur = db.execute("UPDATE messages SET body=?, updated_at=? WHERE campaign_id=? AND contact_id=? "
                             "AND status='draft'", (body, now(), camp["id"], int(r["contact_id"])))
            updated += cur.rowcount
            skipped += not cur.rowcount
    db.commit()
    print(f"Updated {updated} draft(s); skipped {skipped} row(s) (empty, unknown, or already sent).")


def cmd_status(db, args):
    camps = db.execute("SELECT * FROM campaigns ORDER BY id").fetchall()
    if not camps:
        print("No campaigns yet.")
    for k in camps:
        counts = dict(db.execute("SELECT status, COUNT(*) FROM messages WHERE campaign_id=? GROUP BY status",
                                 (k["id"],)).fetchall())
        sent = counts.get("sent", 0) + counts.get("replied", 0)
        rate = f"{100 * counts.get('replied', 0) / sent:.0f}%" if sent else "-"
        print(f"{k['name']:<24} " + "  ".join(f"{s}={counts.get(s, 0)}" for s in STATUSES)
              + f"  reply rate={rate}")
    print(f"\nSent in the last 24h (all campaigns): {sent_last_24h(db)}")


def cmd_mark(db, args):
    camp = get_campaign(db, args.campaign)
    ids = parse_ids(args.contact_ids)
    marked = 0
    for cid in ids:
        marked += db.execute(
            "UPDATE messages SET status=?, updated_at=?, sent_at=COALESCE(sent_at, CASE WHEN ? IN "
            "('sent','replied') THEN ? END), error=CASE WHEN ?='draft' THEN NULL ELSE error END "
            "WHERE campaign_id=? AND contact_id=?",
            (args.status, now(), args.status, now(), args.status, camp["id"], cid)).rowcount
    db.commit()
    print(f"Marked {marked} message(s) as {args.status}."
          + (f" {len(ids) - marked} of the ids have no message in '{args.campaign}' (they are contact ids, "
             "the number `review` shows after 'contact #')." if marked < len(ids) else ""))


# --------------------------------------------------------------------------
# Assisted sending
# --------------------------------------------------------------------------

NETWORK_ERRORS = ("ERR_INTERNET_DISCONNECTED", "ERR_NAME_NOT_RESOLVED", "ERR_CONNECTION_",
                  "ERR_NETWORK_CHANGED", "ERR_ADDRESS_UNREACHABLE", "ERR_PROXY_CONNECTION_FAILED")


def network_error(e: BaseException) -> bool:
    """True for the browser's no-network errors (net::ERR_INTERNET_DISCONNECTED and friends)."""
    text = str(e)
    return "net::" in text and any(code in text for code in NETWORK_ERRORS)


def sent_last_24h(db) -> int:
    cutoff = (dt.datetime.now() - dt.timedelta(hours=24)).isoformat(timespec="seconds")
    return db.execute("SELECT COUNT(*) FROM messages WHERE sent_at >= ?", (cutoff,)).fetchone()[0]


def invites_last_24h(db) -> int:
    """Connection requests sent in the last 24 hours, with or without a note."""
    cutoff = (dt.datetime.now() - dt.timedelta(hours=24)).isoformat(timespec="seconds")
    return db.execute("SELECT COUNT(*) FROM messages WHERE invited_at >= ?", (cutoff,)).fetchone()[0]


def copy_to_clipboard(text: str) -> bool:
    candidates = [["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"],
                  ["xsel", "--clipboard", "--input"], ["clip.exe"], ["clip"]]
    for cmd in candidates:
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text.encode("utf-8"), check=True, timeout=5)
                return True
            except (subprocess.SubprocessError, OSError):
                continue
    return False


def edit_text(text: str, prompt=input) -> str:
    editor = os.environ.get("EDITOR")
    if editor:
        with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(text)
            path = f.name
        subprocess.call([*editor.split(), path])
        new = Path(path).read_text(encoding="utf-8").strip()
        os.unlink(path)
        return new or text
    print("Type the new message; finish with a line containing only '.'")
    lines = []
    while (line := prompt("")) != ".":
        lines.append(line)
    return "\n".join(lines).strip() or text


def cmd_send(db, args, prompt=input, clipboard=copy_to_clipboard, opener=webbrowser.open):
    get_campaign(db, args.campaign)
    queue = campaign_messages(db, args.campaign, "draft")
    if not queue:
        print("Nothing left to send in this campaign.")
        return
    print(f"{len(queue)} draft(s) queued. Daily cap: {args.daily_cap} "
          f"(already sent in the last 24h: {sent_last_24h(db)}).\n"
          "For each one: the message is copied to your clipboard and the profile opens.\n"
          "In LinkedIn click Message, paste, send - then come back here.\n")
    for m in queue:
        if sent_last_24h(db) >= args.daily_cap:
            print(f"Daily cap of {args.daily_cap} reached - stopping. Run again tomorrow.")
            break
        body = m["body"]
        while True:
            print(f"--- {m['first_name']} {m['last_name']}  ({m['position']} @ {m['company']})\n"
                  f"{m['url']}\n\n{body}\n")
            copied = clipboard(body)
            if not copied:
                print("(clipboard unavailable - copy the text above manually)")
            if m["url"] and not args.no_open:
                opener(m["url"])
            choice = prompt("[Enter] sent  [s]kip  [e]dit  [x] never contact  [q]uit > ").strip().lower()
            if choice == "e":
                body = edit_text(body, prompt)
                db.execute("UPDATE messages SET body=?, updated_at=? WHERE id=?", (body, now(), m["id"]))
                db.commit()
                continue
            break
        if choice == "q":
            break
        if choice == "x":
            db.execute("UPDATE contacts SET do_not_contact=1 WHERE id=?", (m["contact_id"],))
            db.execute("UPDATE messages SET status='skipped', updated_at=? WHERE id=?", (now(), m["id"]))
        elif choice == "s":
            db.execute("UPDATE messages SET status='skipped', updated_at=? WHERE id=?", (now(), m["id"]))
        else:
            db.execute("UPDATE messages SET status='sent', sent_at=?, updated_at=? WHERE id=?",
                       (now(), now(), m["id"]))
        db.commit()
    remaining = count_messages(db, args.campaign, "draft")
    print(f"\nDone for now. {remaining} draft(s) remaining; sent in last 24h: {sent_last_24h(db)}.")


# --------------------------------------------------------------------------
# Fully automated sending
# --------------------------------------------------------------------------

def cmd_login(db, args):
    import linkedin_browser as lb
    stop = stop_flag()
    lb.interactive_login(args.profile_dir, channel=args.channel, cancelled=stop.is_set)


def accepted_invitations(page, pending, sleep=time.sleep, stopping=lambda: False) -> list:
    """Those of `pending` (messages to people invited without a note) who are now among your newest
    connections. One read of the top of your connections list, instead of opening every profile:
    it stops once everyone is found, or the list reaches people who connected before the first
    invitation went out."""
    import linkedin_browser as lb

    def slug(url):
        return unquote((url or "").rstrip("/").rsplit("/", 1)[-1]).lower()

    waiting = {slug(m["url"]): m for m in pending}
    first_invite = min((m["invited_at"] or "")[:10] for m in pending)
    seen, older = set(), []

    def look(people):
        for c in people:
            seen.add(slug(c["url"]))
            if (parse_date(c["connected_on"]) or first_invite) < first_invite:
                older.append(c)

    lb.collect_connections(page, url=lb.connections_url_for(pending[0]["url"]), max_contacts=ACCEPT_LOOKBACK,
                           sleep=sleep, on_batch=look, say=lambda *a: None,
                           should_stop=lambda: stopping() or bool(older) or waiting.keys() <= seen)
    return [m for s, m in waiting.items() if s in seen]


def cmd_autosend(db, args, sender_factory=None, sleep=time.sleep, prompt=input, cancelled=lambda: False, ai=None):
    import linkedin_browser as lb
    if args.min_delay > args.max_delay:
        raise SystemExit("--min-delay must be <= --max-delay")
    camp, brief = get_campaign(db, args.campaign), ""
    if getattr(args, "personalize", False):  # fail before touching the browser if Claude isn't set up
        brief = camp["brief"] or ""
        if not brief:
            raise SystemExit("--personalize needs the campaign's brief: set it with "
                             f"`personalize {args.campaign} --brief '...'` (or on the Review tab).")
        ai = ai or ai_backend()
    queue = campaign_messages(db, args.campaign, "draft")
    room = args.daily_cap - sent_last_24h(db)
    if not queue:
        print("Nothing left to send in this campaign.")
        return
    if room <= 0 and not args.dry_run:
        print(f"Daily cap of {args.daily_cap} already reached - try again later.")
        return
    # People you aren't connected to get a connection request with the message as its note.
    # Those count toward the daily cap and have a tighter one of their own.
    size = min(len(queue) if args.dry_run else max(room, 0), args.max or len(queue))
    invite_room = len(queue) if args.dry_run else args.invite_cap - invites_last_24h(db)
    pending = [m for m in queue if m["prospect"] == 2]  # invited without a note; their message waits
    batch, capped = [], 0
    for m in queue:
        if len(batch) >= size:
            break
        if m["prospect"] == 2:
            continue
        if m["prospect"] == 1:
            if invite_room <= 0:
                capped += 1
                continue
            invite_room -= 1
        batch.append(m)
    if capped:
        print(f"{capped} connection request(s) wait for later: the cap of {args.invite_cap} per 24h is reached.")
    if not batch and not pending:
        return
    avg = (args.min_delay + args.max_delay) / 2
    invites = sum(1 for m in batch if m["prospect"] == 1)
    print(f"{'DRY RUN - nothing will be sent. ' if args.dry_run else ''}"
          f"{len(batch)} of {len(queue)} draft(s) in '{args.campaign}' this run"
          f"{f', {invites} as connection requests' if invites else ''} "
          f"(cap {args.daily_cap}/24h, {sent_last_24h(db)} already sent). "
          f"Pacing {args.min_delay:.0f}-{args.max_delay:.0f}s between messages, "
          f"about {len(batch) * avg / 60:.0f} min total.")
    if pending:
        print(f"{len(pending)} invitation(s) await an answer: your newest connections show who accepted, "
              "and those people get their message.")
    if not args.dry_run and not args.yes:
        if prompt("Type 'send' to start sending from your LinkedIn account: ").strip().lower() != "send":
            print("Aborted.")
            return

    if sender_factory is None:
        def sender_factory():
            return lb.browser(args.profile_dir, headless=args.headless, channel=args.channel)

    sent = held = invited = consecutive = 0
    failed_ids, retried = [], set()
    shots = Path(args.screenshots)
    stop = stop_flag()  # webapp.py's Stop button sends SIGTERM; a stop lands between messages

    def stopping():
        return stop.is_set() or cancelled()

    def pace(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end and not stopping():
            sleep(min(1, max(0, end - time.monotonic())))

    with sender_factory() as page:
        sender = lb.LinkedInSender(page)
        first_url = next((m["url"] for m in batch + pending if m["url"]), None)
        if first_url:  # logged out? wait for you to log in before touching the queue
            lb.ensure_logged_in(page, lb.feed_url_for(first_url), cancelled=stopping)
        accepted = []
        if pending:
            try:
                accepted = accepted_invitations(page, pending, sleep, stopping)
            except lb.StopRun as e:
                print(f"STOPPED: {e}")
                batch = []
            except Exception as e:
                print(f"  ! couldn't check who accepted ({type(e).__name__}: {str(e).splitlines()[0]})")
            for m in accepted:
                print(f"  {m['first_name']} {m['last_name']} accepted your invitation")
                if not args.dry_run:
                    db.execute("UPDATE contacts SET prospect=0 WHERE id=?", (m["contact_id"],))
            db.commit()
        # grows: a send that fails before anything went out gets one more try at the end
        todo = (accepted + batch)[:max(size, 0)]
        for i, m in enumerate(todo):
            if i:
                delay = random.uniform(args.min_delay, args.max_delay)
                print(f"  waiting {delay:.0f}s...")
                pace(delay)
            if stopping():
                print("Stopped.")
                break
            if m["id"] in retried:
                m = message_row(db, m["id"])  # it may have been personalized on the first try
            who = f"{m['first_name']} {m['last_name']}"
            body = m["body"]
            invite = m["prospect"] == 1
            blank = invite and getattr(args, "without_note", False)  # the message waits until they accept
            rewrite = ai is not None and not m["ai_at"] and not blank
            if rewrite and getattr(args, "read_profiles", False):
                try:  # their profile first: reading it leaves the page, so before the conversation opens
                    ensure_profile(db, m, page, sleep)
                except lb.StopRun:
                    raise
                except Exception as e:
                    print(f"  ! {who}: couldn't read the profile ({type(e).__name__}: {e})")
            try:
                thread, note_chars = None, 0
                if invite:
                    info = sender.open_invite(m["url"], note=not blank)
                    note_chars = info["limit"]
                    if info["left"] is not None:
                        print(f"  LinkedIn says {info['left']} invitation note(s) remain this month")
                else:
                    thread = sender.open(m["url"], name=who)
                if thread:
                    print(f"  {len(thread)} earlier message(s) in the conversation with {who}")
                    if not rewrite and not args.dry_run:
                        db.execute("UPDATE messages SET thread=? WHERE id=?", (thread_text(thread, m), m["id"]))
                        db.commit()
                if rewrite:
                    try:  # rewrite the draft now, knowing their profile and what was already said
                        body, note = personalize_message(db, ai, m, brief, thread=thread, max_chars=note_chars)
                        if note.startswith(HELD):
                            print(f"  - {who}: {note}; not sent, marked skipped")
                            held += 1
                            if not args.dry_run:
                                db.execute("UPDATE messages SET status='skipped', error=?, updated_at=? "
                                           "WHERE id=?", (note, now(), m["id"]))
                                db.commit()
                            continue
                        print(f"  ! {who}: {note}" if note else f"  personalized {who}")
                    except Exception as e:
                        print(f"  ! {who}: couldn't personalize ({type(e).__name__}: {e}); sending the template text")
                if blank:
                    sender.send_blank(dry_run=args.dry_run)
                elif invite:
                    sender.send_note(body, dry_run=args.dry_run)
                else:
                    sender.send_text(body, dry_run=args.dry_run)
            except lb.AlreadyConnected:
                print(f"  - {who}: already connected to you, so no request sent; message them in a regular campaign")
                if not args.dry_run:
                    db.execute("UPDATE contacts SET prospect=0 WHERE id=?", (m["contact_id"],))
                    db.execute("UPDATE messages SET status='skipped', error='already connected', updated_at=? "
                               "WHERE id=?", (now(), m["id"]))
                    db.commit()
                continue
            except lb.StopRun as e:
                print(f"STOPPED: {e}")
                sender.screenshot(str(shots / f"stopped-{m['contact_id']}.png"))
                break
            except Exception as e:  # one bad profile shouldn't kill the run
                if network_error(e):
                    print(f"  x {who}: {str(e).splitlines()[0]}")
                    print("STOPPED: no internet connection. Nothing was sent to them and their message stays "
                          "a draft; reconnect and run again.")
                    break
                consecutive += 1
                msg = f"{type(e).__name__}: {e}"
                sender.screenshot(str(shots / f"failed-{m['contact_id']}.png"))
                # A slow or half-loaded page fails before anything is sent, so it's safe to try once
                # more later. Not when the person can't be messaged, or Send was already pressed.
                if getattr(e, "retry", True) and m["id"] not in retried:
                    retried.add(m["id"])
                    todo.append(m)
                    print(f"  ~ {who}: {msg.splitlines()[0]}; will try once more at the end of the run")
                else:
                    failed_ids.append(m["contact_id"])
                    print(f"  x {who}: {msg}")
                    if not args.dry_run:
                        db.execute("UPDATE messages SET status='failed', error=?, updated_at=? WHERE id=?",
                                   (msg, now(), m["id"]))
                        db.commit()
                if consecutive >= 3:
                    print("STOPPED: 3 failures in a row - LinkedIn's page layout may have changed. "
                          f"See screenshots in {shots}/.")
                    break
                continue
            consecutive = 0
            if blank:
                invited += 1
                if not args.dry_run:
                    db.execute("UPDATE contacts SET prospect=2 WHERE id=?", (m["contact_id"],))
                    db.execute("UPDATE messages SET invited_at=?, error=?, updated_at=? WHERE id=?",
                               (now(), f"invited without a note on {now()[:10]}; this message goes out once they accept",
                                now(), m["id"]))
                    db.commit()
                print(f"  {'ok' if args.dry_run else 'invited'} {who} without a note"
                      + (" (dry run - nothing sent)" if args.dry_run else "; their message goes out once they accept"))
                continue
            sent += 1
            if args.dry_run:
                print(f"  ok {who} (dry run - typed, not sent)")
            else:
                db.execute("UPDATE messages SET status='sent', kind=?, invited_at=COALESCE(invited_at, ?), sent_at=?, "
                           "error=NULL, updated_at=? WHERE id=?",
                           ("invite" if invite else None, now() if invite else None, now(), now(), m["id"]))
                db.commit()
                print(f"  {'invited' if invite else 'sent'} {who}")
    verb = "checked" if args.dry_run else "sent"
    blank = f", {invited} {'checked' if args.dry_run else 'invited'} without a note" if invited else ""
    print(f"\nDone: {sent} {verb}, {len(failed_ids)} failed{f', {held} held for you' if held else ''}{blank}. "
          f"{count_messages(db, args.campaign, 'draft')} draft(s) remaining.")
    if failed_ids and not args.dry_run:
        print(f"Failed messages are marked 'failed' (see `review {args.campaign} --status failed`); "
              f"requeue with `mark {args.campaign} {','.join(map(str, failed_ids))} draft`.")
    if held and not args.dry_run:
        print(f"Held messages are marked 'skipped' (see `review {args.campaign} --status skipped`): "
              "answer those people yourself first.")


# --------------------------------------------------------------------------
# Setup check and plain-language errors
# --------------------------------------------------------------------------

SETUP_HINT = "Run `python3 start.py` (it installs everything), or: pip install -r requirements.txt && playwright install chromium"


def browser_problem(channel: str | None = None) -> str | None:
    """Why automated browsing can't start on this computer, or None when it can."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return f"Playwright isn't installed. {SETUP_HINT}"
    if channel or os.environ.get("OUTREACH_BROWSER_PATH"):
        return None  # your own Chrome/Edge is used, so Playwright's Chromium isn't needed
    try:
        with sync_playwright() as pw:
            if not Path(pw.chromium.executable_path).exists():
                return ("The automation browser isn't downloaded yet. Run `python3 start.py`, "
                        "or: python -m playwright install chromium")
    except Exception as e:
        return f"Playwright can't start ({(str(e).strip().splitlines() or [type(e).__name__])[0]}). {SETUP_HINT}"
    return None


def friendly_error(e: BaseException) -> str | None:
    """A sentence for the mistakes and hiccups a person can fix; None for a real bug."""
    text = str(e)
    if isinstance(e, FileNotFoundError) and e.filename:
        return f"File not found: {e.filename}"
    if isinstance(e, PermissionError) and e.filename:
        return f"Can't open {e.filename}: permission denied."
    if isinstance(e, sqlite3.OperationalError) and "locked" in text:
        return ("The database is busy: another run is writing to it. Wait for that run to finish "
                "(or stop it) and try again.")
    if isinstance(e, sqlite3.DatabaseError) and "not a database" in text:
        return "That file isn't an AutoLink Messenger database. Check --db / OUTREACH_DB."
    if "Executable doesn't exist" in text:
        return ("The automation browser isn't downloaded yet. Run `python3 start.py`, "
                "or: python -m playwright install chromium")
    if "ProcessSingleton" in text or "SingletonLock" in text or "user data directory is already in use" in text:
        return ("The automation browser is already open: another run is using it. Wait for that run to "
                "finish, or close its browser window, then try again.")
    if "Missing X server" in text or "$DISPLAY" in text:
        return "This computer has no display for the browser window. Add --headless."
    if network_error(e):
        return "No internet connection (the browser couldn't reach LinkedIn). Reconnect and run it again."
    return None


def cmd_check(db, args):
    """Say what is set up and what isn't, in the order a new user needs it."""
    import linkedin_browser as lb
    failed = False

    def line(ok, text):
        print(f"  {'ok' if ok else ('--' if ok is None else 'x ')}  {text}")

    print("AutoLink Messenger setup\n")
    line(True, f"Python {sys.version.split()[0]}")
    problem = browser_problem(getattr(args, "channel", None))
    failed |= bool(problem)
    line(not problem, problem or "Playwright and its browser are installed")
    profile = Path(args.profile_dir or lb.DEFAULT_PROFILE_DIR)
    line(True if profile.exists() else None,
         f"A LinkedIn login was saved before, in {profile}" if profile.exists() else
         "Not logged in to LinkedIn yet: the first import or send opens a window where you log in")
    ai = ai_backend_name()
    line(True if ai else None,
         f"Claude is reachable through {ai} (model {AI_MODEL})" if ai else
         "Claude isn't set up. It is optional: only personalizing messages and sorting people into groups "
         "need it. Put ANTHROPIC_API_KEY in a .env file (see .env.example), or install Claude Code")
    contacts = db.execute("SELECT COUNT(*) FROM contacts WHERE prospect=0").fetchone()[0]
    camps = db.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0]
    line(True, f"Database: {Path(args.db).resolve()} ({contacts} connection(s), {camps} campaign(s), "
               f"{sent_last_24h(db)} sent in the last 24h)")
    print("\n  ok = ready   -- = optional or not done yet   x = needs fixing")
    if failed:
        raise SystemExit(1)
    if not contacts:
        print("\nNext: import your connections (`import-linkedin`, or the Contacts tab of the web page).")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=DEFAULT_DB, help=f"SQLite file (default {DEFAULT_DB})")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("import", help="import LinkedIn's Connections.csv")
    s.add_argument("csv")
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("add-prospects", help="add people you aren't connected to yet, by profile link, "
                                              "to send them a connection request with a note")
    s.add_argument("urls", nargs="*", help="LinkedIn profile links")
    s.add_argument("--file", help="a file with one link per line, optionally followed by: name, company, title")
    s.add_argument("--profile-dir", default=None, help="browser profile holding your LinkedIn login")
    s.add_argument("--channel", choices=["chrome", "msedge"])
    s.add_argument("--headless", action="store_true", help="hide the browser window")
    s.set_defaults(func=cmd_add_prospects)

    s = sub.add_parser("list", help="list contacts matching filters")
    add_filter_args(s)
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("draft", help="create/extend a campaign and draft messages")
    s.add_argument("campaign")
    s.add_argument("--template", help="template file (required for a new campaign)")
    s.add_argument("--brief", help="one-paragraph goal of the campaign (used by --ai)")
    s.add_argument("--sender-name", help="fills {sender_name}")
    s.add_argument("--var", action="append", default=[], metavar="KEY=VALUE",
                   help="fills {sender_KEY}, e.g. --var product=Acme (repeatable)")
    s.add_argument("--ai", action="store_true", help="personalize each draft with Claude (needs --brief)")
    s.add_argument("--cooldown-days", type=int, default=30,
                   help="skip anyone messaged in any campaign within N days (default 30)")
    s.add_argument("--priority", metavar="TAG,TAG,...",
                   help="queue people with these tags first, in this order (see `tag`)")
    s.add_argument("--dry-run", action="store_true", help="preview 3 drafts, save nothing")
    add_filter_args(s)
    s.set_defaults(func=cmd_draft)

    s = sub.add_parser("review", help="print a campaign's messages")
    s.add_argument("campaign")
    s.add_argument("--status", choices=STATUSES)
    s.set_defaults(func=cmd_review)

    s = sub.add_parser("export", help="export a campaign to CSV")
    s.add_argument("campaign")
    s.add_argument("out")
    s.add_argument("--status", choices=STATUSES)
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("import-drafts", help="load edited/personalized drafts back from CSV")
    s.add_argument("campaign")
    s.add_argument("csv", help="CSV with contact_id and message columns (same shape as export)")
    s.set_defaults(func=cmd_import_drafts)

    s = sub.add_parser("send", help="assisted send loop (you press Send in LinkedIn)")
    s.add_argument("campaign")
    s.add_argument("--daily-cap", type=int, default=DEFAULT_DAILY_CAP,
                   help=f"max messages per rolling 24h across campaigns (default {DEFAULT_DAILY_CAP})")
    s.add_argument("--no-open", action="store_true", help="don't open profiles in the browser")
    s.set_defaults(func=cmd_send)

    def browser_args(sp):
        sp.add_argument("--profile-dir", default=None,
                        help="browser profile holding your LinkedIn login (default ~/.outreach-linkedin-profile)")
        sp.add_argument("--channel", choices=["chrome", "msedge"],
                        help="use your installed Chrome/Edge instead of Playwright's Chromium")

    s = sub.add_parser("login", help="log in to LinkedIn in the automation browser (import-linkedin and "
                                     "autosend also ask when needed)")
    browser_args(s)
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("import-linkedin", help="import connections by reading your LinkedIn connections page "
                                                "(after `login`; the CSV export has more detail)")
    browser_args(s)
    s.add_argument("--max", type=int, help="stop after this many connections")
    s.add_argument("--headless", action="store_true", help="hide the browser window")
    s.add_argument("--url", default=None, help=argparse.SUPPRESS)  # for tests
    s.set_defaults(func=cmd_import_linkedin)

    def pacing_args(sp, lo, hi):
        sp.add_argument("--min-delay", type=float, default=lo, help=f"min seconds between people (default {lo:.0f})")
        sp.add_argument("--max-delay", type=float, default=hi, help=f"max seconds between people (default {hi:.0f})")

    s = sub.add_parser("tag", help="have Claude sort contacts into groups you define, from their headlines")
    s.add_argument("--tags", required=True, metavar="SPEC",
                   help="one per line as 'name: what it means', or a comma-separated list of names")
    s.add_argument("--redo", action="store_true", help="sort people who already have tags again")
    s.add_argument("--max", type=int, help="only the first N people")
    add_filter_args(s)
    s.set_defaults(func=cmd_tag)

    s = sub.add_parser("enrich", help="read About/Experience/Education from LinkedIn profiles (for personalize)")
    s.add_argument("--campaign", help="the people with waiting messages in this campaign (else: the filters)")
    add_filter_args(s)
    s.add_argument("--max-age-days", type=int, default=30, help="skip profiles read within N days (default 30)")
    s.add_argument("--refresh", action="store_true", help="read profiles again even if recent")
    browser_args(s)
    pacing_args(s, 3, 6)
    s.add_argument("--headless", action="store_true", help="hide the browser window")
    s.set_defaults(func=cmd_enrich)

    s = sub.add_parser("personalize", help="have Claude rewrite a campaign's waiting drafts for each person")
    s.add_argument("campaign")
    s.add_argument("--brief", help="what this campaign is for (saved on the campaign)")
    s.add_argument("--enrich", action="store_true", help="read profiles first (opens the browser)")
    s.add_argument("--redo", action="store_true", help="also redo messages already personalized")
    s.add_argument("--max", type=int, help="only the next N waiting messages")
    s.add_argument("--max-age-days", type=int, default=30, help=argparse.SUPPRESS)
    s.add_argument("--refresh", action="store_true", help=argparse.SUPPRESS)
    browser_args(s)
    pacing_args(s, 3, 6)
    s.add_argument("--headless", action="store_true", help="hide the browser window")
    s.set_defaults(func=cmd_personalize)

    s = sub.add_parser("autosend", help="FULLY AUTOMATED: send drafts from your browser session")
    s.add_argument("campaign")
    browser_args(s)
    s.add_argument("--daily-cap", type=int, default=DEFAULT_DAILY_CAP,
                   help=f"max messages per rolling 24h across campaigns (default {DEFAULT_DAILY_CAP})")
    s.add_argument("--invite-cap", type=int, default=DEFAULT_INVITE_CAP,
                   help=f"max connection requests per rolling 24h (default {DEFAULT_INVITE_CAP})")
    s.add_argument("--without-note", action="store_true",
                   help="send connection requests without a note (for when LinkedIn's monthly notes are used "
                        "up); each message stays queued and goes out as a normal message once they accept")
    s.add_argument("--max", type=int, help="max messages this run")
    s.add_argument("--min-delay", type=float, default=60, help="min seconds between messages (default 60)")
    s.add_argument("--max-delay", type=float, default=180, help="max seconds between messages (default 180)")
    s.add_argument("--dry-run", action="store_true",
                   help="open each profile and type the message, but don't press Send")
    s.add_argument("--personalize", action="store_true",
                   help="have Claude rewrite each message right before sending, from what is stored about the "
                        "person (headline, company, title) and the conversation you already have with them")
    s.add_argument("--read-profiles", action="store_true",
                   help="with --personalize: also open each person's About, Experience and Education first. More "
                        "to write from, but three more page loads a person, and LinkedIn limits profile views")
    s.add_argument("--no-profile-read", action="store_true", help=argparse.SUPPRESS)  # now the default; older commands still run
    s.add_argument("--headless", action="store_true", help="hide the browser window")
    s.add_argument("--yes", action="store_true", help="skip the confirmation prompt (for scheduled runs)")
    s.add_argument("--screenshots", default=str(HERE / "failures"),
                   help="where to save failure screenshots (default: failures/ next to this file)")
    s.set_defaults(func=cmd_autosend)

    s = sub.add_parser("replies", help="read your inbox and mark who replied")
    browser_args(s)
    s.add_argument("--limit", type=int, default=150, help="how many recent conversations to scan (default 150)")
    s.add_argument("--headless", action="store_true", help="hide the browser window")
    s.add_argument("--url", default=None, help=argparse.SUPPRESS)  # for tests
    s.set_defaults(func=cmd_replies)

    s = sub.add_parser("delete-drafts", help="remove a campaign's unsent messages (sent ones stay)")
    s.add_argument("campaign")
    s.set_defaults(func=cmd_delete_drafts)

    s = sub.add_parser("delete-campaign", help="delete a campaign and all its messages")
    s.add_argument("campaign")
    s.add_argument("--force", action="store_true", help="even if it has sent messages")
    s.set_defaults(func=cmd_delete_campaign)

    s = sub.add_parser("mark", help="set message status, e.g. when someone replies")
    s.add_argument("campaign")
    s.add_argument("contact_ids", help="comma-separated contact ids")
    s.add_argument("status", choices=STATUSES)
    s.set_defaults(func=cmd_mark)

    s = sub.add_parser("dnc", help="mark contacts do-not-contact (or --undo)")
    s.add_argument("ids", help="comma-separated contact ids")
    s.add_argument("--undo", action="store_true")
    s.set_defaults(func=cmd_dnc)

    s = sub.add_parser("status", help="per-campaign counts and reply rate")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("check", help="check the setup: Playwright, LinkedIn login, Claude, database")
    browser_args(s)
    s.set_defaults(func=cmd_check)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if getattr(args, "profile_dir", "unset") is None:
        import linkedin_browser as lb
        args.profile_dir = lb.DEFAULT_PROFILE_DIR
    try:
        db = connect(args.db)
        try:
            args.func(db, args)
        finally:
            db.close()
    except KeyboardInterrupt:  # Ctrl-C: everything is saved as it happens, so there is nothing to undo
        print("\nStopped. What was done so far is saved.")
        raise SystemExit(130)
    except Exception as e:
        message = friendly_error(e)
        if message is None:
            raise
        raise SystemExit(message)


if __name__ == "__main__":
    main()
