# Forge: standing project rules

Forge is a local, single-user job-search tool for US AI/ML, data science, and similar roles. It runs in a rootless Podman container on the owner's laptop and is used through a local web page.

These rules apply to every request. The build plan is in `BUILD_SPEC.md`. Do one phase at a time.

## Hard constraints (never break these)

**Isolation**
- The web app listens on `0.0.0.0` inside the container, but Podman must publish it only as `127.0.0.1:8000:8000`. Nothing may expose it on any other interface.
- The container has exactly one bind mount: `./data` to `/data`. Never mount the home directory, `~/.ssh`, browser profiles, mail or calendar data, or the project root.
- The code never reads files outside `/data` and the app's own install directory.
- No email, Telegram, SMTP, or messaging code. No scheduler, cron, or systemd timers. Every scan or action starts from a button click.
- No telemetry and no outbound calls except: the job-source APIs in the spec, the LLM provider's API (Groq, `api.groq.com`), and user-pasted job URLs (see "Pasted URL fetching" below).

**Local web security**
- Allowlist the `Host` header: only `127.0.0.1:8000` and `localhost:8000`. Anything else returns 400.
- Check `Origin` on every POST. Reject if present and not an allowed local origin.
- Require a custom header (`X-Forge-Request`) on every POST. Missing returns 403.
- No CORS middleware, no `Access-Control-Allow-*` headers. No token is needed.

**Pasted URL fetching** (Job Reader only)
- https only, no credentials in the URL (`user:pass@`).
- Resolve DNS first and block private, loopback, link-local, and cloud-metadata addresses: 127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16, ::1, fc00::/7 (and fe80::/10).
- Connect to the validated IP (no second DNS lookup that could rebind), and re-check every redirect hop. Max 3 redirects.
- Timeout and response-size cap on every fetch.

**Secrets**
- Secrets come from environment variables only: `GROQ_API_KEY`, `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`, and optionally `RAPIDAPI_KEY`.
- Never write a secret to a log, a database, a file, an API response, a test fixture, or the terminal.
- `.env` is in `.gitignore`. Provide `.env.example` with empty values.

**LLM use**
- Only CV Adapter calls an LLM (Groq's API; the provider was switched from Anthropic at the owner's request on 2026-11-02). Scout, Job Reader, and Forge are deterministic Python.
- Do not add other LLM calls, web-search tools, or "discover with AI" features.
- Show an estimated cost before any Claude call, process one job per click, and cache results. Enforce a monthly spend cap set in Settings.

**CV integrity**
- Never invent experience, employers, dates, titles, degrees, skills, or numbers. Tailoring may only rephrase and reprioritize facts already in the base CV.
- Code (not the model) must validate every tailored CV against the base CV before saving it.
- Sanitize the CV text before sending it to Claude: replace the owner's name, email, phone, address, and links (LinkedIn, GitHub, portfolio, any URL) with placeholders (`[NAME]`, `[EMAIL]`, `[PHONE]`, `[ADDRESS]`, `[LINKEDIN]`, `[GITHUB]`, `[URL]`). Sanitization covers the whole text, not only the header. Re-attach the real values in code afterwards.
- The CV page must show every detected contact item for correction and a "Preview sanitized text" view showing exactly what will be sent to Claude.
- The validator must not rely only on `skills.yaml`: it also flags acronyms, CamelCase, and capitalized technical-looking terms in tailored text that are absent from the base CV text, unless on a small common-words allowlist.

**Job sources**
- Use only the official public APIs listed in `BUILD_SPEC.md`. Do not scrape LinkedIn, Indeed, or Google. Do not bypass logins, CAPTCHAs, or bot blocking.
- Send a descriptive `User-Agent`, respect rate limits, and show required attributions in the UI.
- Before coding a source, check its current official docs. If an endpoint or field is unclear, ask the owner. Do not guess.

## Engineering rules

- Python 3.12 (in the container), FastAPI, uvicorn, SQLite via stdlib `sqlite3`, httpx, pydantic, python-docx. LibreOffice (inside the container) for PDF export. Frontend is plain HTML, CSS, and vanilla JS with no build step.
- Pin dependency versions. Keep dependencies minimal and justify any new one.
- All logging to the activity table happens in code, not by asking an agent.
- Escape all external text (job titles, companies, descriptions) before inserting it into the DOM.
- File download endpoints must resolve paths and reject anything outside the allowed directory (`../` returns 400).
- Uploads: allow only pdf, docx, md, txt, with a size limit. Never execute uploaded content.
- Use type hints, small modules, and short docstrings. Keep prompts in `prompts/*.md`, not inline in code.
- Tests must not hit the live network. Use recorded fixtures.

## Working method

1. Read `BUILD_SPEC.md` and work on **one phase only**, the one the owner names.
2. Do not build features that are not in the spec. Ask first.
3. At the end of a phase: run the tests, list the files changed, and state each "Done when" item as pass or fail.
4. Stop and wait for the owner before starting the next phase.
5. Never edit files outside this project directory: `/home/Ark_Angel/Study/personal_project/AI_Job_Search/`, which is the project root (no nested `forge/` folder).
6. `CLAUDE.md` is the real rules file. `.rules` is a symlink to it. Edit only `CLAUDE.md`.
7. Commit after each finished phase. The first commit contains only `.gitignore`.
8. If a request conflicts with these rules, say so and do not proceed.
