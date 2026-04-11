# Personal Newsletter Viewer

A local-only newsletter reader that pulls emails from Outlook.com via Microsoft Graph API, stores them in SQLite, and serves a clean browser UI for browsing by date and category.

No cloud services, no deployment — runs entirely on your machine.

---

## Architecture

```
Microsoft Graph API (graph.microsoft.com)
        │
        ▼  python ingest.py
  newsletters.db  (SQLite)
        │
        ▼  uvicorn app:app
  FastAPI local server  →  http://localhost:8000
        │
        ▼  Jinja2 templates
  Browser UI
```

### File Structure

```
personal_newsfeed/
├── app.py            # FastAPI routes + template rendering
├── ingest.py         # Graph API fetch + SQLite upsert
├── ms_auth.py        # MSAL OAuth2 device-code auth + token cache
├── db.py             # SQLite schema, queries, sync state
├── categorize.py     # Sender domain → category logic
├── config.yaml       # User-editable category rules
├── .env              # App credentials (not committed)
├── templates/
│   ├── base.html     # Shared layout: sidebar, sync button
│   ├── index.html    # Homepage: recent newsletters by category
│   ├── category.html # Filtered list + pagination
│   └── newsletter.html  # Full HTML in sandboxed iframe
└── static/
    └── style.css
```

### Component Responsibilities

| File | Role |
|------|------|
| `ingest.py` | Connects to Microsoft Graph API, fetches messages with delta sync (subject, sender, date, HTML/text body), categorizes via `categorize.py`, upserts into `newsletters.db`. Stores delta link for incremental syncs. |
| `ms_auth.py` | Handles MSAL OAuth2 device-code flow. Caches tokens in `.token_cache.json` so re-authentication is only needed when the refresh token expires. |
| `db.py` | Owns the SQLite schema and all queries. Single `newsletters` table + `sync_state` table for incremental sync. |
| `categorize.py` | Matches sender email against rules in `config.yaml` (priority order). Falls back to subject keyword scan. |
| `config.yaml` | Human-editable rules mapping sender domains/substrings to categories. |
| `app.py` | FastAPI app. Serves the browser UI and a `/sync` endpoint that shells out to `ingest.py`. |
| `templates/` | Jinja2 HTML templates. Newsletter HTML is rendered in a sandboxed `<iframe>` via the `/raw/<id>` route to isolate email CSS from the app. |

---

## Setup

### 1. Install dependencies

```bash
uv sync
```

### 2. Register an Azure application

The app uses the Microsoft Graph API with OAuth2 device-code flow — no client secret required.

1. Go to [portal.azure.com](https://portal.azure.com) → **Azure Active Directory** → **App registrations** → **New registration**
2. Name: anything (e.g. `personal-box`)
3. Supported account types: **Personal Microsoft accounts only**
4. Redirect URI: leave blank
5. Click **Register**
6. Copy the **Application (client) ID** — you'll need it in `.env`
7. Go to **API permissions** → **Add a permission** → **Microsoft Graph** → **Delegated** → add `Mail.Read`
8. Go to **Authentication** → enable **Allow public client flows** (toggle on)

### 3. Configure credentials

Edit `.env`:

```env
MAIL_FOLDER=Newsletters          # or INBOX, or any folder name
GRAPH_CLIENT_ID=<your-client-id-here>
OPENAI_API_KEY=<your-openai-key>
```

### 4. Authenticate (first run)

Run the test command to trigger the device-code flow:

```bash
uv run python ingest.py --test
```

You'll see a message like:
```
To sign in, use a web browser to open the page https://microsoft.com/devicelogin
and enter the code XXXXXXXX to authenticate.
```

Open the URL, enter the code, and sign in with your Microsoft account. The token is cached to `.token_cache.json` — subsequent runs won't require re-authentication until the refresh token expires (~90 days of inactivity).

### 5. Tune categories

Edit `config.yaml` to match your actual subscriptions. Rules are matched against the sender's full email address (case-insensitive substring match), in the order listed. First match wins.

```yaml
categories:
  AI:
    - therundown.ai
    - bensbites.co
  Tech:
    - tldr.tech
    - hackernewsletter.com
```

---

## Usage

### Sync emails

```bash
# Incremental sync (only new messages since last run)
uv run python ingest.py

# Re-fetch everything in the folder
uv run python ingest.py --full

# Test auth and folder access
uv run python ingest.py --test
```

### Start the web UI

```bash
uv run uvicorn app:app --reload --port 8765
```

Open [http://localhost:8000](http://localhost:8000).

### Debug categorization

```bash
uv run python categorize.py --debug
```

Prints a table comparing stored categories vs. what the current rules would assign — useful for tuning `config.yaml`.

---

## Routes

| Route | Description |
|-------|-------------|
| `GET /` | Recent newsletters (last 30 days), grouped by category |
| `GET /category/{name}` | All newsletters in a category, newest first |
| `GET /newsletter/{id}` | Full newsletter view (renders HTML in iframe) |
| `GET /raw/{id}` | Raw HTML body — used as the iframe `src` |
| `POST /sync` | Trigger incremental sync, returns JSON status |
| `GET /api/newsletters` | JSON endpoint with `category`, `start`, `end`, `page` filters |
| `GET /docs` | Auto-generated FastAPI API docs |

---

## Design Decisions

**Microsoft Graph API over IMAP** — Outlook.com has deprecated Basic Auth for IMAP on personal accounts. The Graph API with OAuth2 device-code flow works without a client secret and supports delta sync (only fetching changed messages), which is more efficient than UID-based IMAP polling.

**SQLite** — Zero infrastructure. Fast enough for thousands of newsletters. Enables date/category filtering without re-parsing raw email on every request.

**iframe rendering** — Newsletter HTML is rendered in a sandboxed iframe (`/raw/<id>`) so email CSS doesn't bleed into the app's styles, and vice versa.

**`config.yaml` for categories** — Rules live outside the code and can be tuned without touching Python. Run `python categorize.py --debug` to see the effect of changes against existing emails.

**Delta sync** — `ingest.py` stores the Graph API delta link in `sync_state` so repeated syncs only fetch new/changed messages. If the delta link expires (HTTP 410), it automatically falls back to a full sync.

**Subprocess-safe auth** — When `app.py` triggers sync via `subprocess.run(..., capture_output=True)`, `sys.stdin.isatty()` returns false, so the device-code flow is suppressed and a clean error is returned to the UI instead of hanging.
