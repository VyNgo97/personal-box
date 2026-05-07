# personal-box: Full App Breakdown

This is a **local newsletter reader**. It pulls emails from your Microsoft Outlook account via an API, uses AI to categorize them, stores them in a local database, and serves them through a web interface you open in your browser. Nothing is hosted — it runs entirely on your machine.

---

## Big Picture: How the Pieces Fit Together

```
Microsoft Outlook (your email)
        │
        ▼
   ms_auth.py  ←── authenticates you
        │
        ▼
   ingest.py   ←── fetches emails, maps them into records
        │
        ▼
  categorize.py ←── asks GPT to assign a category to each email
        │
        ▼
     db.py      ←── saves everything to a local SQLite file
        │
        ▼
     app.py     ←── FastAPI web server — reads from DB, renders HTML
        │
        ▼
  Your browser at localhost:8765
```

There are two separate processes:
1. **The web server** (`app.py`) — always running, serves the UI
2. **The ingest script** (`ingest.py`) — runs on demand (or when you click "Sync Now"), fetches new mail

---

## File-by-File Walkthrough

### `ms_auth.py` — Authentication

This file handles proving to Microsoft that you are who you say you are, so the app can read your mail.

**The flow used here is called the Device Code Flow.** This is an OAuth 2.0 pattern designed for apps that don't have a browser popup (like a CLI tool). Here's how it works:

1. Your app tells Microsoft "I want to read mail for a user"
2. Microsoft responds with a short code like `ABCD-1234` and a URL
3. You open that URL in your browser, enter the code, and log in with your Microsoft account
4. Microsoft gives the app an **access token** — a time-limited credential (like a hall pass) that proves you authorized it

The library doing this heavy lifting is **MSAL** (Microsoft Authentication Library). You don't have to implement OAuth yourself — MSAL handles the protocol.

**Token caching** is the other key concept here. Access tokens expire (usually after ~1 hour). Rather than making you log in every time, the app saves the token to `.token_cache.json`. On the next run, it reads that file and silently refreshes the token without any user interaction. That's what `_load_cache` and `_save_cache` do.

```python
accounts = app.get_accounts()
if accounts:
    result = app.acquire_token_silent(SCOPES, account=accounts[0])  # tries cache first
```

If there's no cached token and `allow_device_flow=False` (which is what happens when the web server triggers a sync — it can't show you a terminal prompt), it raises a `RuntimeError` instead of hanging. That's an intentional safety valve.

---

### `ingest.py` — Fetching Emails

This script talks to the **Microsoft Graph API** — Microsoft's unified REST API for accessing Outlook, OneDrive, Teams, and more. It fetches emails from a specific mail folder and saves them to the database.

**Key concepts:**

**REST API calls with `requests`**

Every call follows the same pattern: build a URL, attach an `Authorization: Bearer <token>` header, send the request, check for errors with `raise_for_status()`. The `_headers()` helper just keeps that boilerplate in one place.

**Delta sync**

This is the most interesting engineering decision here. Naively, you could fetch all your emails every time you sync — but that's slow and wasteful if you have thousands of emails. Instead, the Microsoft Graph API supports **delta queries**.

How it works:
- First sync: fetch everything, and Microsoft gives you back a `deltaLink` URL
- Every subsequent sync: call that `deltaLink` URL — Microsoft only returns what changed since last time
- The delta link is saved in the database (in the `sync_state` table) and reused next time

```python
delta_url = None if full else get_sync_state("delta_link")
messages, new_delta_link = _run_delta(token, folder_id, delta_url)
# after processing...
set_sync_state("delta_link", new_delta_link)
```

If the delta link expires (HTTP 410 Gone), the app falls back to a full sync automatically.

**Tombstones**

When a message is deleted in Outlook, the delta API returns it with `"@removed"` in the response instead of the message data. The `_map_message` function returns `None` for these, and they're counted as "tombstones." This prevents crashes when the API signals a deletion.

**Running as subprocess detection**

```python
def _running_as_subprocess() -> bool:
    return not sys.stdin.isatty()
```

When the web server triggers a sync via `subprocess.run(...)`, there's no interactive terminal attached. This check detects that situation so the app knows not to attempt the device code flow (which would need user input at a terminal). Clever low-tech solution.

