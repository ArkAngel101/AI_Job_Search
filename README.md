# Forge

A local tool that finds US AI/ML, data-science and similar jobs from free public sources, remembers every
job it has seen, and tailors your CV to one chosen job at a time. It runs on your laptop in a locked-down
container and you use it through a web page that only your computer can open.

**What it does not do:** it never applies to a job for you, never sends email or messages, and never runs
by itself. Every scan and every AI call starts from a button you click.

## Setup

You need **Podman** (rootless) on Linux, and these accounts:

| Account | Needed for | Notes |
|---|---|---|
| **Anthropic API key** | Building your base CV and tailoring it | Create it in the [Anthropic Console](https://console.anthropic.com/) and **set a spend limit there**. This is the only thing that costs money. |
| **Adzuna API key** (free) | Broad US job search | Register at [developer.adzuna.com](https://developer.adzuna.com/) for an app id and key. Without it, Adzuna is skipped. |
| RapidAPI key (optional) | JSearch | JSearch is **not built** (its docs could not be verified), so `RAPIDAPI_KEY` is unused for now. |

All other sources (Greenhouse, Lever, Ashby, Workable, Remotive, RemoteOK, The Muse, Jobicy) need no account.

```bash
cp .env.example .env        # then fill in ANTHROPIC_API_KEY, ADZUNA_APP_ID, ADZUNA_APP_KEY
./run.sh                    # builds the image and starts the container
```

Open **http://127.0.0.1:8000**. Other commands: `./run.sh stop`, `./run.sh logs`, `./run.sh build`.
`CLAUDE_MODEL` in `.env` picks the model (default `claude-sonnet-5-5`; for example `claude-haiku-5-5` is cheaper).

## First use

1. **Settings → Claude:** copy the per-million-token prices from the Anthropic Console (needed for cost
   estimates) and set a **monthly spend cap**. The cap is a hard stop.
2. **CV:** drop your CV (PDF, DOCX, Markdown or text). Check the detected contact details, press
   *Preview sanitized text* to see exactly what will be sent, press *Estimate cost*, tick both confirmations,
   and build. This is one Claude call.
3. **Jobs → Scan now:** fetches every enabled source (free, no AI), de-duplicates, scores, and ranks.
   You can also paste a job link or the job text.
4. **Tailor** a job you like: you see the estimated cost first. Claude may only reword your headline,
   summary and bullets; code then checks the result against your base CV. Download it as `.docx` or `.pdf`.

## Privacy

- **What is sent to Anthropic:** when you build your base CV, your CV text **with your name, email, phone,
  links and street address replaced by tokens** such as `[NAME]` and `[EMAIL]`. The CV page shows the exact
  text first. When you tailor, it sends the job description plus your CV's facts (again without name or
  contact details). The real details are put back by this app afterwards. Nothing else goes to Anthropic.
- A bare "City, ST" (for example "Boston, MA") is not hidden, so employer locations survive. Add it as a
  detected detail on the CV page if you want it hidden.
- **Job sources** receive only the searches and board requests needed to list jobs. Pasted links are fetched
  from your computer, and only `https` addresses on the public internet are allowed.
- Everything you create lives in `./data` (database, CV files, generated CVs, `boards.yaml`). There is no
  telemetry and no analytics.

## How it is isolated

- The page is published only on `127.0.0.1:8000`; other machines on your network cannot reach it.
- The container has exactly one mount, `./data`; it runs as a non-root user with a read-only root filesystem,
  every Linux capability dropped, and `no-new-privileges`.
- Secrets come only from `.env` (git-ignored) and are never written to logs, files, or responses. Settings
  show a key only as "set" or "not set".
- Writes need a custom header and a local origin, the `Host` header is allow-listed, there is no CORS, and the
  page's Content Security Policy blocks all outside scripts.

## Good to know

- The example company boards in `boards.yaml` are **unverified**: use *Settings → Company boards → Check slug*.
- Remotive allows about 4 fetches a day and Jobicy asks for at most one an hour, so Forge skips them if you
  scanned recently (the scan notes say so).
- Adzuna's `max_days_old` could not be confirmed in its docs, so the maximum posting age is applied locally.
- Pages that block automated reading (LinkedIn, Indeed) cannot be added by link; paste the job text instead.
- Job text that is only a snippet is marked *partial*.

## Checking it yourself

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q                       # ~550 tests; no network, no real Claude calls
.venv/bin/python scripts/api_audit.py               # every endpoint against the running container
.venv/bin/python scripts/security_audit.py          # mounts, ports, hardening flags, secret scan
podman exec forge python -m app.scout.manual_scan   # a live check of every source (see docs/MANUAL_SCAN.md)
```

The browser tests need a headless Chromium (they are skipped without one). The suite also passes on
Python 3.12, the version the container uses.

## Layout

`app/` the FastAPI app (`forge.py` orchestrator, `scout/` sources, dedup and scoring, `reader/` job reader,
`adapter/` CV builder, tailor, validator and export, `static/` the page) · `prompts/` the two Claude prompts ·
`boards.yaml`, `skills.yaml` shipped defaults · `scripts/` audits · `tests/` · `BUILD_SPEC.md` the plan and
every decision made along the way · `CLAUDE.md` the project rules.
