#!/usr/bin/env python3
"""
Confluence inventory tool for client-hosted (Data Center / Server) Confluence.

Configuration comes from a .env file in the current or script directory
(see .env.example). Command-line flags override it.

Commands, smallest first:

    check              verify the URL + token work, show who you are
    spaces             list spaces you can see
    space KEY          quick summary of one space (a few API calls)
    tree KEY           page hierarchy with per-section rollups
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
    def __init__(self, base_url, token, email=None, verify=True, timeout=60,
                 rate=None, max_calls=None):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.link_base = self.base
        self.calls = 0
        self.max_calls = max_calls
        # rate is requests per second; None means no artificial delay
        self.min_interval = (1.0 / rate) if rate else 0
        self._last_request = 0.0
        self.s = requests.Session()
        self.s.verify = verify
        self.s.headers["Accept"] = "application/json"
        if email:
            self.s.auth = HTTPBasicAuth(email, token)
            self.auth_mode = f"HTTP Basic as {email} (Cloud-style API token)"
        else:
            self.s.headers["Authorization"] = f"Bearer {token}"
            self.auth_mode = "Bearer token (Data Center / Server PAT)"

    def _throttle(self):
        """Hold requests to --rate, and stop dead at the --max-calls budget."""
        if self.max_calls is not None and self.calls >= self.max_calls:
            raise ConfluenceError(
                f"Stopped at the {self.max_calls}-call budget (--max-calls).\n"
                "  Raise it, or narrow the run with --skip-attachments / "
                "--skip-comments / --limit."
            )
        if self.min_interval:
            wait = self.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
        self._last_request = time.monotonic()
        self.calls += 1

    def get(self, path, params=None):
        url = path if path.startswith("http") else self.base + path
        last = None
        for attempt in range(6):
            self._throttle()
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


SESSIONS = []


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

    def _num(flag_value, env_name, cast):
        raw = flag_value if flag_value is not None else os.environ.get(env_name)
        if raw in (None, ""):
            return None
        try:
            value = cast(raw)
        except ValueError:
            raise ConfluenceError(f"{env_name} must be a number, got {raw!r}")
        return value if value > 0 else None

    api = Confluence(base, token, email, verify=verify,
                     rate=_num(args.rate, "CONFLUENCE_RATE", float),
                     max_calls=_num(args.max_calls, "CONFLUENCE_MAX_CALLS", int))
    SESSIONS.append(api)
    return api


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


REL_DATE_RE = re.compile(r"^(\d+)\s*([dwmy])$", re.I)
DAYS_PER = {"d": 1, "w": 7, "m": 30, "y": 365}


def cql_date(value):
    """Turn '2025-01-01' or a relative '90d' / '6m' into a CQL date literal."""
    if not value:
        return None
    text = value.strip()
    m = REL_DATE_RE.match(text)
    if m:
        return 'now("-%dd")' % (int(m.group(1)) * DAYS_PER[m.group(2).lower()])
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        raise ConfluenceError(
            f"Bad date {value!r}. Use YYYY-MM-DD, or a relative window "
            "like 90d, 12w, 6m, 2y."
        )
    return '"%s"' % text


def build_cql(key, ctype, since=None, created_since=None):
    parts = ['space="%s"' % key, "type=%s" % ctype]
    if since:
        parts.append("lastmodified >= %s" % since)
    if created_since:
        parts.append("created >= %s" % created_since)
    return " and ".join(parts)


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
                  include_archived=False, cap=None, progress=None,
                  since=None, created_since=None):
    expand = ["version", "history", "history.createdBy", "history.lastUpdated",
              "ancestors", "metadata.labels", "extensions"]
    if with_body:
        expand.append("body.storage")
    if with_restrictions:
        expand += ["restrictions.read.restrictions.user",
                   "restrictions.read.restrictions.group"]

    # The by-space content endpoint has no date filter, so a windowed run goes
    # through CQL search instead. Search covers current content only.
    if since or created_since:
        params = {"cql": build_cql(key, ctype, since, created_since),
                  "expand": ",".join(expand)}
        out = []
        for item in api.paginate("/rest/api/content/search", params, cap=cap,
                                 progress=progress):
            item["_status"] = item.get("status", "current")
            out.append(item)
        return out

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


def fetch_attachments(api, key, page_ids, since=None, created_since=None):
    expand = "version,history,container,extensions"
    try:
        return fetch_by_cql(api, build_cql(key, "attachment", since, created_since),
                            expand, progress="attachments")
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
        "ancestor_chain": [(str(a.get("id")), a.get("title") or "") for a in ancestors],
        "root_id": str(ancestors[0]["id"]) if ancestors else str(item.get("id")),
        "root_title": (ancestors[0].get("title") if ancestors
                       else item.get("title", "")),
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
# hierarchy
# --------------------------------------------------------------------------

def build_forest(pages):
    """Reconstruct the page tree from each page's ancestor chain.

    Using the chain rather than parent links means a windowed run still lands
    every page under its real section, even when the intervening parents were
    not themselves fetched. Nodes with no page of their own are marked
    `fetched=False` - they exist as ancestors only.
    """
    nodes = {}
    roots = {}

    def node(nid, title):
        n = nodes.get(nid)
        if n is None:
            n = nodes[nid] = {"id": nid, "title": title or "(untitled)",
                              "children": [], "child_ids": set(),
                              "page": None, "fetched": False}
        elif title and n["title"] == "(untitled)":
            n["title"] = title
        return n

    for p in pages:
        if p["type"] != "page":
            continue
        parent = None
        for aid, atitle in p.get("ancestor_chain") or []:
            n = node(aid, atitle)
            if parent is None:
                roots[aid] = n
            elif aid not in parent["child_ids"]:
                parent["child_ids"].add(aid)
                parent["children"].append(n)
            parent = n
        me = node(str(p["id"]), p["title"])
        me["page"] = p
        me["fetched"] = True
        if parent is None:
            roots[str(p["id"])] = me
        elif me["id"] not in parent["child_ids"]:
            parent["child_ids"].add(me["id"])
            parent["children"].append(me)

    return sorted(roots.values(), key=lambda n: n["title"].lower())


def summarize_node(node, stale_days):
    """Post-order rollup: pages, freshness, people and files under each node."""
    pages = 1 if node["fetched"] else 0
    stale = 0
    updated = None
    people = set()
    files = 0
    fbytes = 0
    p = node["page"]
    if p:
        updated = p["updated"]
        if p["updated_by"]:
            people.add(p["updated_by"])
        files = p.get("attachment_count", 0)
        fbytes = p.get("attachment_bytes", 0)
        if (p["days_since_update"] or 0) >= stale_days:
            stale = 1
    for child in node["children"]:
        c = summarize_node(child, stale_days)
        pages += c["pages"]
        stale += c["stale"]
        people |= c["people"]
        files += c["files"]
        fbytes += c["fbytes"]
        if c["updated"] and (updated is None or c["updated"] > updated):
            updated = c["updated"]
    node["roll"] = {"pages": pages, "stale": stale, "updated": updated,
                    "people": people, "files": files, "fbytes": fbytes}
    node["children"].sort(key=lambda n: -n["roll"]["pages"])
    return node["roll"]


def forest_stats(pages, stale_days):
    forest = build_forest(pages)
    for root in forest:
        summarize_node(root, stale_days)
    forest.sort(key=lambda n: -n["roll"]["pages"])
    return forest


def walk_forest(nodes, max_depth, depth=0):
    """Yield (node, depth, is_last) down to max_depth."""
    for i, n in enumerate(nodes):
        yield n, depth, i == len(nodes) - 1
        if depth + 1 < max_depth:
            for item in walk_forest(n["children"], max_depth, depth + 1):
                yield item


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
        "forest": forest_stats(pages, stale_days),
    }


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

def describe_window(since, created_since):
    def phrase(value):
        m = REL_DATE_RE.match((value or "").strip())
        if not m:
            return value
        unit = {"d": "day", "w": "week", "m": "month", "y": "year"}[m.group(2).lower()]
        n = int(m.group(1))
        return f"the last {n} {unit}{'' if n == 1 else 's'}"

    bits = []
    if since:
        bits.append(f"edited since {phrase(since)}"
                    if not REL_DATE_RE.match(since.strip())
                    else f"edited in {phrase(since)}")
    if created_since:
        bits.append(f"created since {phrase(created_since)}"
                    if not REL_DATE_RE.match(created_since.strip())
                    else f"created in {phrase(created_since)}")
    return " and ".join(bits)


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


def write_report(path, space, api, stats, stale_days, with_body, window=""):
    t = stats["totals"]
    L = []
    add = L.append
    desc = (((space.get("description") or {}).get("plain") or {}).get("value") or "").strip()

    add(f"# Confluence inventory — {space.get('name', '?')} ({space.get('key')})")
    add("")
    add(f"Generated {NOW:%Y-%m-%d %H:%M UTC} from {api.base}")
    if window:
        add("")
        add(f"**Scope: only content {window}.** Totals below count that window, "
            "not the whole space.")
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

    add("## Sections")
    add("")
    add("Top-level branches of the page tree, biggest first — the fastest way to "
        "see where the content actually lives.")
    add("")
    sections = [n for n in stats["forest"] if n["roll"]["pages"] >= 2][:25]
    if sections:
        add("| Pages | Stale | Last edit | People | Section |")
        add("| ---: | ---: | --- | ---: | --- |")
        for n in sections:
            roll = n["roll"]
            pct = (100.0 * roll["stale"] / roll["pages"]) if roll["pages"] else 0
            last = roll["updated"].strftime("%Y-%m-%d") if roll["updated"] else "—"
            add(f"| {roll['pages']:,} | {pct:.0f}% | {last} | "
                f"{len(roll['people'])} | {n['title'][:60]} |")
    else:
        add("_The space is flat — no page has children._")
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
# HTML report
# --------------------------------------------------------------------------

# Colors are the validated reference palette: one blue hue used as an ordinal
# ramp for age, ink tokens for all text. Bars are a single series, so no legend
# is needed - the section heading names what is plotted.
LIGHT_TOKENS = """
  color-scheme: light;
  --page:           #f4f4f1;
  --surface-1:      #fcfcfb;
  --border:         #e4e3de;
  --text-primary:   #0b0b0b;
  --text-secondary: #52514e;
  --text-muted:     #75746f;
  --series-1:       #2a78d6;
  --ramp-1: #86b6ef; --ramp-2: #5598e7; --ramp-3: #2a78d6;
  --ramp-4: #1c5cab; --ramp-5: #104281;