---

### `categorize.py` — AI Classification

Each email needs a category (Technology, Business, etc.) so you can filter them in the UI. This file asks GPT to assign one.

**Structured outputs with Pydantic**

Rather than asking GPT to return free text and then parsing it, the app uses OpenAI's **structured output** feature. You define a Pydantic model:

```python
class CategoryMapping(BaseModel):
    category: str
```

And pass it as `response_format`. OpenAI guarantees the response matches that schema — no string parsing, no `if "technology" in response.lower()` hacks.

**Prompt templating with Jinja2**

The prompt lives in `prompts/category_mappings.md` as a Jinja2 template:

```
- Sender name: {{ sender_name }}
- Sender email: {{ sender_email }}
- Subject: {{ subject }}
```

At runtime, `_get_template()` loads it and renders it with the actual values. Keeping the prompt in a separate file (rather than hardcoding it as a Python string) makes it easy to edit without touching code.

**Lazy loading with a global**

```python
_prompt_template: Template | None = None

def _get_template() -> Template:
    global _prompt_template
    if _prompt_template is None:
        _prompt_template = Template(path.read_text())
    return _prompt_template
```

The template is only read from disk once — the first time it's needed — and then cached in a module-level variable. This pattern is called **lazy initialization**. It avoids reading the file on every single email.

---

### `db.py` — Data Storage

This is where data lives. It uses **SQLite**, a file-based database that requires zero setup — no server, no config, just a `.db` file on disk.

**The `newsletters` table** stores one row per email:

| Column | What it holds |
|---|---|
| `id` | Microsoft's unique message ID (primary key) |
| `subject` | Email subject line |
| `sender_name` / `sender_email` | Who sent it |
| `date` | When it was received (ISO 8601 string) |
| `category` | What GPT assigned (e.g., "Technology") |
| `html_body` / `text_body` | The email content |
| `is_read` | 0 or 1 — whether you've opened it |
| `folder` | Which mail folder it came from |

**Context manager for connections**

```python
@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
```

A context manager (used with `with`) ensures the connection is always closed and changes are always committed, even if something crashes mid-function. `sqlite3.Row` makes rows behave like dictionaries so you can do `row["subject"]` instead of `row[1]`.

**Upsert (INSERT OR UPDATE)**

```sql
INSERT INTO newsletters (...) VALUES (...)
ON CONFLICT(id) DO UPDATE SET ...
```

"Upsert" means: insert the row if it doesn't exist, update it if it does. This is important because the delta sync might return an email you already have (e.g., its read status changed). Using upsert means you don't have to check first — you just write, and the database handles the collision. Notice `is_read` is intentionally excluded from the update — once you mark something read locally, a re-sync won't overwrite that.

**Indexes**

```sql
CREATE INDEX IF NOT EXISTS idx_category ON newsletters(category)
CREATE INDEX IF NOT EXISTS idx_date ON newsletters(date)
```

An index is like a pre-sorted lookup table the database maintains behind the scenes. Without indexes, every query that filters by `category` or `date` has to scan every single row. With them, the database jumps directly to matching rows. Since the UI frequently filters by both, these indexes matter.

**The `sync_state` table**

A simple key-value store used only to save the delta link between syncs. The whole table is just `key TEXT, value TEXT`. The upsert pattern is used here too.

---

### `app.py` — The Web Server

This is the FastAPI application. It reads from the database and serves HTML pages (or JSON for API calls).

**FastAPI basics**

FastAPI is a Python web framework. You define a function and decorate it with the route it handles:

```python
@app.get("/")
async def index(request: Request):
    ...
```

The `async` keyword means the function is a coroutine — it can pause while waiting for I/O (like a database read) without blocking the whole server. For a personal local tool this doesn't matter much, but it's the FastAPI convention.

**Template rendering**

The app uses **Jinja2 templates** for HTML. Rather than building HTML strings in Python (messy), you write `.html` files with placeholders:

```html
{% for nl in newsletters %}
  <div>{{ nl.subject }}</div>
{% endfor %}
```

FastAPI passes a dictionary of data to the template, which fills in the placeholders and returns finished HTML. `templates/base.html` is the shared layout (sidebar, nav), and the other templates extend it with `{% block content %}`.

