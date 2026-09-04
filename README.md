# Confluence inventory

A small CLI for taking stock of a client-hosted Confluence (Data Center /
Server) instance with a Personal Access Token: what spaces exist, what's in a
given space, who owns it, and how stale it is.

Read-only — it never writes to Confluence. Everything it produces lands in
local files.

## Setup

```bash
python3 -m pip install -r requirements.txt
cp .env.example .env
$EDITOR .env          # base URL + PAT
./confluence.py check
```

`.env` is gitignored. Don't commit a real token.

### Getting a PAT

In Confluence: avatar (top right) → **Settings** → **Personal Access Tokens** →
**Create token**. The token inherits your own permissions, so the inventory
only covers what your account can already see — worth remembering when a page
count looks low.

### Configuration

Everything comes from `.env`, and every value can be overridden by a real
environment variable or a command-line flag (flag > environment > `.env`).

| Variable | Meaning |
| --- | --- |
| `CONFLUENCE_BASE_URL` | e.g. `https://confluence.client.com`, including any context path like `/confluence` |
| `CONFLUENCE_TOKEN` | the PAT, sent as `Authorization: Bearer` |
| `CONFLUENCE_SPACE` | optional default space key, so you can omit it on the command line |
| `CONFLUENCE_VERIFY` | path to a CA bundle for a private CA, or `false` to skip TLS verification |
| `CONFLUENCE_EMAIL` | **Cloud only.** Setting it switches to Basic auth. Leave unset for client-hosted. |
| `CONFLUENCE_RATE` | cap requests per second, e.g. `5`. Unset means no artificial delay |
| `CONFLUENCE_MAX_CALLS` | abort a run once it has made this many API calls |

Point at a different client with `--env-file ../other-client/.env`.

## Commands

Ordered smallest to largest — start at the top and work down.

### `check` — is the connection working?

```bash
./confluence.py check
```

Confirms the URL and token, prints who Confluence thinks you are, and counts
the spaces visible to you. Two API calls. Run this first; if the token is being
sent the wrong way the error tells you which way to flip it.

### `spaces` — what's on this instance?

```bash
./confluence.py spaces                          # global spaces
./confluence.py spaces --type all               # include personal spaces
./confluence.py spaces --contains ops           # filter by key or name
./confluence.py spaces --counts                 # add page + attachment counts
./confluence.py spaces --csv spaces.csv         # also write a CSV
```

`--counts` adds two API calls per space, so it's noticeably slower on a large
instance — worth it once, to find where the content actually lives.

### `space KEY` — quick look at one space

```bash
./confluence.py space ENG
```

Name, description, homepage, counts of pages / blog posts / attachments /
comments, and the ten most recently edited pages. A handful of calls regardless
of space size — cheap enough to run against several spaces while you decide
which one to inventory properly.

### `pages KEY` — list the pages

```bash
./confluence.py pages ENG                       # newest edits first
./confluence.py pages ENG --sort depth          # tree order, indented
./confluence.py pages ENG --limit 50
./confluence.py pages ENG --csv eng-pages.csv   # full metadata to CSV
```

Prints last-edit date, age in days, title, last editor, and labels. Sort by
`updated`, `created`, `title`, or `depth`.

### `inventory KEY` — the full picture

```bash
./confluence.py inventory ENG
./confluence.py inventory ENG --with-body --stale-days 180
```

Walks every page, blog post, attachment, and comment in the space and writes
into `inventory-<KEY>-<date>/`:

| File | Contents |
| --- | --- |
| `report.md` | the readable summary (see below) |
| `report.html` | the same summary as a self-contained page, plus a filterable index of every page |
| `pages.csv` | one row per page: title, URL, created/updated + who, version count, depth, parent, child count, labels, attachment count and bytes, comment count, restrictions |
| `attachments.csv` | one row per file: name, media type, size, owner, parent page |
| `comments.csv` | one row per comment |
| `inventory.json` | all of the above plus the computed summary, for further scripting |

By default you get both `report.md` and `report.html`; use `--format md` or
`--format html` for just one. Add `--print` to dump the markdown report to the
terminal as well, so a whole space can be reviewed without leaving the shell.

`report.html` is a single self-contained file — no network access, no CDN, safe
to email or drop on a share. It leads with the headline numbers, charts
freshness / depth / creation-per-year / contributors / labels / file types, then
ends with a **filterable, sortable index of every page**: type in the box to
search titles, editors, and labels, click a column to sort, or toggle the Stale
/ Orphans / Unlabeled chips. That index is usually the fastest way to get your
bearings in an unfamiliar space.

`report.md` covers:

- **Totals** — pages, archived pages, blog posts, attachments and their total
  size, comments, restricted pages, label count
- **Freshness** — pages bucketed by last edit (≤30d / 31–90d / 91–365d / 1–2y /
  2+y), then the 25 stalest with their last editor
- **People** — top page creators alongside the most active recent editors, which
  is usually how you find who to ask about a space
- **Structure** — nesting depth histogram, leaf pages, orphan pages with no
  parent, duplicate titles
- **Labels** — the 25 most used, plus how many pages have none
- **Attachments** — breakdown by media type and the 20 largest files
- **Page size** (with `--with-body`) — longest pages and stubs under 50 words
- **Most discussed pages** — by comment count

