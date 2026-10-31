# Forge: build spec

A local web app that finds US AI/ML, data science, and similar jobs from free sources, remembers every job it has seen, and tailors the owner's CV to one chosen job at a time.

**How to use this file:** `CLAUDE.md` (the rules) is the real file and `.rules` is a symlink to it. Ask for one phase at a time, for example: "Build Phase 2 from BUILD_SPEC.md." Check the result, then move on.

---

## Decisions (owner-confirmed; these override anything below that conflicts)

1. **Project root** is `/home/Ark_Angel/Study/personal_project/AI_Job_Search/`. The `forge/` in the layout below is just the root's name; no nested folder.
2. **Rules file:** `CLAUDE.md` is the real file; `.rules` is a symlink (`ln -s CLAUDE.md .rules`).
3. **Git:** `git init` at Phase 0. The first commit contains only `.gitignore` (with `.env` and `data/`). Commit after each finished phase.
4. **Pasted URLs** are an allowed outbound category, with limits: https only, no credentials in the URL; resolve DNS first and block private, loopback, link-local, and cloud-metadata IPs (127.0.0.0/8, 10/8, 172.16/12, 192.168/16, 169.254/16, ::1, fc00::/7); re-check every redirect hop, max 3 redirects; timeout and response-size cap. The rules file and the Phase 11 check reflect this.
5. **CV suggestions:** profile suggestions come from the same single Claude call that structures the CV, via a `suggested_profile` field in the schema. One call per upload.
6. **Default skills:** until a CV is uploaded, scoring uses a default skill list seeded from `skills.yaml`. CV-derived skills merge into it without overwriting anything edited in Settings.
7. **Name privacy:** the owner's name is stripped too. Claude sees `[NAME]`; the real name is re-attached in code. The CV page shows the detected name for confirmation.
8. **Local web security:** allowlist the `Host` header (`127.0.0.1:8000`, `localhost:8000`); check `Origin` on every POST; require a custom header (`X-Forge-Request`) on every POST; no CORS. No token. Added to the rules, Phase 0, and Phase 11.
9. **Model:** `CLAUDE_MODEL` defaults to `claude-sonnet-5-5` in `.env.example`. The owner can switch to Haiku in `.env`.
10. **HTML parsing:** BeautifulSoup.
11. **Validator** does not rely only on `skills.yaml`: it also flags any acronym, CamelCase, or capitalized technical-looking term in tailored text that is not in the base CV text, unless on a small common-words allowlist.
12. **Sanitization transparency:** the CV page has a "Preview sanitized text" view showing exactly what will be sent to Claude (with `[NAME]`, `[EMAIL]`, `[PHONE]`, `[LINKEDIN]` and so on in place), and lists every detected contact item so the owner can correct a miss. Sanitization covers contact details anywhere in the text, not only the header.

---

## 0. Overview

### Agents

Four named roles. Three are plain Python modules. Only CV Adapter calls an LLM.

| Agent | Job | LLM? |
|---|---|---|
| **Forge** | Orchestrator. Handles button clicks, runs the pipeline in order, tracks progress, enforces one-job-per-click for paid steps, writes the activity log. | No |
| **Scout** | Fetches from all enabled sources, normalizes, merges, de-duplicates, scores 0-100 against the profile. | No |
| **Job Reader** | Takes one promoted job (or a pasted URL or text), gets the full description, extracts structured fields with rules. | No |
| **CV Adapter** | (1) Structures the uploaded CV into base JSON. (2) Tailors it to one job. | Yes (Claude) |

Pipeline order: Forge, then Scout, then Job Reader, then CV Adapter. Scout runs on every scan. Job Reader and CV Adapter run only when the owner promotes a job.

### "Memory" means SQLite

Claude API calls are stateless. Each agent's memory is a table written by code, loaded into prompts only when needed:

- Forge: run history, settings.
- Scout: every job ever seen, with scores and `times_seen`.
- Job Reader: extracted job descriptions (cached forever).
- CV Adapter: base CV versions and tailored CVs per job.

