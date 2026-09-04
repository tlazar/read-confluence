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
| `pages.csv` | one row per page: title, URL, created/updated + who, version count, depth, parent, child count, labels, attachment count and bytes, comment count, restrictions |
| `attachments.csv` | one row per file: name, media type, size, owner, parent page |
| `comments.csv` | one row per comment |
| `inventory.json` | all of the above plus the computed summary, for further scripting |

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

## Notes

- Uses the v1 REST API (`/rest/api/...`), which is present on both Data Center
  and Cloud, so the same script works if a client migrates.
- Retries 429 and 5xx responses with backoff, honoring `Retry-After`.
- Pagination follows the server's `next` link, so it handles both `start`-based
  and cursor-based paging.
- Attachments are found via a CQL search; if the instance rejects that, it falls
  back to per-page lookups automatically.
- A first `inventory` run on an unfamiliar space is the slow one. If it's taking
  too long, `--skip-comments --skip-attachments` gets you the page inventory
  quickly, then run again without them.