"""

# The same eight hues stepped for the dark surface, not an automatic flip.
DARK_TOKENS = """
  color-scheme: dark;
  --page:           #121211;
  --surface-1:      #1a1a19;
  --border:         #33332f;
  --text-primary:   #ffffff;
  --text-secondary: #c3c2b7;
  --text-muted:     #96958c;
  --series-1:       #3987e5;
  --ramp-1: #184f95; --ramp-2: #256abf; --ramp-3: #3987e5;
  --ramp-4: #6da7ec; --ramp-5: #9ec5f4;
"""

# Three scopes: light by default; dark when the OS says so unless the reader
# stamped light; dark whenever the reader stamped dark. The toggle wins both ways.
HTML_CSS = (
    ":root {" + LIGHT_TOKENS + "}\n"
    '@media (prefers-color-scheme: dark) {\n'
    '  :root:where(:not([data-theme="light"])) {' + DARK_TOKENS + "  }\n}\n"
    ':root[data-theme="dark"] {' + DARK_TOKENS + "}\n"
    """
* { box-sizing: border-box; }
body {
  margin: 0; padding: 0 20px 64px;
  background: var(--page); color: var(--text-primary);
  font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
.wrap { max-width: 1100px; margin: 0 auto; }
header { padding: 40px 0 24px; border-bottom: 1px solid var(--border); margin-bottom: 28px; }
h1 { margin: 0 0 6px; font-size: 26px; font-weight: 650; letter-spacing: -0.01em; }
.sub { color: var(--text-secondary); font-size: 13px; }
.sub code { color: var(--text-muted); }
h2 { font-size: 15px; font-weight: 650; margin: 36px 0 14px; letter-spacing: -0.005em; }
h3 { font-size: 13px; font-weight: 600; color: var(--text-secondary); margin: 22px 0 10px; }
.card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 18px 20px; }
.grid { display: grid; gap: 14px; }
.kpis { grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }
.two { grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }
.tile .label { color: var(--text-secondary); font-size: 12px; margin-bottom: 6px; }
.tile .value { font-size: 27px; font-weight: 650; line-height: 1.1; letter-spacing: -0.02em; }
.tile .note { color: var(--text-muted); font-size: 12px; margin-top: 4px; }