### Free vs paid

Scanning, scoring, memory, and reading are free. Only CV Adapter spends API tokens, and only on the owner's click.

### Sources (all free)

| Source | Key? | Notes |
|---|---|---|
| Greenhouse | none | One company per call, so a board list is needed |
| Lever | none | One company per call. Keep at least 1 second between Lever requests |
| Ashby | none | One company per call |
| Workable | none | Public endpoint for published jobs. Pace requests about 3 seconds apart |
| Adzuna (US) | free key | Broad US search |
| Remotive | none | Remote jobs |
| RemoteOK | none | Remote jobs. Show a link back to RemoteOK as required by its terms. Feed only has a recent window |
| The Muse | none reported | US-focused. Credit it in the UI |
| Jobicy | none | Remote jobs |
| JSearch (RapidAPI) | free key, **optional** | Free plan is 200 requests per month, hard limit. Off unless `RAPIDAPI_KEY` is set |

Not used: LinkedIn, Indeed, Google Jobs, and USAJOBS. Never scrape them.

**Endpoint verification rule:** the endpoints below are the known starting points. Before writing each source module, confirm it against the source's current official docs. If a doc is unreachable or unclear, skip that source, report it, and ask the owner.

- Greenhouse: `GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true`
- Lever: `GET https://api.lever.co/v0/postings/{slug}?mode=json`
- Ashby: `GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true`
- Adzuna: `https://api.adzuna.com/v1/api/jobs/us/search/{page}`
- Remotive: `GET https://remotive.com/api/remote-jobs?search={keyword}`
- RemoteOK: `GET https://remoteok.com/api` (the first array element is a legal notice, not a job)
- JSearch: `https://jsearch.p.rapidapi.com/search` with `X-RapidAPI-Host` and `X-RapidAPI-Key` headers
- Workable, The Muse, Jobicy: look up the current documented endpoints.

### Target profile (defaults, editable in Settings)

- **Country:** US only. Remote US roles count as in-country.
- **Titles:** AI Engineer, Machine Learning Engineer, ML Engineer, Data Scientist, Applied Scientist, NLP Engineer, Computer Vision Engineer, ML Research Engineer, Data Analyst, MLOps Engineer.
- **Search keywords (1-2 words each):** `machine learning`, `data scientist`, `AI engineer`, `NLP`, `computer vision`, `LLM`, `deep learning`, `MLOps`, `PyTorch`, `applied scientist`.
- **Seniority:** `entry`. Intern, co-op, new grad, entry-level, junior, and associate get full credit. Mid gets partial. Senior, staff, principal, lead, and director are penalized. Settings can switch to `any` (no level preference). Allowed values: `entry | mid | senior | lead | principal | manager | any`.
- **Skills (scoring_skills):** a default list seeded from `skills.yaml` until a CV is uploaded; CV-derived skills then merge in without overwriting anything edited in Settings.
- **Thresholds:** first_cut 50, final 65.

### Tech stack

Python 3.12, FastAPI, uvicorn, sqlite3, httpx, pydantic, python-docx, BeautifulSoup for HTML, LibreOffice in the container for PDF. Plain HTML/CSS/JS frontend. Anthropic Python SDK for CV Adapter.

### Project layout

```
AI_Job_Search/            (project root)
  CLAUDE.md               (rules)
  .rules                  (symlink to CLAUDE.md)
  BUILD_SPEC.md
  Containerfile
  run.sh
  .env.example
  .gitignore
  requirements.txt
  boards.yaml             # company boards per ATS (owner-editable)
  skills.yaml             # AI/ML skill vocabulary for Job Reader
  prompts/
    cv_builder.md
    cv_tailor.md
  app/
    main.py               # FastAPI app and routes
    db.py                 # schema, connection, helpers
    forge.py              # orchestrator and run management
    scout/
      sources/            # one module per source
      normalize.py
      dedup.py
      scoring.py
    reader/
      reader.py
    adapter/
      cv_builder.py
      cv_tailor.py
      validator.py
      export.py           # docx and pdf
      llm.py              # the only module that calls Anthropic
    static/               # index.html, app.js, styles.css
  data/                   # bind-mounted; db, cv, outputs (gitignored)
  tests/
    fixtures/
```

