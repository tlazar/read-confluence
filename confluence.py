#!/usr/bin/env python3
"""
Confluence inventory tool for client-hosted (Data Center / Server) Confluence.

Configuration comes from a .env file in the current or script directory
(see .env.example). Command-line flags override it.

Commands, smallest first:

    check              verify the URL + token work, show who you are
    spaces             list spaces you can see
    space KEY          quick summary of one space (a few API calls)
    pages KEY          list the pages in a space
    inventory KEY      full inventory: report.md + CSVs + JSON

Run `confluence.py <command> --help` for per-command options.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlparse

try:
    import requests
    from requests.auth import HTTPBasicAuth
except ImportError:
    sys.exit("This script needs 'requests':  python3 -m pip install requests")


NOW = datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# .env loading
# --------------------------------------------------------------------------

ENV_KEYS = ("CONFLUENCE_BASE_URL", "CONFLUENCE_TOKEN", "CONFLUENCE_EMAIL",
            "CONFLUENCE_VERIFY", "CONFLUENCE_SPACE")


def find_env_file(explicit=None):
    if explicit:
        return explicit if os.path.exists(explicit) else None
    for candidate in (os.path.join(os.getcwd(), ".env"),
                      os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
        if os.path.isfile(candidate):
            return candidate
    return None


def load_env(path):
    """Minimal .env parser: KEY=VALUE, optional `export`, # comments, quotes.

    Values already present in the real environment win, so you can override a
    .env entry for one run with `CONFLUENCE_SPACE=OPS ./confluence.py ...`.
    """
    if not path:
        return {}
    loaded = {}
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            else:
                value = value.split(" #")[0].strip()
            loaded[key] = value
            os.environ.setdefault(key, value)
    return loaded


def as_bool(value, default=True):
    if value is None or value == "":
        return default
    return value.strip().lower() not in ("0", "false", "no", "off")


# --------------------------------------------------------------------------
# API client
# --------------------------------------------------------------------------

class ConfluenceError(RuntimeError):
    pass


class NotFound(ConfluenceError):
    pass


class Confluence:
    def __init__(self, base_url, token, email=None, verify=True, timeout=60):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.link_base = self.base
        self.s = requests.Session()
        self.s.verify = verify
        self.s.headers["Accept"] = "application/json"
        if email:
            self.s.auth = HTTPBasicAuth(email, token)
            self.auth_mode = f"HTTP Basic as {email} (Cloud-style API token)"
        else:
            self.s.headers["Authorization"] = f"Bearer {token}"
            self.auth_mode = "Bearer token (Data Center / Server PAT)"

    def get(self, path, params=None):
        url = path if path.startswith("http") else self.base + path
        last = None
        for attempt in range(6):
            try:
                r = self.s.get(url, params=params, timeout=self.timeout)
            except requests.exceptions.SSLError as exc:
                raise ConfluenceError(
                    f"TLS verification failed for {url}\n"
                    f"  {exc}\n"
                    "  For a private CA, point CONFLUENCE_VERIFY at the CA bundle path;\n"
                    "  to skip verification entirely set CONFLUENCE_VERIFY=false."
                )
            except requests.RequestException as exc:
                last = exc
                time.sleep(min(2 ** attempt, 30))
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                wait = r.headers.get("Retry-After")
                time.sleep(min(int(wait) if wait and wait.isdigit() else 2 ** attempt, 60))
                last = ConfluenceError(f"{r.status_code} from {r.url}")
                continue
            if r.status_code in (401, 403):
                raise ConfluenceError(
                    f"{r.status_code} {r.reason} for {r.url}\n"
                    "  Check CONFLUENCE_TOKEN, and how it is being sent:\n"
                    "    Data Center/Server PAT -> leave CONFLUENCE_EMAIL unset (Bearer)\n"
                    "    Cloud API token        -> set CONFLUENCE_EMAIL (Basic auth)"
                )
            if r.status_code == 404:
                raise NotFound(
                    f"404 Not Found for {r.url}\n"
                    "  Wrong base URL, missing context path (e.g. /confluence), or bad key."
                )
            r.raise_for_status()
            if "json" not in r.headers.get("Content-Type", ""):
                raise ConfluenceError(
                    f"Expected JSON from {r.url} but got {r.headers.get('Content-Type')}.\n"
                    "  This usually means the base URL points at a login page or proxy."
                )
            data = r.json()
            base_link = (data.get("_links") or {}).get("base")
            if base_link:
                self.link_base = base_link.rstrip("/")
            return data
        raise ConfluenceError(f"Giving up on {url}: {last}")

    def paginate(self, path, params, page_size=100, cap=None, progress=None):
        """Yield results across pages, handling both start= and cursor= styles."""
        params = dict(params or {})
        params["limit"] = page_size
        seen = 0
        while True:
            data = self.get(path, params)
            results = data.get("results", [])
            for item in results:
                yield item
                seen += 1
                if cap and seen >= cap:
                    return
            if progress and results and sys.stderr.isatty():
                print(f"\r  {progress}: {seen:,}", end="", file=sys.stderr, flush=True)
            nxt = (data.get("_links") or {}).get("next")
            if not nxt or not results:
                if progress and seen and sys.stderr.isatty():
                    print(file=sys.stderr)
                return
            # Re-issue against the same path using the next page's query params;
            # covers both cursor pagination and start/limit pagination.
            params.update(dict(parse_qsl(urlparse(nxt).query)))

    def count_cql(self, cql):
        """Cheap count via search totalSize, falling back to counting results."""
        data = self.get("/rest/api/content/search", {"cql": cql, "limit": 1})
        total = data.get("totalSize")
        if isinstance(total, int):
            return total
        return sum(1 for _ in self.paginate("/rest/api/content/search", {"cql": cql}))


def connect(args):
    base = args.base_url or os.environ.get("CONFLUENCE_BASE_URL")
    token = args.token or os.environ.get("CONFLUENCE_TOKEN")
    email = args.email or os.environ.get("CONFLUENCE_EMAIL") or None
    if not base:
        raise ConfluenceError("No base URL. Set CONFLUENCE_BASE_URL in .env or pass --base-url.")
    if not token:
        raise ConfluenceError("No token. Set CONFLUENCE_TOKEN in .env or pass --token.")

    verify_raw = os.environ.get("CONFLUENCE_VERIFY", "")
    if args.insecure or verify_raw.strip().lower() in ("0", "false", "no", "off"):
        verify = False
    elif verify_raw and os.path.exists(verify_raw):
        verify = verify_raw          # path to a CA bundle
    else:
        verify = True
    if verify is False:
        requests.packages.urllib3.disable_warnings()

    return Confluence(base, token, email, verify=verify)


def resolve_space(args):
    key = getattr(args, "space", None) or os.environ.get("CONFLUENCE_SPACE")
    if not key:
        raise ConfluenceError("No space key. Pass it as an argument or set CONFLUENCE_SPACE in .env.")
    return key


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

TAG_RE = re.compile(r"<[^>]+>")
MACRO_RE = re.compile(r"<ac:structured-macro.*?</ac:structured-macro>", re.S)


TS_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})"
    r"(?:\.(\d+))?\s*(Z|[+-]\d{2}:?\d{2})?")


def parse_ts(value):
    """Parse a Confluence timestamp on any Python >= 3.7.

    datetime.fromisoformat only became lenient in 3.11, and instances vary in
    whether they emit 'Z', '+0000' or '+00:00', so parse it by hand.
    """
    if not value:
        return None
    m = TS_RE.match(value.strip())
    if not m:
        return None
    year, month, day, hour, minute, second = (int(g) for g in m.group(1, 2, 3, 4, 5, 6))
    micro = int((m.group(7) or "0").ljust(6, "0")[:6])
    offset = m.group(8)
    if offset in (None, "Z", "z"):
        tz = timezone.utc
    else:
        sign = -1 if offset[0] == "-" else 1
        digits = offset[1:].replace(":", "")
        tz = timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:4])))
    return datetime(year, month, day, hour, minute, second, micro, tz)


def days_since(dt):
    return None if dt is None else (NOW - dt).days


def iso(dt):
    return dt.isoformat() if dt else ""


def word_count(storage_html):
    if not storage_html:
        return 0
    text = MACRO_RE.sub(" ", storage_html)
    text = TAG_RE.sub(" ", text)
    return len(html.unescape(text).split())


def human_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024


def person(node):
    """Pull a display name out of the various user shapes Confluence returns."""
    if not node:
        return ""
    user = node.get("by") or node.get("creator") or node
    return user.get("publicName") or user.get("displayName") or user.get("username") or ""


def print_table(rows, headers, aligns=None):
    if not rows:
        print("  (none)")
        return
    cols = list(zip(*([headers] + [[str(c) for c in r] for r in rows])))
    widths = [min(max(len(c) for c in col), 60) for col in cols]
    aligns = aligns or ["<"] * len(headers)

    def line(cells):
        return "  ".join(
            f"{str(c)[:w]:{a}{w}}" for c, w, a in zip(cells, widths, aligns)
        ).rstrip()

    print(line(headers))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print(line(r))


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def _space_detail(api, key):
    """GET one space, or None if the server says it does not exist."""
    for expand in ("description.plain,homepage,metadata.labels",
                   "description.plain,homepage", None):
        try:
            return api.get(f"/rest/api/space/{key}",
                           {"expand": expand} if expand else None)
        except NotFound:
            return None
        except requests.HTTPError:
            continue      # this instance rejects that expand; try a smaller one
    return None


def lookup_space(api, key):
    """Search every visible space for `key`, by key or name, ignoring case.

    Returns (exact_match_or_None, near_misses). Used when the by-key endpoint
    404s, which usually means a case difference, a space name typed in place of
    its key, or a personal/archived space outside the default listing.
    """
    wanted = key.strip().lower()
    near, seen = [], set()
    for status in ("current", "archived"):
        try:
            spaces = list(api.paginate("/rest/api/space", {"status": status}))
        except (ConfluenceError, requests.HTTPError):
            continue
        for s in spaces:
            k = (s.get("key") or "").lower()
            n = (s.get("name") or "").lower()
            if wanted in (k, n):
                return s, near
            if (wanted in k or wanted in n) and k not in seen:
                seen.add(k)
                near.append(s)
    return None, near


def fetch_space(api, key):
    space = _space_detail(api, key)
    if space:
        return space

    match, near = lookup_space(api, key)
    if match:
        real = match.get("key")
        if real != key:
            print(f"  note: no space keyed '{key}'; matched '{real}' "
                  f"({match.get('name')})", file=sys.stderr)
        return _space_detail(api, real) or match

    msg = f"No space matching '{key}' is visible to your account."
    if near:
        msg += "\n  Close matches:\n" + "\n".join(
            f"    {s.get('key', ''):<14} {s.get('name', '')}" for s in near[:10])
    else:
        msg += ("\n  Space keys are case-sensitive. Run "
                "`confluence.py spaces --type all` to list what you can see,\n"
                "  or `confluence.py spaces --contains <text>` to search by name.")
    raise ConfluenceError(msg)


def fetch_content(api, key, ctype, with_body=False, with_restrictions=False,
                  include_archived=False, cap=None, progress=None):
    expand = ["version", "history", "history.createdBy", "history.lastUpdated",
              "ancestors", "metadata.labels", "extensions"]
    if with_body:
        expand.append("body.storage")
    if with_restrictions:
        expand += ["restrictions.read.restrictions.user",
                   "restrictions.read.restrictions.group"]
    out = []
    for status in (["current", "archived"] if include_archived else ["current"]):
        params = {"spaceKey": key, "type": ctype, "status": status,
                  "expand": ",".join(expand)}
        try:
            for item in api.paginate("/rest/api/content", params, cap=cap,
                                     progress=progress):
                item["_status"] = status
                out.append(item)
        except (ConfluenceError, requests.HTTPError) as exc:
            if status == "archived":
                print(f"  (archived {ctype}s unavailable: {exc})", file=sys.stderr)
            else:
                raise
    return out


def fetch_by_cql(api, cql, expand, progress=None):
    return list(api.paginate("/rest/api/content/search",
                             {"cql": cql, "expand": expand}, progress=progress))


def fetch_attachments(api, key, page_ids):
    expand = "version,history,container,extensions"
    try:
        return fetch_by_cql(api, f'space="{key}" and type=attachment', expand,
                            progress="attachments")
    except (ConfluenceError, requests.HTTPError) as exc:
        print(f"  CQL attachment search failed ({exc}); falling back to per-page lookups",
              file=sys.stderr)
        found = []
        for i, pid in enumerate(page_ids, 1):
            if i % 50 == 0 and sys.stderr.isatty():
                print(f"\r  attachments: page {i}/{len(page_ids)}", end="",
                      file=sys.stderr, flush=True)
            for att in api.paginate(f"/rest/api/content/{pid}/child/attachment",
                                    {"expand": "version,history,extensions"}):
                att.setdefault("container", {"id": pid})
                found.append(att)
        return found


# --------------------------------------------------------------------------
# shaping
# --------------------------------------------------------------------------

def shape_page(api, item):
    history = item.get("history") or {}
    version = item.get("version") or {}
    ancestors = item.get("ancestors") or []
    labels = [l.get("name") for l in
              (((item.get("metadata") or {}).get("labels") or {}).get("results") or [])]
    created = parse_ts(history.get("createdDate"))
    updated = parse_ts(((history.get("lastUpdated") or {}).get("when")) or version.get("when"))
    webui = (item.get("_links") or {}).get("webui", "")
    read = (item.get("restrictions") or {}).get("read") or {}
    r_users = (((read.get("restrictions") or {}).get("user") or {}).get("results") or [])
    r_groups = (((read.get("restrictions") or {}).get("group") or {}).get("results") or [])
    body = ((item.get("body") or {}).get("storage") or {}).get("value")
    return {
        "id": item.get("id"),
        "type": item.get("type"),
        "status": item.get("_status", item.get("status")),
        "title": item.get("title", ""),
        "url": (api.link_base + webui) if webui else "",
        "created": created,
        "created_by": person(history.get("createdBy")) or person(history),
        "updated": updated,
        "updated_by": person(history.get("lastUpdated")) or person(version),
        "days_since_update": days_since(updated),
        "version": version.get("number"),
        "parent_id": ancestors[-1]["id"] if ancestors else None,
        "parent_title": ancestors[-1].get("title") if ancestors else "",
        "depth": len(ancestors),
        "labels": labels,
        "restricted": bool(r_users or r_groups),
        "restricted_to": ", ".join([person(u) for u in r_users]
                                   + [g.get("name", "") for g in r_groups]),
        "word_count": word_count(body) if body is not None else None,
    }


def shape_attachment(api, item):
    ext = item.get("extensions") or {}
    history = item.get("history") or {}
    version = item.get("version") or {}
    container = item.get("container") or {}
    webui = (item.get("_links") or {}).get("webui", "")
    updated = parse_ts((history.get("lastUpdated") or {}).get("when") or version.get("when"))
    return {
        "id": item.get("id"),
        "title": item.get("title", ""),
        "media_type": ext.get("mediaType", ""),
        "bytes": ext.get("fileSize") or 0,
        "version": version.get("number"),
        "created": parse_ts(history.get("createdDate")),
        "created_by": person(history.get("createdBy")) or person(history),
        "updated": updated,
        "days_since_update": days_since(updated),
        "page_id": container.get("id", ""),
        "page_title": container.get("title", ""),
        "url": (api.link_base + webui) if webui else "",
    }


def shape_comment(api, item):
    history = item.get("history") or {}
    container = item.get("container") or {}
    created = parse_ts(history.get("createdDate"))
    return {
        "id": item.get("id"),
        "page_id": container.get("id", ""),
        "page_title": container.get("title", ""),
        "created": created,
        "created_by": person(history.get("createdBy")) or person(history),
        "days_old": days_since(created),
    }


# --------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------

def _sum_by(rows, key, value):
    out = Counter()
    for r in rows:
        out[r.get(key) or "unknown"] += r.get(value) or 0
    return out


def analyse(space, pages, attachments, comments, stale_days):
    children = Counter(p["parent_id"] for p in pages if p["parent_id"])
    att_by_page = defaultdict(list)
    for a in attachments:
        att_by_page[str(a["page_id"])].append(a)
    comments_by_page = Counter(str(c["page_id"]) for c in comments)

    homepage_id = str(((space.get("homepage") or {}).get("id")) or "")
    docs = [p for p in pages if p["type"] == "page"]

    for p in pages:
        pid = str(p["id"])
        p["child_count"] = children.get(pid, 0)
        p["attachment_count"] = len(att_by_page.get(pid, []))
        p["attachment_bytes"] = sum(a["bytes"] for a in att_by_page.get(pid, []))
        p["comment_count"] = comments_by_page.get(pid, 0)

    def bucket(p):
        d = p["days_since_update"]
        if d is None:
            return "unknown"
        for limit, name in ((30, "<=30 days"), (90, "31-90 days"),
                            (365, "91-365 days"), (730, "1-2 years")):
            if d <= limit:
                return name
        return "2+ years"

    stale = sorted((p for p in pages if (p["days_since_update"] or 0) >= stale_days),
                   key=lambda p: p["days_since_update"], reverse=True)
    dup_titles = {t: n for t, n in
                  Counter(p["title"].strip().lower() for p in docs).items() if n > 1}
    sized = [p for p in pages if p["word_count"] is not None]

    return {
        "totals": {
            "pages": sum(1 for p in pages if p["type"] == "page"),
            "blogposts": sum(1 for p in pages if p["type"] == "blogpost"),
            "archived": sum(1 for p in pages if p["status"] == "archived"),
            "attachments": len(attachments),
            "attachment_bytes": sum(a["bytes"] for a in attachments),
            "comments": len(comments),
            "restricted_pages": sum(1 for p in pages if p["restricted"]),
        },
        "freshness": Counter(bucket(p) for p in pages),
        "created_by_year": Counter(p["created"].year for p in pages if p["created"]),
        "updated_by_year": Counter(p["updated"].year for p in pages if p["updated"]),
        "top_authors": Counter(p["created_by"] for p in pages if p["created_by"]),
        "top_editors": Counter(p["updated_by"] for p in pages if p["updated_by"]),
        "labels": Counter(l for p in pages for l in p["labels"]),
        "unlabeled": sum(1 for p in pages if not p["labels"]),
        "max_depth": max((p["depth"] for p in pages), default=0),
        "depth_hist": Counter(p["depth"] for p in docs),
        "orphans": [p for p in docs if p["depth"] == 0 and str(p["id"]) != homepage_id],
        "stale": stale,
        "duplicate_titles": dup_titles,
        "leaf_pages": sum(1 for p in docs if p["child_count"] == 0),
        "media_types": Counter(a["media_type"] or "unknown" for a in attachments),
        "media_bytes": _sum_by(attachments, "media_type", "bytes"),
        "largest_attachments": sorted(attachments, key=lambda a: a["bytes"], reverse=True)[:20],
        "biggest_pages": sorted(sized, key=lambda p: p["word_count"], reverse=True)[:20],
        "stub_pages": [p for p in sized if p["word_count"] < 50],
        "busiest_pages": sorted(pages, key=lambda p: p["comment_count"], reverse=True)[:10],
        "homepage_id": homepage_id,
    }


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

def write_csv(path, rows, columns):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            out = {}
            for c in columns:
                v = row.get(c)
                if isinstance(v, datetime):
                    v = iso(v)
                elif isinstance(v, list):
                    v = ", ".join(str(x) for x in v)
                out[c] = v
            w.writerow(out)


def write_report(path, space, api, stats, stale_days, with_body):
    t = stats["totals"]
    L = []
    add = L.append
    desc = (((space.get("description") or {}).get("plain") or {}).get("value") or "").strip()

    add(f"# Confluence inventory — {space.get('name', '?')} ({space.get('key')})")
    add("")
    add(f"Generated {NOW:%Y-%m-%d %H:%M UTC} from {api.base}")
    if desc:
        add("")
        add(f"> {desc}")
    add("")
    add("## Totals")
    add("")
    add("| Item | Count |")
    add("| --- | ---: |")
    add(f"| Pages (current) | {t['pages'] - t['archived']:,} |")
    add(f"| Pages (archived) | {t['archived']:,} |")
    add(f"| Blog posts | {t['blogposts']:,} |")
    add(f"| Attachments | {t['attachments']:,} ({human_bytes(t['attachment_bytes'])}) |")
    add(f"| Comments | {t['comments']:,} |")
    add(f"| Pages with view restrictions | {t['restricted_pages']:,} |")
    add(f"| Distinct labels | {len(stats['labels']):,} |")
    add(f"| Unlabeled pages | {stats['unlabeled']:,} |")
    add("")

    add("## Freshness (last edit)")
    add("")
    add("| Age | Pages |")
    add("| --- | ---: |")
    for name in ("<=30 days", "31-90 days", "91-365 days", "1-2 years",
                 "2+ years", "unknown"):
        if stats["freshness"].get(name):
            add(f"| {name} | {stats['freshness'][name]:,} |")
    add("")
    add(f"### Stalest content (untouched {stale_days}+ days) — top 25 of {len(stats['stale']):,}")
    add("")
    if stats["stale"]:
        add("| Days | Title | Last editor | Link |")
        add("| ---: | --- | --- | --- |")
        for p in stats["stale"][:25]:
            add(f"| {p['days_since_update']:,} | {p['title'][:70]} | {p['updated_by']} | {p['url']} |")
    else:
        add("_Nothing older than the threshold._")
    add("")

    add("## People")
    add("")
    add("| Top page creators | Pages | Most recent editors | Pages |")
    add("| --- | ---: | --- | ---: |")
    creators = stats["top_authors"].most_common(10)
    editors = stats["top_editors"].most_common(10)
    for i in range(max(len(creators), len(editors))):
        c = f"{creators[i][0]} | {creators[i][1]:,}" if i < len(creators) else " | "
        e = f"{editors[i][0]} | {editors[i][1]:,}" if i < len(editors) else " | "
        add(f"| {c} | {e} |")
    add("")

    add("## Structure")
    add("")
    add(f"- Max nesting depth: **{stats['max_depth']}**")
    add(f"- Leaf pages (no children): **{stats['leaf_pages']:,}**")
    add(f"- Top-level orphans (no parent, not the homepage): **{len(stats['orphans']):,}**")
    add(f"- Duplicate titles: **{len(stats['duplicate_titles']):,}**")
    add("")
    add("| Depth | Pages |")
    add("| ---: | ---: |")
    for depth in sorted(stats["depth_hist"]):
        add(f"| {depth} | {stats['depth_hist'][depth]:,} |")
    add("")
    if stats["orphans"]:
        add("### Orphan pages (top 20)")
        add("")
        for p in stats["orphans"][:20]:
            add(f"- [{p['title']}]({p['url']}) — last edited {p['days_since_update']} "
                f"days ago by {p['updated_by']}")
        add("")
    if stats["duplicate_titles"]:
        add("### Duplicate titles (top 20)")
        add("")
        for title, n in sorted(stats["duplicate_titles"].items(),
                               key=lambda kv: kv[1], reverse=True)[:20]:
            add(f"- `{title}` — {n} pages")
        add("")

    add("## Labels")
    add("")
    if stats["labels"]:
        add("| Label | Pages |")
        add("| --- | ---: |")
        for name, n in stats["labels"].most_common(25):
            add(f"| {name} | {n:,} |")
    else:
        add("_No labels in use._")
    add("")

    add("## Attachments")
    add("")
    add("| Media type | Files | Size |")
    add("| --- | ---: | ---: |")
    for mt, n in stats["media_types"].most_common(15):
        add(f"| {mt} | {n:,} | {human_bytes(stats['media_bytes'][mt])} |")
    add("")
    if stats["largest_attachments"]:
        add("### Largest files")
        add("")
        add("| Size | File | On page |")
        add("| ---: | --- | --- |")
        for a in stats["largest_attachments"]:
            add(f"| {human_bytes(a['bytes'])} | {a['title'][:60]} | {a['page_title'][:50]} |")
        add("")

    if with_body:
        add("## Page size")
        add("")
        add(f"- Stub pages (<50 words): **{len(stats['stub_pages']):,}**")
        add("")
        if stats["biggest_pages"]:
            add("| Words | Title |")
            add("| ---: | --- |")
            for p in stats["biggest_pages"]:
                add(f"| {p['word_count']:,} | {p['title'][:70]} |")
            add("")
        if stats["stub_pages"]:
            add("### Stubs (top 20)")
            add("")
            for p in stats["stub_pages"][:20]:
                add(f"- [{p['title']}]({p['url']}) — {p['word_count']} words")
            add("")

    busiest = [p for p in stats["busiest_pages"] if p["comment_count"]]
    if busiest:
        add("## Most discussed pages")
        add("")
        for p in busiest:
            n = p["comment_count"]
            add(f"- [{p['title']}]({p['url']}) — {n} comment{'' if n == 1 else 's'}")
        add("")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_check(args):
    api = connect(args)
    print(f"Base URL: {api.base}")
    print(f"Auth:     {api.auth_mode}")
    try:
        me = api.get("/rest/api/user/current")
        name = me.get("displayName") or me.get("publicName") or me.get("username")
        print(f"User:     {name} ({me.get('username') or me.get('accountId', '')})")
    except ConfluenceError as exc:
        if "401" in str(exc) or "403" in str(exc):
            raise
        print(f"User:     could not read /rest/api/user/current ({exc})")
    except requests.HTTPError as exc:
        print(f"User:     could not read /rest/api/user/current ({exc})")
    spaces = list(api.paginate("/rest/api/space", {"status": "current"}, cap=1000))
    globals_ = [s for s in spaces if s.get("type") == "global"]
    print(f"Visible:  {len(spaces):,} spaces ({len(globals_):,} global, "
          f"{len(spaces) - len(globals_):,} personal)")
    print("\nConnection OK.")
    return 0


def cmd_spaces(args):
    api = connect(args)
    params = {"status": "archived" if args.archived else "current",
              "expand": "description.plain"}
    if args.type != "all":
        params["type"] = args.type
    spaces = list(api.paginate("/rest/api/space", params, progress="spaces"))
    if args.contains:
        needle = args.contains.lower()
        spaces = [s for s in spaces
                  if needle in (s.get("key", "") + " " + s.get("name", "")).lower()]
    spaces.sort(key=lambda s: s.get("key", ""))

    rows = []
    for s in spaces:
        desc = (((s.get("description") or {}).get("plain") or {}).get("value") or "")
        row = [s.get("key", ""), s.get("name", ""), s.get("type", ""),
               desc.replace("\n", " ")[:50]]
        if args.counts:
            key = s.get("key")
            n_pages = api.count_cql('space="%s" and type=page' % key)
            n_files = api.count_cql('space="%s" and type=attachment' % key)
            row.insert(3, format(n_pages, ","))
            row.insert(4, format(n_files, ","))
        rows.append(row)

    headers = ["KEY", "NAME", "TYPE", "DESCRIPTION"]
    aligns = ["<", "<", "<", "<"]
    if args.counts:
        headers[3:3] = ["PAGES", "FILES"]
        aligns = ["<", "<", "<", ">", ">", "<"]
    print()
    print_table(rows, headers, aligns)
    print(f"\n{len(rows):,} spaces")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(headers)
            w.writerows(rows)
        print(f"Wrote {args.csv}")
    return 0


def cmd_space(args):
    api = connect(args)
    space = fetch_space(api, resolve_space(args))
    key = space.get("key")
    desc = (((space.get("description") or {}).get("plain") or {}).get("value") or "").strip()
    home = space.get("homepage") or {}

    print(f"\n{space.get('name')}  [{space.get('key')}]")
    print(f"  type:     {space.get('type')} / {space.get('status', 'current')}")
    if desc:
        print(f"  about:    {desc[:200]}")
    if home:
        print(f"  homepage: {home.get('title')} "
              f"({api.link_base}{(home.get('_links') or {}).get('webui', '')})")

    if sys.stdout.isatty():
        print("\n  counting content...", end="", flush=True)
    counts = {}
    for label, cql in (("pages", f'space="{key}" and type=page'),
                       ("blog posts", f'space="{key}" and type=blogpost'),
                       ("attachments", f'space="{key}" and type=attachment'),
                       ("comments", f'space="{key}" and type=comment')):
        try:
            counts[label] = api.count_cql(cql)
        except (ConfluenceError, requests.HTTPError):
            counts[label] = None
    print("\r" + " " * 22 + "\r" if sys.stdout.isatty() else "")
    for label, n in counts.items():
        print(f"  {label + ':':13}{'n/a' if n is None else format(n, ',')}")

    # Ask the server to order by last edit; fall back to sorting a local sample
    # if the instance rejects the `order by` clause.
    try:
        recent = list(api.paginate(
            "/rest/api/content/search",
            {"cql": f'space="{key}" and type=page order by lastmodified desc',
             "expand": "version,history.lastUpdated,ancestors"},
            page_size=args.recent, cap=args.recent))
        heading = "Most recently edited"
    except (ConfluenceError, requests.HTTPError):
        recent = fetch_content(api, key, "page", cap=args.recent)
        heading = f"Recently edited (sample of {args.recent})"
    shaped = sorted((shape_page(api, p) for p in recent),
                    key=lambda p: p["updated"] or NOW, reverse=True)[:args.recent]
    print(f"\n  {heading}:")
    print_table([[p["updated"].strftime("%Y-%m-%d") if p["updated"] else "?",
                  p["title"][:55], p["updated_by"][:22]] for p in shaped],
                ["UPDATED", "TITLE", "BY"])
    print("\nRun `inventory` on this space for the full report.")
    return 0


def cmd_pages(args):
    api = connect(args)
    key = fetch_space(api, resolve_space(args)).get("key")
    raw = fetch_content(api, key, "page", include_archived=args.include_archived,
                        cap=args.limit, progress="pages")
    pages = [shape_page(api, p) for p in raw]
    order = {"updated": lambda p: p["updated"] or NOW,
             "created": lambda p: p["created"] or NOW,
             "title": lambda p: p["title"].lower(),
             "depth": lambda p: (p["depth"], p["title"].lower())}[args.sort]
    pages.sort(key=order, reverse=args.sort in ("updated", "created"))

    print()
    print_table([[p["updated"].strftime("%Y-%m-%d") if p["updated"] else "?",
                  p["days_since_update"] if p["days_since_update"] is not None else "?",
                  ("  " * min(p["depth"], 5)) + p["title"][:55],
                  p["updated_by"][:20], ",".join(p["labels"])[:24]] for p in pages],
                ["UPDATED", "AGE", "TITLE", "BY", "LABELS"],
                ["<", ">", "<", "<", "<"])
    print(f"\n{len(pages):,} pages")
    if args.csv:
        write_csv(args.csv, pages,
                  ["id", "type", "status", "title", "url", "created", "created_by",
                   "updated", "updated_by", "days_since_update", "version", "depth",
                   "parent_id", "parent_title", "labels", "restricted"])
        print(f"Wrote {args.csv}")
    return 0


def cmd_inventory(args):
    api = connect(args)
    print(f"Auth:  {api.auth_mode}")
    space = fetch_space(api, resolve_space(args))
    key = space.get("key")
    out_dir = args.out_dir or f"inventory-{key}-{NOW:%Y-%m-%d}"
    os.makedirs(out_dir, exist_ok=True)
    print(f"Space: {key} @ {api.base}")
    print(f"       {space.get('name')} (type={space.get('type')})")

    raw_pages = []
    for ctype in ("page", "blogpost"):
        got = fetch_content(api, key, ctype, args.with_body, args.with_restrictions,
                            args.include_archived, progress=f"{ctype}s")
        print(f"  {ctype}s: {len(got):,}")
        raw_pages += got
    pages = [shape_page(api, p) for p in raw_pages]

    attachments = []
    if not args.skip_attachments:
        attachments = [shape_attachment(api, a)
                       for a in fetch_attachments(api, key, [p["id"] for p in pages])]
        print(f"  attachments: {len(attachments):,}")

    comments = []
    if not args.skip_comments:
        try:
            comments = [shape_comment(api, c) for c in
                        fetch_by_cql(api, f'space="{key}" and type=comment',
                                     "history,container", progress="comments")]
            print(f"  comments: {len(comments):,}")
        except (ConfluenceError, requests.HTTPError) as exc:
            print(f"  comments: skipped ({exc})")

    stats = analyse(space, pages, attachments, comments, args.stale_days)

    write_csv(os.path.join(out_dir, "pages.csv"), pages,
              ["id", "type", "status", "title", "url", "created", "created_by",
               "updated", "updated_by", "days_since_update", "version", "depth",
               "parent_id", "parent_title", "child_count", "labels",
               "attachment_count", "attachment_bytes", "comment_count",
               "word_count", "restricted", "restricted_to"])
    if attachments:
        write_csv(os.path.join(out_dir, "attachments.csv"), attachments,
                  ["id", "title", "media_type", "bytes", "version", "created",
                   "created_by", "updated", "days_since_update", "page_id",
                   "page_title", "url"])
    if comments:
        write_csv(os.path.join(out_dir, "comments.csv"), comments,
                  ["id", "page_id", "page_title", "created", "created_by", "days_old"])

    def jsonable(o):
        if isinstance(o, datetime):
            return o.isoformat()
        if isinstance(o, Counter):
            return {str(k): v for k, v in o.most_common()}
        raise TypeError(type(o))

    summary = {k: v for k, v in stats.items()
               if k not in ("orphans", "stale", "largest_attachments",
                            "biggest_pages", "stub_pages", "busiest_pages")}
    with open(os.path.join(out_dir, "inventory.json"), "w", encoding="utf-8") as fh:
        json.dump({"generated": NOW.isoformat(), "base_url": api.base,
                   "space": {k: space.get(k) for k in ("id", "key", "name", "type", "status")},
                   "summary": summary, "pages": pages,
                   "attachments": attachments, "comments": comments},
                  fh, indent=2, default=jsonable)

    write_report(os.path.join(out_dir, "report.md"), space, api, stats,
                 args.stale_days, args.with_body)

    t = stats["totals"]
    print()
    print(f"{t['pages']:,} pages, {t['blogposts']:,} blog posts, "
          f"{t['attachments']:,} attachments ({human_bytes(t['attachment_bytes'])}), "
          f"{t['comments']:,} comments")
    print(f"{len(stats['stale']):,} items untouched for {args.stale_days}+ days; "
          f"{stats['unlabeled']:,} unlabeled; {len(stats['orphans']):,} orphans")
    print(f"\nWrote {out_dir}/ -> report.md, pages.csv, attachments.csv, inventory.json")
    return 0


# --------------------------------------------------------------------------

def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--env-file", help="path to a .env file (default: ./.env, then script dir)")
    common.add_argument("--base-url", help="overrides CONFLUENCE_BASE_URL")
    common.add_argument("--token", help="overrides CONFLUENCE_TOKEN")
    common.add_argument("--email", help="Cloud only: overrides CONFLUENCE_EMAIL (Basic auth)")
    common.add_argument("--insecure", action="store_true", help="skip TLS verification")

    ap = argparse.ArgumentParser(
        prog="confluence.py",
        description="Inventory a client-hosted Confluence instance.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check", parents=[common], help="verify URL + token work")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("spaces", parents=[common], help="list spaces you can see")
    p.add_argument("--type", choices=["global", "personal", "all"], default="global")
    p.add_argument("--archived", action="store_true", help="list archived spaces instead")
    p.add_argument("--contains", help="filter on key or name substring")
    p.add_argument("--counts", action="store_true",
                   help="add page/attachment counts (2 extra calls per space)")
    p.add_argument("--csv", help="also write the table to this CSV path")
    p.set_defaults(func=cmd_spaces)

    p = sub.add_parser("space", parents=[common], help="quick summary of one space")
    p.add_argument("space", nargs="?", help="space key (default: CONFLUENCE_SPACE)")
    p.add_argument("--recent", type=int, default=10, help="recently-edited sample size")
    p.set_defaults(func=cmd_space)

    p = sub.add_parser("pages", parents=[common], help="list pages in a space")
    p.add_argument("space", nargs="?", help="space key (default: CONFLUENCE_SPACE)")
    p.add_argument("--sort", choices=["updated", "created", "title", "depth"],
                   default="updated")
    p.add_argument("--limit", type=int, help="stop after N pages")
    p.add_argument("--include-archived", action="store_true")
    p.add_argument("--csv", help="also write full page metadata to this CSV path")
    p.set_defaults(func=cmd_pages)

    p = sub.add_parser("inventory", parents=[common],
                       help="full inventory: report.md + CSVs + JSON")
    p.add_argument("space", nargs="?", help="space key (default: CONFLUENCE_SPACE)")
    p.add_argument("--out-dir", help="default: ./inventory-<SPACE>-<YYYY-MM-DD>")
    p.add_argument("--stale-days", type=int, default=365,
                   help="flag content untouched this long (default 365)")
    p.add_argument("--with-body", action="store_true",
                   help="fetch bodies for word counts / stub detection (slower)")
    p.add_argument("--with-restrictions", action="store_true",
                   help="expand read restrictions per page (slower)")
    p.add_argument("--include-archived", action="store_true")
    p.add_argument("--skip-comments", action="store_true")
    p.add_argument("--skip-attachments", action="store_true")
    p.set_defaults(func=cmd_inventory)

    return ap


def main():
    args = build_parser().parse_args()
    env_path = find_env_file(getattr(args, "env_file", None))
    load_env(env_path)
    if getattr(args, "env_file", None) and not env_path:
        sys.exit(f"Error: no such .env file: {args.env_file}")
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ConfluenceError as exc:
        sys.exit(f"\nError: {exc}")
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