**Route breakdown**

| Route | What it does |
|---|---|
| `GET /` | Shows recent newsletters (last 2 days), grouped by category |
| `GET /category/{name}` | Shows newsletters for one category, with pagination and date filtering |
| `GET /newsletter/{id}` | Shows a single newsletter, marks it read |
| `GET /raw/{id}` | Returns just the raw HTML body of an email (used in iframes) |
| `POST /sync` | Triggers `ingest.py` as a subprocess, returns status |
| `GET /api/newsletters` | JSON API version of the newsletter list |

**The sync endpoint and subprocess**

```python
result = subprocess.run(
    [sys.executable, "ingest.py"],
    capture_output=True,
    text=True,
    timeout=300,
)
```

Rather than importing and calling `sync()` directly, the web server spawns `ingest.py` as a child process. This is a pragmatic choice: the ingest process can take minutes, may need to do interactive auth, and isolates any crashes from taking down the web server. `sys.executable` ensures the same Python interpreter (and virtual environment) is used.

**Pagination**

```python
limit = 30
offset = (page - 1) * limit
```

Rather than loading all matching newsletters at once, the app fetches a page at a time. `LIMIT` tells SQLite how many rows to return; `OFFSET` tells it how many to skip. Page 1 skips 0, page 2 skips 30, etc. `has_next` is determined by checking if the current page returned a full `limit` of results — if so, there's probably a next page.

**`{id:path}` route parameter**

```python
@app.get("/newsletter/{id:path}")
```

Microsoft Graph message IDs contain slashes (`/`), which would normally be interpreted as URL path separators. The `:path` converter tells FastAPI to capture everything after `/newsletter/` as the `id`, including slashes.

---

### Templates

**`base.html`** defines the shell every page shares: sidebar with category links, date filter form, and the "Sync Now" button.

The sync button uses `fetch()` (the browser's built-in HTTP client) to hit `POST /sync` without navigating away from the page. It shows a status message, then reloads the page on success. This pattern — updating the page without a full reload — is sometimes called an **AJAX request**.

---

### `config.yaml` — Category Rules

This file isn't actually used by the current code (the `categorize.py` module calls GPT directly and doesn't read it). It looks like it was the original rule-based categorizer before GPT was added — matching sender emails to categories without an AI call. It may be a candidate for revival as a fast pre-filter before hitting the API.

---

## How Data Flows End-to-End

1. You click **Sync Now** in the browser
2. Browser sends `POST /sync` to FastAPI
3. FastAPI spawns `ingest.py` as a subprocess
4. `ingest.py` calls `ms_auth.py` → gets access token from cache (or prompts device flow)
5. `ingest.py` calls Microsoft Graph API → gets new/changed emails since last delta link
6. For each email, `categorize.py` calls GPT → gets a category string back
7. Each email is upserted into `newsletters.db` via `db.py`
8. The new delta link is saved so the next sync is incremental
9. `ingest.py` exits, stdout goes back to FastAPI
10. FastAPI returns `{"status": "ok", "message": "Ingested 12 newsletter(s)."}` to the browser
11. Browser shows the message, then reloads the page

---

## Project Layout

```
personal-box/
├── src/
│   └── personal_box/       ← the Python package
│       ├── __init__.py
│       ├── app.py          ← web server
│       ├── db.py           ← database layer
│       ├── ingest.py       ← email fetcher
│       ├── categorize.py   ← AI classifier
│       ├── ms_auth.py      ← Microsoft auth
│       └── main.py         ← entry point for `uv run dev`
├── templates/              ← HTML templates
│   ├── base.html
│   ├── index.html
│   ├── category.html
│   └── newsletter.html
├── static/                 ← CSS, JS, images
├── prompts/
│   └── category_mappings.md  ← GPT prompt template
├── config.yaml             ← legacy category rules (unused)
├── pyproject.toml          ← project config + `dev` script alias
└── newsletters.db          ← SQLite database (gitignored)
```

The `src/` layout is a Python packaging convention. Putting your code inside `src/personal_box/` (instead of the project root) prevents accidental imports — Python won't find `personal_box` unless the package is properly installed, which forces you to always run things through `uv`.