---

## Phase 0: Skeleton and container

**Build**
- Project layout, `requirements.txt` (pinned), `.gitignore` (includes `.env` and `data/`), `.env.example`.
- FastAPI app with `GET /api/health`, serving a placeholder `index.html` with `Cache-Control: no-store`.
- `Containerfile` (slim Python base, non-root user, LibreOffice installed for later).
- `run.sh` with build and run steps. The run command must include:
  - `-p 127.0.0.1:8000:8000`
  - `-v ./data:/data:Z`
  - `--userns=keep-id`
  - `--env-file .env`
  - `--read-only --tmpfs /tmp`
  - `--cap-drop=all --security-opt no-new-privileges`
- App uses `/data` for everything it writes.
- Local web security middleware (see Decisions 8): `Host` allowlist (400 otherwise), `Origin` check and required `X-Forge-Request` header on every POST (403 otherwise), no CORS.
- `git init` with `.gitignore` as the first commit; commit at the end of the phase.

**Done when**
- `podman build` and `run.sh` start the app, and `curl http://127.0.0.1:8000/api/health` returns 200.
- The port is not reachable on the machine's LAN address.
- The container has no mounts other than `/data`.
- Tests show a bad `Host` returns 400, and a POST with a missing header or foreign `Origin` is rejected.

---

## Phase 1: Database and activity log

**Build** `db.py` with these SQLite tables, created if missing:

- `jobs(key PRIMARY KEY, title, company, location, url, canonical_url, source, source_id, description, score, score_breakdown_json, status, user_status, times_seen, first_run, last_run, closed, jd_json, cv_path)`
  - `status` only moves forward: `seen`, `matched`, `read`, `tailored`. It never downgrades.
  - `user_status`: `new | interested | applied | interview | rejected`.
- `runs(id, trigger, status, started_at, ended_at, summary_json)`
- `activity_log(id TEXT PRIMARY KEY, agent_name, task_description, status, model_used, created_at)`
  - agent names: `forge`, `scout`, `job-reader`, `cv-adapter`. Status: `completed | failed`. Model is `none` for non-LLM agents. Indexes on agent_name, status, created_at DESC.
- `llm_usage(id, ts, job_key, model, input_tokens, output_tokens, est_cost_usd)`
- `cv_versions(id, created_at, raw_filename, base_cv_json, valid)`
- `tailored_cvs(job_key, created_at, cv_json, docx_path, pdf_path, validation_json)`
- `settings(key PRIMARY KEY, value_json)`

Add a single `log_activity(agent, description, status, model)` helper. Descriptions are under 140 characters. All writes go through `db.py`.

**Done when:** tests cover creating the schema, status never downgrading, and `log_activity` populating all fields.

---

## Phase 2: Scout sources

**Build** one module per source in `app/scout/sources/`, each exposing `fetch(profile, boards, client) -> (list[RawListing], notes[])`.

- Normalize every listing to: `{source, source_id, title, company, location, description, url, created, salary_min, salary_max, contract_type}`.
- Strip HTML from descriptions.
- Shared httpx client with `User-Agent: ForgeJobSearch/1.0`, timeouts, and per-source rate limiting (Lever 1 per second, Workable about 1 per 3 seconds).
- `boards.yaml` lists company slugs per ATS. Ship these **unverified examples** and tell the owner to check them:
  - greenhouse: `anthropic`, `databricks`, `stripe`, `scaleai`
  - lever: `netflix`
  - ashby: `openai`, `ramp`
  - workable: `huggingface`