/* horizontal bars: max 24px thick, 4px rounded data-end, square at the baseline */
.bars { display: grid; gap: 8px; }
.bar-row { display: grid; grid-template-columns: 128px 1fr auto; align-items: center; gap: 12px; }
.bar-row .cat { color: var(--text-secondary); font-size: 12.5px; text-align: right;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bar-track { position: relative; height: 18px; }
.bar { height: 18px; max-height: 24px; border-radius: 0 4px 4px 0; background: var(--series-1);
  min-width: 2px; transition: filter .12s ease; }
.bar-row:hover .bar { filter: brightness(1.12); }
.bar-row .val { font-size: 12.5px; color: var(--text-secondary);
  font-variant-numeric: tabular-nums; min-width: 84px; }
.r1 { background: var(--ramp-1); } .r2 { background: var(--ramp-2); }
.r3 { background: var(--ramp-3); } .r4 { background: var(--ramp-4); }
.r5 { background: var(--ramp-5); }
.tip { position: absolute; left: 0; top: -30px; z-index: 5; display: none;
  background: var(--text-primary); color: var(--page); font-size: 12px;
  padding: 4px 8px; border-radius: 6px; white-space: nowrap; pointer-events: none; }
.bar-row:hover .tip { display: block; }

table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
th { color: var(--text-secondary); font-weight: 600; font-size: 12px; white-space: nowrap; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
tbody tr:hover { background: color-mix(in srgb, var(--series-1) 7%, transparent); }
a { color: var(--series-1); text-decoration: none; }
a:hover { text-decoration: underline; }
.muted { color: var(--text-muted); }
.scroll { overflow-x: auto; }
ul.plain { margin: 0; padding-left: 18px; }
ul.plain li { margin-bottom: 5px; }
.tag { display: inline-block; background: color-mix(in srgb, var(--series-1) 12%, transparent);
  color: var(--text-secondary); border-radius: 4px; padding: 1px 6px; font-size: 11.5px; margin-right: 4px; }

/* collapsible top-level sections - the report opens as an outline */
.panel { background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 10px; margin: 12px 0; }
.panel > summary { cursor: pointer; padding: 15px 18px; display: flex;
  align-items: baseline; gap: 10px; list-style: none; }
.panel > summary::-webkit-details-marker { display: none; }
.panel > summary::before { content: "\25B8"; color: var(--text-muted); font-size: 11px; }
.panel[open] > summary::before { content: "\25BE"; }
.panel > summary:hover .ptitle { color: var(--series-1); }
.ptitle { font-weight: 650; font-size: 15px; letter-spacing: -0.005em; }
.pmeta { color: var(--text-muted); font-size: 12.5px; margin-left: auto; }
.pbody { padding: 16px 18px 20px; border-top: 1px solid var(--border); }
.pbody > h3:first-child { margin-top: 0; }
.allctl { display: flex; gap: 8px; margin: 22px 0 2px; }

/* page tree */
.tree-list { list-style: none; margin: 0; padding-left: 17px; }
#tree { padding-left: 2px; max-height: 72vh; overflow: auto;
  border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; }
.tree-list li { margin: 2px 0; line-height: 1.55; }
.tree-list details > summary { cursor: pointer; }
.tree-list summary::marker { color: var(--text-muted); }
.tree-list summary:hover { background: color-mix(in srgb, var(--series-1) 8%, transparent); }
.tree-list details > .tree-list { border-left: 1px solid var(--border); margin-left: 5px; }
.nmeta { color: var(--text-muted); font-size: 12px; margin-left: 6px; }
.age { font-size: 11px; padding: 1px 5px; border-radius: 4px; margin-left: 4px;
  font-variant-numeric: tabular-nums; color: var(--text-secondary); }
.age.new { background: color-mix(in srgb, var(--series-1) 14%, transparent); }
.age.old { background: color-mix(in srgb, #e34948 18%, transparent); }

/* page index controls */
.controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 12px; }
input[type=search] { flex: 1 1 260px; min-width: 200px; padding: 7px 10px; font: inherit;
  font-size: 13px; color: var(--text-primary); background: var(--surface-1);
  border: 1px solid var(--border); border-radius: 7px; }
button.chip { font: inherit; font-size: 12.5px; cursor: pointer; padding: 6px 11px;
  border: 1px solid var(--border); border-radius: 7px; background: var(--surface-1);
  color: var(--text-secondary); }
button.chip[aria-pressed="true"] { background: var(--series-1); border-color: var(--series-1); color: #fff; }
th.sortable { cursor: pointer; user-select: none; }
th.sortable:hover { color: var(--text-primary); }
th[data-dir]:after { content: " \\2193"; }
th[data-dir="asc"]:after { content: " \\2191"; }
#count { color: var(--text-muted); font-size: 12.5px; }

/* theme switch */
.themebar { display: flex; gap: 6px; align-items: center; margin-left: auto; }
.themebar .lbl { color: var(--text-muted); font-size: 12px; margin-right: 2px; }
header .row { display: flex; align-items: flex-start; gap: 16px; flex-wrap: wrap; }
""")

HTML_JS = """
(function () {
  // theme switch - light / dark / follow the OS
  var root = document.documentElement;
  var themeBtns = Array.prototype.slice.call(document.querySelectorAll('[data-theme-set]'));
  function setTheme(mode, persist) {
    if (mode === 'system') { delete root.dataset.theme; }
    else { root.dataset.theme = mode; }
    themeBtns.forEach(function (b) {
      b.setAttribute('aria-pressed', String(b.dataset.themeSet === mode));
    });
    if (persist) { try { localStorage.setItem('ci-theme', mode); } catch (e) {} }
  }
  var saved = 'system';
  try { saved = localStorage.getItem('ci-theme') || 'system'; } catch (e) {}
  setTheme(saved, false);
  themeBtns.forEach(function (b) {
    b.addEventListener('click', function () { setTheme(b.dataset.themeSet, true); });
  });

  // expand / collapse every top-level panel
  function allPanels(open) {
    Array.prototype.slice.call(document.querySelectorAll('details.panel'))
      .forEach(function (d) { d.open = open; });
  }
  var openAll = document.getElementById('openall');
  var shutAll = document.getElementById('shutall');
  if (openAll) openAll.addEventListener('click', function () { allPanels(true); });
  if (shutAll) shutAll.addEventListener('click', function () { allPanels(false); });

  // the page tree: text filter, stale-only, expand / collapse
  var tree = document.getElementById('tree');
  if (tree) {
    var nodes = Array.prototype.slice.call(tree.querySelectorAll('li'));
    var tq = document.getElementById('treeq');
    var tstale = document.getElementById('tstale');
    var tcount = document.getElementById('tcount');

    function applyTree() {
      var q = tq.value.toLowerCase().trim();
      var staleOnly = tstale.getAttribute('aria-pressed') === 'true';
      if (!q && !staleOnly) {
        nodes.forEach(function (li) { li.hidden = false; });
        tcount.textContent = nodes.length.toLocaleString() + ' nodes';
        return;
      }
      nodes.forEach(function (li) { li.hidden = true; });
      var shown = 0;
      nodes.forEach(function (li) {
        if (q && li.dataset.search.indexOf(q) === -1) return;
        if (staleOnly && li.dataset.stale !== '1') return;
        shown++;
        li.hidden = false;
        var p = li.parentElement;            // reveal the path back to the root
        while (p && p !== tree) {
          if (p.tagName === 'LI') p.hidden = false;
          if (p.tagName === 'DETAILS') p.open = true;
          p = p.parentElement;
        }
      });
      tcount.textContent = shown.toLocaleString() + (shown === 1 ? ' match' : ' matches');
    }

    tq.addEventListener('input', applyTree);
    tstale.addEventListener('click', function () {
      tstale.setAttribute('aria-pressed',
        tstale.getAttribute('aria-pressed') === 'true' ? 'false' : 'true');
      applyTree();
    });
    function treeDetails(open) {
      Array.prototype.slice.call(tree.querySelectorAll('details'))
        .forEach(function (d) { d.open = open; });
    }
    document.getElementById('texpand').addEventListener('click', function () { treeDetails(true); });
    document.getElementById('tcollapse').addEventListener('click', function () { treeDetails(false); });
    applyTree();
  }

  var table = document.getElementById('pages');
  if (!table) return;
  var tbody = table.tBodies[0];
  var rows = Array.prototype.slice.call(tbody.rows);
  var search = document.getElementById('q');
  var count = document.getElementById('count');
  var chips = Array.prototype.slice.call(document.querySelectorAll('button.chip'));

  function apply() {
    var needle = search.value.toLowerCase().trim();
    var active = chips.filter(function (c) { return c.getAttribute('aria-pressed') === 'true'; })
                      .map(function (c) { return c.dataset.flag; });
    var shown = 0;
    rows.forEach(function (row) {
      var okText = !needle || row.dataset.search.indexOf(needle) !== -1;
      var okFlags = active.every(function (f) { return row.dataset[f] === '1'; });
      var show = okText && okFlags;
      row.hidden = !show;
      if (show) shown++;
    });
    count.textContent = shown.toLocaleString() + ' of ' + rows.length.toLocaleString() + ' pages';
  }

  search.addEventListener('input', apply);
  chips.forEach(function (chip) {
    chip.addEventListener('click', function () {
      chip.setAttribute('aria-pressed', chip.getAttribute('aria-pressed') === 'true' ? 'false' : 'true');
      apply();
    });
  });

  Array.prototype.slice.call(table.querySelectorAll('th.sortable')).forEach(function (th) {
    th.addEventListener('click', function () {
      var idx = th.cellIndex;
      var numeric = th.classList.contains('num');
      var dir = th.getAttribute('data-dir') === 'asc' ? 'desc' : 'asc';
      table.querySelectorAll('th[data-dir]').forEach(function (o) { o.removeAttribute('data-dir'); });
      th.setAttribute('data-dir', dir);
      var sign = dir === 'asc' ? 1 : -1;
      rows.sort(function (a, b) {
        var x = a.cells[idx].dataset.v, y = b.cells[idx].dataset.v;
        if (numeric) return sign * ((parseFloat(x) || 0) - (parseFloat(y) || 0));
        return sign * String(x).localeCompare(String(y));
      });
      rows.forEach(function (r) { tbody.appendChild(r); });
    });
  });

  apply();
})();
"""


def esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def _tile(label, value, note=""):
    note_html = f'<div class="note">{esc(note)}</div>' if note else ""
    return (f'<div class="card tile"><div class="label">{esc(label)}</div>'
            f'<div class="value">{esc(value)}</div>{note_html}</div>')


def _bars(rows, unit="pages", ramp=False):
    """rows: [(category, count, tooltip_extra)] - one series, so no legend."""
    if not rows:
        return '<p class="muted">Nothing to show.</p>'
    top = max(r[1] for r in rows) or 1
    total = sum(r[1] for r in rows) or 1
    out = ['<div class="bars">']
    for i, (cat, n, extra) in enumerate(rows):
        pct = 100.0 * n / top
        share = 100.0 * n / total
        cls = f"bar r{min(i + 1, 5)}" if ramp else "bar"
        tip = f"{n:,} {unit} · {share:.0f}% of total"
        if extra:
            tip += f" · {extra}"
        out.append(
            f'<div class="bar-row"><div class="cat" title="{esc(cat)}">{esc(cat)}</div>'
            f'<div class="bar-track"><div class="{cls}" style="width:{pct:.1f}%"></div>'
            f'<span class="tip">{esc(tip)}</span></div>'
            f'<div class="val">{n:,}</div></div>')
    out.append("</div>")
    return "".join(out)


def _table(headers, rows, aligns=None):
    aligns = aligns or [""] * len(headers)
    head = "".join(f'<th class="{"num" if a == "num" else ""}">{esc(h)}</th>'
                   for h, a in zip(headers, aligns))
    body = []
    for row in rows:
        cells = "".join(
            f'<td class="{"num" if a == "num" else ""}">{c}</td>'
            for c, a in zip(row, aligns))
        body.append(f"<tr>{cells}</tr>")
    return (f'<div class="scroll"><table><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def _panel(title, meta, body, open_=False):
    """A collapsible top-level section, so the report opens as an outline."""
    return (f'<details class="panel"{" open" if open_ else ""}>'
            f'<summary><span class="ptitle">{esc(title)}</span>'
            f'<span class="pmeta">{esc(meta)}</span></summary>'
            f'<div class="pbody">{body}</div></details>')


def _node_html(node, depth, stale_days):
    roll = node["roll"]
    page = node.get("page")
    pct = (100.0 * roll["stale"] / roll["pages"]) if roll["pages"] else 0
    title = esc(node["title"])
    if page and page.get("url"):
        title = f'<a href="{esc(page["url"])}">{title}</a>'
    elif not node["fetched"]:
        title = f'{title} <span class="muted" title="not in scope">*</span>'

    own_stale = bool(page and (page["days_since_update"] or 0) >= stale_days)
    search = " ".join(filter(None, [
        node["title"],
        page["updated_by"] if page else "",
        " ".join(page["labels"]) if page else ""])).lower()

    if node["children"]:
        last = roll["updated"].strftime("%Y-%m-%d") if roll["updated"] else "—"
        meta = (f'<span class="nmeta">{roll["pages"]:,} pages · {pct:.0f}% stale '
                f"· {last}</span>")
        kids = "".join(_node_html(c, depth + 1, stale_days) for c in node["children"])
        return (f'<li data-search="{esc(search)}" data-stale="{1 if own_stale else 0}">'
                f'<details{" open" if depth == 0 else ""}>'
                f"<summary>{title} {meta}</summary>"
                f'<ul class="tree-list">{kids}</ul></details></li>')

    if page:
        age = page["days_since_update"]
        badge = ("" if age is None else
                 f'<span class="age {"old" if own_stale else "new"}">{age:,}d</span>')
        who = f'<span class="nmeta">{esc(page["updated_by"])}</span>' if page["updated_by"] else ""
        return (f'<li data-search="{esc(search)}" data-stale="{1 if own_stale else 0}">'
                f"{title} {badge} {who}</li>")
    return f'<li data-search="{esc(search)}" data-stale="0">{title}</li>'


def _tree_html(forest, stale_days):
    body = "".join(_node_html(n, 0, stale_days) for n in forest)
    return (
        '<div class="controls">'
        '<input type="search" id="treeq" placeholder="Find a page or section…">'
        '<button class="chip" id="tstale" aria-pressed="false">Stale only</button>'
        '<button class="chip" id="texpand">Expand all</button>'
        '<button class="chip" id="tcollapse">Collapse all</button>'
        '<span id="tcount"></span></div>'
        f'<ul class="tree-list" id="tree">{body}</ul>')


def write_html_report(path, space, api, stats, pages, stale_days, with_body,
                      window=""):
    t = stats["totals"]
    name = space.get("name", "?")
    key = space.get("key", "")
    desc = (((space.get("description") or {}).get("plain") or {}).get("value") or "").strip()
    current_pages = t["pages"] - t["archived"]
    contributors = len(set(list(stats["top_authors"]) + list(stats["top_editors"])))
    stale_pct = (100.0 * len(stats["stale"]) / len(pages)) if pages else 0
    forest = stats["forest"]

    P = []
    add = P.append
    add('<!doctype html><html lang="en"><head><meta charset="utf-8">')
    add('<meta name="viewport" content="width=device-width, initial-scale=1">')
    add(f"<title>{esc(name)} — Confluence inventory</title>")
    add(f"<style>{HTML_CSS}</style>")
    # set the stamp before the body renders, so a chosen theme never flashes
    add("<script>try{var _m=localStorage.getItem('ci-theme');"
        "if(_m&&_m!=='system')document.documentElement.dataset.theme=_m;}catch(e){}</script>")
    add("</head><body><div class='wrap'>")

    add('<header><div class="row"><div>')
    add(f"<h1>{esc(name)} <span class='muted'>({esc(key)})</span></h1>")
    add(f'<div class="sub">Confluence inventory · generated {NOW:%Y-%m-%d %H:%M UTC} · '
        f"<code>{esc(api.base)}</code></div>")
    if window:
        add(f'<div class="sub" style="margin-top:6px"><strong>Scope:</strong> only '
            f"content {esc(window)} — totals count that window, not the whole space.</div>")
    if desc:
        add(f'<div class="sub" style="margin-top:6px">{esc(desc)}</div>')
    add("</div>")
    add('<div class="themebar" role="group" aria-label="Theme">'
        '<span class="lbl">Theme</span>'
        '<button class="chip" data-theme-set="light" aria-pressed="false">Light</button>'
        '<button class="chip" data-theme-set="dark" aria-pressed="false">Dark</button>'
        '<button class="chip" data-theme-set="system" aria-pressed="true">System</button>'
        "</div>")
    add("</div></header>")

    # Always-visible headline numbers
    add('<section class="grid kpis">')
    add(_tile("Pages", f"{current_pages:,}",
              f"{t['archived']:,} archived · {t['blogposts']:,} blog posts"))
    add(_tile("Sections", f"{len(forest):,}", "top-level branches"))
    add(_tile("Attachments", f"{t['attachments']:,}", human_bytes(t["attachment_bytes"])))
    add(_tile("Contributors", f"{contributors:,}", "created or last-edited a page"))
    add(_tile(f"Stale ({stale_days}d+)", f"{stale_pct:.0f}%",
              f"{len(stats['stale']):,} of {len(pages):,} items"))
    add("</section>")

    add('<div class="allctl"><button class="chip" id="openall">Expand all sections</button>'
        '<button class="chip" id="shutall">Collapse all sections</button></div>')

    # 1. Sections + the tree - the way into a large space
    body = ""
    if forest:
        top = [n for n in forest if n["roll"]["pages"] >= 2][:10]
        body += "<h3>Biggest sections</h3>"
        body += _bars([(n["title"], n["roll"]["pages"],
                        f"{100.0 * n['roll']['stale'] / n['roll']['pages']:.0f}% stale")
                       for n in top])
        body += "<h3>Section rollups</h3>"
        body += _table(["Pages", "Stale", "Last edit", "People", "Section"],
                       [[f"{n['roll']['pages']:,}",
                         f"{100.0 * n['roll']['stale'] / n['roll']['pages']:.0f}%",
                         (n["roll"]["updated"].strftime("%Y-%m-%d")
                          if n["roll"]["updated"] else "—"),
                         f"{len(n['roll']['people'])}", esc(n["title"])]
                        for n in forest[:30] if n["roll"]["pages"] >= 2],
                       ["num", "num", "", "num", ""])
        body += "<h3>Full page tree</h3>"
        body += _tree_html(forest, stale_days)
    else:
        body = '<p class="muted">The space is flat — no page has children.</p>'
    add(_panel("Where the content lives",
               f"{len(forest):,} sections · full tree", body, open_=True))

    # 2. Freshness
    order = ["<=30 days", "31-90 days", "91-365 days", "1-2 years", "2+ years", "unknown"]
    fresh_rows = [(b, stats["freshness"][b], "") for b in order if stats["freshness"].get(b)]
    body = _bars(fresh_rows, "pages", ramp=True)
    if stats["stale"]:
        body += f"<h3>Stalest content — top 25 of {len(stats['stale']):,}</h3>"
        body += _table(["Days", "Title", "Last editor"],
                       [[f"{p['days_since_update']:,}",
                         f'<a href="{esc(p["url"])}">{esc(p["title"])}</a>',
                         esc(p["updated_by"])] for p in stats["stale"][:25]],
                       ["num", "", ""])
    add(_panel("How current is it", f"{stale_pct:.0f}% stale", body))

    # 3. People
    body = ('<div class="grid two"><div><h3>Top page creators</h3>'
            + _bars([(w, n, "") for w, n in stats["top_authors"].most_common(8)])
            + '</div><div><h3>Most recent editors</h3>'
            + _bars([(w, n, "") for w, n in stats["top_editors"].most_common(8)])
            + "</div></div>")
    add(_panel("Who works here", f"{contributors:,} people", body))

    # 4. Structure
    body = ('<div class="grid two"><div><h3>Pages by depth</h3>'
            + _bars([(f"depth {d}" if d else "top level", stats["depth_hist"][d], "")
                     for d in sorted(stats["depth_hist"])])
            + '</div><div><h3>Pages created per year</h3>'
            + _bars([(str(y), stats["created_by_year"][y], "")
                     for y in sorted(stats["created_by_year"])])
            + "</div></div>")
    body += '<div class="grid two" style="margin-top:18px"><div>'
    body += f'<h3>Orphan pages <span class="muted">({len(stats["orphans"]):,})</span></h3>'
    body += ('<ul class="plain">' + "".join(
        f'<li><a href="{esc(p["url"])}">{esc(p["title"])}</a> '
        f'<span class="muted">· {p["days_since_update"]}d · {esc(p["updated_by"])}</span></li>'
        for p in stats["orphans"][:15]) + "</ul>") if stats["orphans"] else \
        '<p class="muted">None — every page has a parent.</p>'
    body += "</div><div>"
    body += f'<h3>Duplicate titles <span class="muted">({len(stats["duplicate_titles"]):,})</span></h3>'
    body += ('<ul class="plain">' + "".join(
        f"<li>{esc(title)} <span class='muted'>· {n} pages</span></li>"
        for title, n in sorted(stats["duplicate_titles"].items(),
                               key=lambda kv: kv[1], reverse=True)[:15]) + "</ul>") \
        if stats["duplicate_titles"] else '<p class="muted">None.</p>'
    body += "</div></div>"
    add(_panel("Shape of the space", f"max depth {stats['max_depth']}", body))

    # 5. Labels and files
    body = ('<div class="grid two"><div>'
            f'<h3>Most used labels <span class="muted">({stats["unlabeled"]:,} unlabeled)</span></h3>'
            + _bars([(l, n, "") for l, n in stats["labels"].most_common(10)])
            + '</div><div><h3>Attachments by type</h3>'
            + _bars([(mt, n, human_bytes(stats["media_bytes"][mt]))
                     for mt, n in stats["media_types"].most_common(8)], unit="files")
            + "</div></div>")
    if stats["largest_attachments"]:
        body += "<h3>Largest files</h3>"
        body += _table(["Size", "File", "On page"],
                       [[human_bytes(a["bytes"]), esc(a["title"]), esc(a["page_title"])]
                        for a in stats["largest_attachments"][:15]], ["num", "", ""])
    if with_body and stats["stub_pages"]:
        body += (f'<h3>Stub pages under 50 words '
                 f'<span class="muted">({len(stats["stub_pages"]):,})</span></h3>')
        body += '<ul class="plain">' + "".join(
            f'<li><a href="{esc(p["url"])}">{esc(p["title"])}</a> '
            f'<span class="muted">· {p["word_count"]} words</span></li>'
            for p in stats["stub_pages"][:20]) + "</ul>"
    add(_panel("Labels and files",
               f"{len(stats['labels']):,} labels · {human_bytes(t['attachment_bytes'])}", body))

    # 6. The flat index, for lookup rather than browsing
    body = ('<div class="controls">'
            '<input type="search" id="q" placeholder="Filter by title, editor, or label…">'
            f'<button class="chip" data-flag="stale" aria-pressed="false">Stale ({stale_days}d+)</button>'
            '<button class="chip" data-flag="orphan" aria-pressed="false">Orphans</button>'
            '<button class="chip" data-flag="unlabeled" aria-pressed="false">Unlabeled</button>'
            '<span id="count"></span></div>')
    cols = [("Title", ""), ("Updated", ""), ("Age (d)", "num"), ("Last editor", ""),
            ("Depth", "num"), ("Children", "num"), ("Files", "num"), ("Comments", "num")]
    if with_body:
        cols.append(("Words", "num"))
    cols.append(("Labels", ""))
    rows = ['<div class="scroll"><table id="pages"><thead><tr>']
    for label, kind in cols:
        rows.append(f'<th class="sortable {kind}">{esc(label)}</th>')
    rows.append("</tr></thead><tbody>")
    orphan_ids = {str(p["id"]) for p in stats["orphans"]}
    for p in sorted(pages, key=lambda p: p["days_since_update"] or 0, reverse=True):
        age = p["days_since_update"] if p["days_since_update"] is not None else ""
        labels = " ".join(f'<span class="tag">{esc(l)}</span>' for l in p["labels"])
        haystack = " ".join([p["title"], p["updated_by"], " ".join(p["labels"])]).lower()
        rows.append(
            f'<tr data-stale="{1 if (p["days_since_update"] or 0) >= stale_days else 0}" '
            f'data-orphan="{1 if str(p["id"]) in orphan_ids else 0}" '
            f'data-unlabeled="{0 if p["labels"] else 1}" data-search="{esc(haystack)}">')
        rows.append(f'<td data-v="{esc(p["title"].lower())}">'
                    f'<a href="{esc(p["url"])}">{esc(p["title"])}</a></td>')
        rows.append(f'<td data-v="{esc(iso(p["updated"]))}">'
                    f'{esc(p["updated"].strftime("%Y-%m-%d") if p["updated"] else "?")}</td>')
        rows.append(f'<td class="num" data-v="{age or 0}">'
                    f'{age if age == "" else format(age, ",")}</td>')
        rows.append(f'<td data-v="{esc(p["updated_by"].lower())}">{esc(p["updated_by"])}</td>')
        rows.append(f'<td class="num" data-v="{p["depth"]}">{p["depth"]}</td>')
        rows.append(f'<td class="num" data-v="{p["child_count"]}">{p["child_count"] or ""}</td>')
        rows.append(f'<td class="num" data-v="{p["attachment_count"]}">'
                    f'{p["attachment_count"] or ""}</td>')
        rows.append(f'<td class="num" data-v="{p["comment_count"]}">'
                    f'{p["comment_count"] or ""}</td>')
        if with_body:
            wc = p["word_count"] or 0
            rows.append(f'<td class="num" data-v="{wc}">{wc:,}</td>')
        rows.append(f'<td data-v="{esc(",".join(p["labels"]))}">{labels}</td></tr>')
    rows.append("</tbody></table></div>")
    add(_panel("Every page (flat index)", f"{len(pages):,} rows · sortable",
               body + "".join(rows)))

    add(f"<script>{HTML_JS}</script>")
    add("</div></body></html>")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(P))


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


def load_inventory(path):
    """Rehydrate a previous `inventory` run so terminal views cost no API calls."""
    target = os.path.join(path, "inventory.json") if os.path.isdir(path) else path
    if not os.path.exists(target):
        raise ConfluenceError(
            f"No inventory.json at {target}.\n"
            "  Point --from at an inventory-<KEY>-<date>/ directory."
        )
    with open(target, encoding="utf-8") as fh:
        data = json.load(fh)
    pages = data.get("pages") or []
    if pages and "ancestor_chain" not in pages[0]:
        raise ConfluenceError(
            f"{target} predates section support — re-run `inventory` to refresh it."
        )
    for p in pages:
        p["created"] = parse_ts(p.get("created"))
        p["updated"] = parse_ts(p.get("updated"))
        p["ancestor_chain"] = [tuple(a) for a in (p.get("ancestor_chain") or [])]
    return data


def cmd_tree(args):
    if args.from_dir:
        data = load_inventory(args.from_dir)
        space, pages = data.get("space") or {}, data["pages"]
        window = ""
        print(f"\nFrom {args.from_dir} (snapshot of {data.get('generated', '?')[:16]}) "
              "— no API calls")
    else:
        since, created_since = cql_date(args.since), cql_date(args.created_since)
        window = describe_window(args.since, args.created_since)
        api = connect(args)
        space = fetch_space(api, resolve_space(args))
        raw = fetch_content(api, space.get("key"), "page", progress="pages",
                            since=since, created_since=created_since)
        pages = [shape_page(api, p) for p in raw]
    key = space.get("key")
    if not pages:
        print("No pages matched.")
        return 0

    forest = forest_stats(pages, args.stale_days)
    shown = [n for n in forest if n["roll"]["pages"] >= args.min_pages]

    print(f"\n{space.get('name')} [{key}] — {len(pages):,} pages in "
          f"{len(forest):,} top-level sections")
    if window:
        print(f"Scope: content {window}")
    if len(shown) < len(forest):
        print(f"Showing {len(shown)} sections with {args.min_pages}+ pages "
              f"(--min-pages 1 for all)")
    print()
    print(f"{'PAGES':>6}  {'STALE':>5}  {'LAST EDIT':<10}  {'WHO':>3}  SECTION")
    print(f"{'-' * 6}  {'-' * 5}  {'-' * 10}  {'-' * 3}  {'-' * 46}")

    for node, depth, _last in walk_forest(shown, args.depth):
        roll = node["roll"]
        stale_pct = (100.0 * roll["stale"] / roll["pages"]) if roll["pages"] else 0
        last = roll["updated"].strftime("%Y-%m-%d") if roll["updated"] else "—"
        prefix = "   " * depth + ("└─ " if depth else "")
        title = node["title"] if node["fetched"] else node["title"] + " *"
        print(f"{roll['pages']:>6,}  {stale_pct:>4.0f}%  {last:<10}  "
              f"{len(roll['people']):>3}  {prefix}{title[:60]}")

    blogs = [p for p in pages if p["type"] == "blogpost"]
    if blogs:
        print(f"\n  plus {len(blogs):,} blog posts (not in the page tree)")
    print(f"\n* = section header not itself in scope; counts still include it")
    print(f"Depth {args.depth} — use --depth 3 to go deeper, "
          f"--min-pages 1 to show every section.")
    return 0


def cmd_pages(args):
    if args.from_dir:
        data = load_inventory(args.from_dir)
        pages = [p for p in data["pages"] if p["type"] == "page"]
        if args.limit:
            pages = pages[:args.limit]
        print(f"\nFrom {args.from_dir} (snapshot of {data.get('generated', '?')[:16]}) "
              "— no API calls", file=sys.stderr)
    else:
        since, created_since = cql_date(args.since), cql_date(args.created_since)
        api = connect(args)
        key = fetch_space(api, resolve_space(args)).get("key")
        raw = fetch_content(api, key, "page", include_archived=args.include_archived,
                            cap=args.limit, progress="pages",
                            since=since, created_since=created_since)
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
    since, created_since = cql_date(args.since), cql_date(args.created_since)
    window = describe_window(args.since, args.created_since)
    api = connect(args)
    print(f"Auth:  {api.auth_mode}")
    space = fetch_space(api, resolve_space(args))
    key = space.get("key")
    out_dir = args.out_dir or f"inventory-{key}-{NOW:%Y-%m-%d}"
    os.makedirs(out_dir, exist_ok=True)
    print(f"Space: {key} @ {api.base}")
    print(f"       {space.get('name')} (type={space.get('type')})")

    if window:
        print(f"Scope: {window}")

    raw_pages = []
    for ctype in ("page", "blogpost"):
        got = fetch_content(api, key, ctype, args.with_body, args.with_restrictions,
                            args.include_archived, progress=f"{ctype}s",
                            since=since, created_since=created_since)
        print(f"  {ctype}s: {len(got):,}")
        raw_pages += got
    pages = [shape_page(api, p) for p in raw_pages]

    attachments = []
    if not args.skip_attachments:
        attachments = [shape_attachment(api, a)
                       for a in fetch_attachments(api, key, [p["id"] for p in pages],
                                                  since, created_since)]
        print(f"  attachments: {len(attachments):,}")

    comments = []
    if not args.skip_comments:
        try:
            comments = [shape_comment(api, c) for c in
                        fetch_by_cql(api, build_cql(key, "comment", since, created_since),
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
                            "biggest_pages", "stub_pages", "busiest_pages",
                            "forest")}
    with open(os.path.join(out_dir, "inventory.json"), "w", encoding="utf-8") as fh:
        json.dump({"generated": NOW.isoformat(), "base_url": api.base,
                   "space": {k: space.get(k) for k in ("id", "key", "name", "type", "status")},
                   "summary": summary, "pages": pages,
                   "attachments": attachments, "comments": comments},
                  fh, indent=2, default=jsonable)

    written = ["pages.csv", "inventory.json"]
    if attachments:
        written.append("attachments.csv")
    if comments:
        written.append("comments.csv")

    md_path = os.path.join(out_dir, "report.md")
    if args.format in ("md", "both") or args.print_report:
        write_report(md_path, space, api, stats, args.stale_days, args.with_body, window)
        written.insert(0, "report.md")
    if args.format in ("html", "both"):
        write_html_report(os.path.join(out_dir, "report.html"), space, api, stats,
                          pages, args.stale_days, args.with_body, window)
        written.insert(0, "report.html")

    t = stats["totals"]
    print()
    print(f"{t['pages']:,} pages, {t['blogposts']:,} blog posts, "
          f"{t['attachments']:,} attachments ({human_bytes(t['attachment_bytes'])}), "
          f"{t['comments']:,} comments")
    print(f"{len(stats['stale']):,} items untouched for {args.stale_days}+ days; "
          f"{stats['unlabeled']:,} unlabeled; {len(stats['orphans']):,} orphans")
    print(f"\nWrote {out_dir}/ -> {', '.join(written)}")
    if args.format in ("html", "both"):
        print(f"Open it with:  xdg-open {os.path.join(out_dir, 'report.html')}")

    if args.print_report:
        print("\n" + "=" * 72 + "\n")
        with open(md_path, encoding="utf-8") as fh:
            print(fh.read())
    return 0


# --------------------------------------------------------------------------

def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--env-file", help="path to a .env file (default: ./.env, then script dir)")
    common.add_argument("--base-url", help="overrides CONFLUENCE_BASE_URL")
    common.add_argument("--token", help="overrides CONFLUENCE_TOKEN")
    common.add_argument("--email", help="Cloud only: overrides CONFLUENCE_EMAIL (Basic auth)")
    common.add_argument("--insecure", action="store_true", help="skip TLS verification")
    common.add_argument("--rate", type=float, metavar="N",
                        help="cap at N requests/second (default: no artificial delay)")
    common.add_argument("--max-calls", type=int, metavar="N",
                        help="abort once the run has made N API calls")

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

    p = sub.add_parser("tree", parents=[common],
                       help="page hierarchy with per-section rollups "
                            "(add --from to reuse an inventory)")
    p.add_argument("space", nargs="?", help="space key (default: CONFLUENCE_SPACE)")
    p.add_argument("--depth", type=int, default=2, help="levels to show (default 2)")
    p.add_argument("--min-pages", type=int, default=3,
                   help="hide sections smaller than this (default 3)")
    p.add_argument("--stale-days", type=int, default=365)
    p.add_argument("--since", metavar="DATE",
                   help="only content edited since DATE (YYYY-MM-DD, or 90d/12w/6m/2y)")
    p.add_argument("--created-since", metavar="DATE",
                   help="only content created since DATE (same formats)")
    p.add_argument("--from", dest="from_dir", metavar="DIR",
                   help="read a previous inventory's directory instead of the API "
                        "(no calls)")
    p.set_defaults(func=cmd_tree)

    p = sub.add_parser("pages", parents=[common], help="list pages in a space")
    p.add_argument("space", nargs="?", help="space key (default: CONFLUENCE_SPACE)")
    p.add_argument("--sort", choices=["updated", "created", "title", "depth"],
                   default="updated")
    p.add_argument("--limit", type=int, help="stop after N pages")
    p.add_argument("--since", metavar="DATE",
                   help="only content edited since DATE (YYYY-MM-DD, or 90d/12w/6m/2y)")
    p.add_argument("--created-since", metavar="DATE",
                   help="only content created since DATE (same formats)")
    p.add_argument("--include-archived", action="store_true")
    p.add_argument("--csv", help="also write full page metadata to this CSV path")
    p.add_argument("--from", dest="from_dir", metavar="DIR",
                   help="read a previous inventory's directory instead of the API "
                        "(no calls)")
    p.set_defaults(func=cmd_pages)

    p = sub.add_parser("inventory", parents=[common],
                       help="full inventory: report.md + CSVs + JSON")
    p.add_argument("space", nargs="?", help="space key (default: CONFLUENCE_SPACE)")
    p.add_argument("--out-dir", help="default: ./inventory-<SPACE>-<YYYY-MM-DD>")
    p.add_argument("--stale-days", type=int, default=365,
                   help="flag content untouched this long (default 365)")
    p.add_argument("--format", choices=["md", "html", "both"], default="both",
                   help="which report(s) to write (default both)")
    p.add_argument("--print", dest="print_report", action="store_true",
                   help="also print the markdown report to the terminal")
    p.add_argument("--with-body", action="store_true",
                   help="fetch bodies for word counts / stub detection (slower)")
    p.add_argument("--with-restrictions", action="store_true",
                   help="expand read restrictions per page (slower)")
    p.add_argument("--since", metavar="DATE",
                   help="only content edited since DATE (YYYY-MM-DD, or 90d/12w/6m/2y)")
    p.add_argument("--created-since", metavar="DATE",
                   help="only content created since DATE (same formats)")
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
    try:
        return args.func(args)
    finally:
        # always report the cost, including on an error or Ctrl-C
        spent = sum(api.calls for api in SESSIONS)
        if spent:
            sys.stdout.flush()   # keep the counter after the command's output
            print(f"[{spent} API call{'' if spent == 1 else 's'}]", file=sys.stderr)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ConfluenceError as exc:
        sys.exit(f"\nError: {exc}")
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