Useful flags:

| Flag | Effect |
| --- | --- |
| `--with-body` | fetch page bodies for word counts and stub detection (slower, much more data) |
| `--stale-days N` | staleness threshold, default 365 |
| `--with-restrictions` | record per-page view restrictions |
| `--include-archived` | include archived pages |
| `--skip-attachments`, `--skip-comments` | skip those passes on a big space |
| `--since DATE` | only content edited since DATE |
| `--created-since DATE` | only content created since DATE |
| `--format md\|html\|both` | which report to write, default both |
| `--print` | echo the markdown report to the terminal |

### Limiting to recent content

On a space with years of history, most of it isn't worth reading. `--since`
scopes the whole run to content edited after a date, and `--created-since` to
content created after one. Both take `YYYY-MM-DD` or a relative window —
`90d`, `12w`, `6m`, `2y`:

```bash
./confluence.py inventory ABC --since 12m          # edited in the last year
./confluence.py inventory ABC --since 2025-01-01   # edited since a fixed date
./confluence.py pages ABC --since 90d              # quick look at active pages
```

The filter is applied by the server via CQL, so it cuts the API calls and the
time roughly in proportion — on a 6,500-page space, `--since 12m` took the
inventory from 127 calls to 83.

Both reports state the window at the top, because every total below it then
counts that window rather than the whole space. Two things to know:

- Attachments and comments are filtered by the same window, so per-page file
  and comment counts mean "recent ones", not all of them.
- A date filter runs through search, which covers current content only —
  `--include-archived` has no effect alongside it.

## How much load does this put on the instance?

Very little. Every request is a read-only `GET`, they run one at a time (never
concurrently), and pagination pulls 100 items per call. Measured call counts:

| Command | API calls | On a big instance |
| --- | --- | --- |
| `check` | 2 | 2 |
| `spaces` | 1 per 100 spaces | 3 for 250 spaces |
| `spaces --counts` | + 2 per space | 500 extra for 250 spaces — the one command worth thinking about |
| `space KEY` | ~6, regardless of size | 6 |
| `pages KEY` | 1 + 1 per 100 pages | 21 for 2,000 pages |
| `inventory KEY` | 1 + 1 per 100 of each of pages, blog posts, attachments, comments | 127 for 6,500 pages / 4,200 files / 1,800 comments |
| `inventory KEY --since 12m` | the same, over the filtered set | 83 for that same space |

So a full inventory of a large space is a few dozen requests — comparable to one
person clicking around the UI for a minute, and far less than the site's own
search indexer. The tool also backs off and retries on `429` and `5xx`, so if an
admin has rate limiting in place it cooperates rather than hammering.

Every run prints what it actually cost, to stderr, so you can quote a real
number if anyone asks:

```
$ ./confluence.py inventory ABC
...
[57 API calls]
```

### Throttling

Off by default, because sequential reads at this volume don't need it. Two flags
when you want a guarantee — before a first run on an unfamiliar production
instance, or when an ops team asks for a number:

```bash
./confluence.py inventory ABC --rate 5        # at most 5 requests/second
./confluence.py inventory ABC --max-calls 500 # hard stop, in case a space is huge
```

Set `CONFLUENCE_RATE` in a client's `.env` to make the cap permanent for that
engagement. `--max-calls` aborts with a clear message rather than silently
truncating, so a partial inventory never looks like a complete one.

Two caveats worth knowing:

- `--with-body` doesn't change the call count, but each response now carries the
  full storage-format body of every page. That's a much bigger payload, so run it
  when you want word counts, not by default.
- `spaces --counts` is the only command whose cost scales with the number of
  spaces rather than pages. Narrow it with `--contains` on a large instance.

### Checking a page count from the Confluence UI

To sanity-check the tool's numbers without running anything, use the site search
restricted to one space — every Confluence version can do this:

```
https://confluence.example.com/dosearchsite.action?cql=space%3D%22ABC%22%20and%20type%3Dpage
```

The results header gives the total. The same thing is reachable by hand through
**Search → Advanced**, filtering by space and by type *Page*. Note that search
is index-backed, so the count reflects what your account can see, excludes
archived pages by default, and can lag a reindex — where the REST count reads
live content. A gap between the two is usually one of those three things.

## Notes

- Runs on Python 3.7+.
- Uses the v1 REST API (`/rest/api/...`), which is present on both Data Center
  and Cloud, so the same script works if a client migrates.
- Space keys are case-sensitive in the API. If the by-key lookup 404s, the tool
  searches the space listing for a case-insensitive key or name match and tells
  you what it used — so `space eng` and `space Engineering` both find `ENG`.
  When nothing matches it prints close matches; `spaces --type all` and
  `spaces --contains <text>` show what your account can actually see.
- Retries 429 and 5xx responses with backoff, honoring `Retry-After`.
- Pagination follows the server's `next` link, so it handles both `start`-based
  and cursor-based paging.
- Attachments are found via a CQL search; if the instance rejects that, it falls
  back to per-page lookups automatically.
- A first `inventory` run on an unfamiliar space is the slow one. If it's taking
  too long, `--skip-comments --skip-attachments` gets you the page inventory
  quickly, then run again without them.