- At the start of a scan, an invalid slug (404) is skipped and reported in `notes`, never fatal.
- If one source fails, record a note and continue with the others.
- Adzuna: for each keyword, sort by date for pages 1 to `pages`, plus relevance page 1. Adzuna ANDs all words, so if a keyword returns zero hits, drop trailing words until it returns results and note the change.
- ATS boards return all of a company's jobs, so filter by US location (or remote-US) and by title relevance in Scout, not in the fetcher.
- Skip sources whose key is missing (Adzuna, JSearch) and note why.
- Add the attribution strings for RemoteOK and The Muse to a constants file for the UI.

**Done when:** each source has a fixture-based test, a failing source never aborts the run, and a manual run against live sources is documented but not part of the tests.

---

## Phase 3: Normalization and de-duplication

**Build** `dedup.py` and the key logic.

Match order for deciding two listings are the same job:
1. Same `(source, source_id)`.
2. Same canonical URL. Strip `utm_*`, `gh_src`, `lever-source`, `ref`, `source`, tracking fragments, and trailing slashes. Lowercase the host.
3. Same normalized `title + company + location`. Normalize by lowercasing, removing punctuation, and stripping company suffixes (Inc, LLC, Ltd, Corp, Co, Group, GmbH, AG, SE, Technologies). Strip gender/format tags like `(m/f/d)` from titles.

Location must be part of the fuzzy key, so the same title and company in different cities stay separate. Remote counts as its own location value.

When the same job appears from several sources, keep one row and prefer the source with the fullest description (ATS first). Record all source names seen.

**Closed postings:** after a scan, for each ATS board that was fetched **successfully and completely**, mark previously seen jobs from that board that no longer appear as `closed = 1`. Never mark closed after a failed or partial fetch. Reappearing jobs are reopened.

**Done when:** tests cover all three match levels, tracking-parameter stripping, two cities staying separate, a cross-source duplicate collapsing, and closed/reopened behavior.

---

## Phase 4: Scoring

**Build** `scoring.py`: `score_listing(listing, profile, mode) -> {score, breakdown, flags}`. Deterministic, no LLM.

Weights:
```
first_cut: title 50, skill 22, seniority 16, location 12
final:     skill 42, title 34, seniority 14, location 10
```

- **Title:** overlap of distinctive domain tokens only. Strip generic words from both sides first: level words (senior, junior, intern, co-op, lead...), role nouns (engineer, scientist, analyst, specialist...), and filler (remote, full-time, m/f/d...). Multiply by a precision factor (0.5 to 1.0) so one lucky keyword in an off-topic title cannot score full. Apply a role-family penalty (x0.35) when the job's role family differs from the profile's (for example Sales, Recruiter, or Assistant roles when the profile targets engineers or scientists). Treat Engineer, Developer, Scientist, and Researcher as related within AI/ML.
- **Skill:** fraction of `scoring_skills` found in title plus snippet. A multi-word skill counts on exact phrase or at least 60% of its meaningful tokens.
- **Seniority:** closeness to `profile.seniority`. Full credit when `any`. For `entry`, treat intern, co-op, new grad, entry level, junior, and associate as full credit.
- **Location:** full credit for US or remote-US. Reject non-US listings before scoring. Keep this component small.

Two guards:
1. **Relevance gate:** if there is no domain anchor (no distinctive title overlap and zero skill hits), cap the score at about 38.
2. **Wrong-level filter:** if `seniority = entry`, multiply by 0.35 for staff, principal, director, VP, or "10+ years" titles. Do not apply it when seniority is `any`.

Flags: `domain_anchor`, `wrong_level`, `needs_sponsorship_or_citizenship` (set later by Job Reader).

Add a CLI or test helper that prints the top 10 of a scored fixture set with breakdowns.

**Done when:** tests show obvious off-domain roles (sales, recruiter, unrelated engineering) are filtered, entry-level and intern AI/ML roles rank high, and senior roles are penalized under `entry`.

---

## Phase 5: Job memory and the Find run (Forge)

**Build** `forge.py` and the run flow.

- `run_find()`: for each enabled source (Scout) fetch, normalize, de-duplicate, score at `first_cut`, upsert into `jobs`, mark closed, then record the run. All of this is attributed to `scout` in the activity log. Forge logs run start and finish.
- For each passing listing, `upsert_seen`: insert, or bump `times_seen` and `last_run`. Raise `status` only forward. A job is NEW if `first_run == this run`.
- Run in a background thread. Return 409 if a run is already active. Track progress per source (pending, running, done, failed) so the UI can show live status that survives a page reload.
- Endpoints:
  - `POST /api/run` starts a find.
  - `GET /api/run-status` returns the current or last run, with per-source progress.
  - `GET /api/jobs` returns ranked rows `{rank, score, title, company, location, url, source, new, times_seen, closed, status, user_status, key}` with filters.
  - `GET /api/overview` returns counts: found, matched, read, tailored, known total, new in last run.

**Done when**
- Running a scan twice shows `times_seen` incrementing, NEW counts dropping on the second run, and no duplicate keys.
- A source failure shows in the run notes without stopping the run.
- No Claude calls happen anywhere in this phase (assert in a test).

---

## Phase 6: Job Reader (no LLM)

**Build** `reader.py`. Input: a job key, a pasted URL, or pasted text.

- ATS sources (Greenhouse, Lever, Ashby, Workable) already return full descriptions. Use them directly, no page fetch.
- Other sources: fetch the posting page with httpx, first looking for JSON-LD `JobPosting`, then falling back to cleaned page text. If the page is blocked, a login wall, or very thin, fall back to the listing's own description and mark the result `partial`.
- Extract `{title, company, location, remote, salary, responsibilities[], required_skills[], preferred_skills[], seniority_signals[], flags}`:
  - Split sections by headings such as Responsibilities, What you'll do, Requirements, Qualifications, Minimum, Preferred, Nice to have, Bonus.
  - Match skills against `skills.yaml` (an AI/ML vocabulary: PyTorch, TensorFlow, scikit-learn, SQL, Spark, LLM, RAG, MLOps, AWS, and so on) plus the profile's skills.
  - Flag US work authorization, sponsorship, citizenship, or security clearance language.
- Re-score at `final` mode and set `status = read`.
- Cache in `jd_json`. A job already read is never re-fetched.

**Paste URL / paste text (Forge):**
- `POST /api/add-job {url?, text?}`.
- Recognize ATS URLs (`boards.greenhouse.io/{slug}/jobs/{id}`, `job-boards.greenhouse.io`, `jobs.lever.co/{slug}/{id}`, `jobs.ashbyhq.com/{slug}/{id}`, `apply.workable.com/{slug}/j/{id}`). Fetch via the official API, and offer to add the slug to `boards.yaml`.
- For any other URL, try JSON-LD. If the page cannot be read (LinkedIn and Indeed usually cannot), return a clear message asking the owner to paste the description text instead. Do not attempt to bypass blocks.
- All page fetches follow the "Pasted URL fetching" rules in `CLAUDE.md`: https only, no credentials in URL, DNS resolved first with private/loopback/link-local/metadata IPs blocked, every redirect hop re-checked (max 3), timeout and size cap. Tests cover each block.

**Done when:** fixtures for each ATS and for a JSON-LD page parse correctly, a blocked page produces the paste-text prompt, and no LLM calls occur.

---

## Phase 7: CV upload and base CV (CV Adapter, Claude)

**Build** `adapter/llm.py` (the only Anthropic caller), `cv_builder.py`, and `prompts/cv_builder.md`.

- `POST /api/cv`: accepts pdf, docx, md, or txt (size limit), saves it to `/data/cv/`, archives the previous raw file, and keeps the existing base CV until the new build validates.
- Extract text (pdftotext for PDF, python-docx for DOCX, direct read for text) into `base_cv.txt`.
- **Sanitize** the whole text (not only the header) in code, replacing the owner's name, email, phone, address, and links with `[NAME]`, `[EMAIL]`, `[PHONE]`, `[ADDRESS]`, `[LINKEDIN]`, `[GITHUB]`, `[URL]`. Store the detected items. Then make **one** Claude call to structure the sanitized text into:
  ```
  { name, headline, summary, competencies[], experience[{company,location,start,end,role,bullets[]}],
    education[], skills[{category,items[]}], projects[], languages[{lang,level}],
    suggested_profile{titles[], keywords[], scoring_skills[], seniority} }
  ```
  strictly from the document's content. Re-attach the real name and contact info in code. Validate against a pydantic schema, then replace `base_cv.json` atomically only if valid.
- **Contact review:** `GET /api/cv` also returns every detected contact item (name, emails, phones, addresses, links) and the sanitized text. The owner can correct a miss (add or remove an item), and the text is re-sanitized before any call. The detected name is shown for confirmation.
- **Preview sanitized text:** `GET /api/cv/preview` returns exactly the text that would be sent to Claude. The preview is built by the same function the call uses.
- `suggested_profile` (from the same call) is **merged** into the profile without overwriting any setting the owner already changed, and into the seeded default skills. This step is best-effort and must never break the base-CV build.
- `GET /api/cv` returns status (`building` flag, base CV preview).
- Before the call, show the estimated cost. Log usage to `llm_usage` and the activity log (`cv-adapter`, model name).
- Model comes from the `CLAUDE_MODEL` env var (default `claude-sonnet-5-5` in `.env.example`). Per-token prices come from Settings (the owner enters them from the Anthropic Console), because pricing changes.

**Done when:** a fixture CV builds a valid `base_cv.json`, a re-upload that fails validation leaves the old base CV intact, contact details (including the name, and details placed mid-document or in the footer) never appear in the prompt (test it), the preview endpoint matches the text actually sent, and the API key never appears in any log.

---

## Phase 8: Tailoring, validation, and export

**Build** `cv_tailor.py`, `validator.py`, `export.py`, `prompts/cv_tailor.md`, and `POST /api/promote {key}`.

Promote flow for one job:
1. Run Job Reader if the job is not yet read (free, cached).
2. If a tailored CV already exists for this job, reuse it and spend nothing.
3. Show the estimated cost, require the click's confirmation, then make one Claude call.
4. Claude may rewrite **only** `headline`, `summary`, and experience `bullets`. It returns JSON keyed by `(company, role, start)`. Code merges those onto the base CV, so employers, roles, dates, and education can never change.
5. **Validator (code, not the model):**
   - Every skill or tool named in the summary or bullets must appear in the base CV text.
   - Not limited to `skills.yaml`: also flag any acronym (e.g. `GAN`), CamelCase term (e.g. `LangChain`), or capitalized technical-looking term in the tailored text that is absent from the base CV text, unless it is on a small common-words allowlist.
   - Every number or percentage in a bullet must appear in the base CV.
   - Bullet counts per role must match, and no new roles or employers can appear.
   - On failure, retry once with the violations listed. If it still fails, save the result with `validation: failed` and warn the owner clearly.
6. Save to `tailored_cvs`, set job `status = tailored`.

**Export:**
- `export.py` renders a tailored CV to `.docx` with python-docx (name, headline, shaded section bars, experience, education, skills) and converts to PDF headlessly with LibreOffice.
- `GET /api/cv-docx?key=...` and `/api/cv-pdf?key=...` resolve paths and reject traversal with 400.

**Done when:** a fixture promote produces a validated tailored CV, an injected fabricated skill is caught by the validator, a repeat promote costs nothing, both downloads open correctly, and `../../etc/passwd` returns 400.

---

## Phase 9: Web UI

Pure DOM, no framework, no build step. Poll the API every few seconds while a run is active.

**Design:** dark theme only. Background `#080c17`, gold accent `#eac266`, text `#f3f5fb`, secondary `#b4b7c1`, faint `#70747f`, panels `#121724`, hairlines `#272c3c`. Glass-style cards (translucent fill, 1px low-opacity border, 16px radius, blur). Large heavy numbers for stats. Agent colors: Forge gold, Scout sky-blue, Job Reader teal `#00c89c`, CV Adapter coral `#ff7c68`. Laptop-first, no horizontal overflow at 1024px and up.

**Shell:** left sidebar (FORGE brand, Overview, Jobs, CV, Settings), top bar with page title and a run-status pill (`RUNNING...` or `LAST FIND · N matched`).

**Pages**
- **Overview:** counts (found, matched, read, tailored, known total), a four-stage funnel, four agent cards (name, role, status LIVE/WORKING/IDLE, task count, success rate, last action, model; glow while working, driven by backend state), and a scrolling activity stream (agent pill, task text, model, completed/failed, time).
- **Jobs:** "Scan now" button with a scanning state and per-source progress. A "paste URL or text" box. Table: rank, score pill (green at 65+, gold at 50-64), role, company, location, source, status dropdown (persisted), flags (NEW, xN seen, CLOSED, sponsorship warning, CV done), actions (Tailor with cost confirmation, JD drawer, CV view, DOC, PDF, original posting link). A legend under the table. A filter for new only and hide closed.
- **CV:** drop zone, building state, base CV preview, the detected contact items (editable, with the detected name for confirmation), and a "Preview sanitized text" view showing exactly what will be sent to Claude.
- **Settings:** see Phase 10.

Footer shows required source attributions (RemoteOK, The Muse). Escape all external text.

**Done when:** every page renders with real data, scanning shows live progress and survives a reload, and a headless-browser smoke test reports zero console errors.

---

## Phase 10: Settings

`GET/POST /api/settings` edits the profile and app settings, with an atomic write that preserves untouched keys.

- **Validation:** clamp numbers (thresholds 0-100, `final >= first_cut`, pages 1-5, max age 1-365 days, results 10-300). Reject invalid choices with 400: seniority outside the allowed list, or all sources turned off.
- **Sections:** profile and targeting (seniority segmented control with `any`, target titles and scoring skills as editable chips); search (keywords as chips, pages, max posting age, results per scan); scoring (two threshold sliders); sources (toggle per source, key status shown as set or not set, never the key itself); company boards editor for `boards.yaml` with a "check slug" button; Claude (model name, per-token prices, monthly spend cap with a hard stop, spend used this month); system (read-only status).
- Dirty-tracking Save button.

**Done when:** a threshold change and a new keyword change the next scan's results, bad input returns 400 or is clamped as described, and no secret appears in any settings response.

---

## Phase 11: Tests and hardening

- API audit script: every endpoint returns 200 with expected keys, bad input returns 4xx, traversal is rejected, profile snapshot and restore around write tests.
- Security checks: the port is bound to `127.0.0.1` only, the container has only the `/data` mount, a grep across `/data` and logs finds no secret values, no code path calls any LLM outside `adapter/llm.py`, and no network call goes to a host outside the allowed list (job-source hosts, the Anthropic API, and user-pasted https URLs that pass the SSRF checks).
- SSRF checks: pasted URLs with `http://`, embedded credentials, or hostnames resolving to private, loopback, link-local, or metadata addresses are rejected; a redirect to such an address is rejected; more than 3 redirects is rejected.
- Local web checks: a bad `Host` returns 400; a POST without `X-Forge-Request` or with a foreign `Origin` is rejected; no `Access-Control-Allow-*` header is ever sent.
- Headless UI audit: all pages load with zero console errors, settings save works, the status dropdown persists, the scan button works, and drawers and downloads work.
- Write a short `README.md`: setup, accounts needed (Adzuna free key, Anthropic API key with a spend limit, optional RapidAPI key for JSearch), how to run, and a privacy note that CV text (without contact details) is sent to the Anthropic API when you build or tailor.

**Done when:** the full test suite passes on repeated runs with zero failures.
