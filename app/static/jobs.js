"use strict";
/* Jobs: scan, paste a URL or text, the ranked table, and per-job actions. */

(() => {
  const USER_STATUSES = ["new", "interested", "applied", "interview", "rejected"];
  const SOURCE_LABEL = {
    greenhouse: "Greenhouse", lever: "Lever", ashby: "Ashby", workable: "Workable", adzuna: "Adzuna",
    remotive: "Remotive", remoteok: "RemoteOK", themuse: "The Muse", jobicy: "Jobicy",
  };
  const filters = { newOnly: false, hideClosed: false, q: "" };
  let container = null;
  let stopListening = null;
  let flashKey = null;
  let wasActive = false;
  let searchTimer = null;

  /* --- scan card ----------------------------------------------------------------------- */

  function sourceChips(sources) {
    const names = Object.keys(sources);
    if (!names.length) return h("span", { class: "faint small" }, "No scan has run yet.");
    return h("div", { class: "sources" }, names.map((name) =>
      h("span", { class: `source-chip ${sources[name]}`, dataset: { source: name } }, h("span", { class: "dot" }),
        SOURCE_LABEL[name] || name, h("span", { class: "faint" }, sources[name]))));
  }

  function scanCard() {
    const { active, run, sources } = Forge.runStatus;
    const notes = (run && run.summary && run.summary.notes) || [];
    const summary = run && run.summary && run.status === "completed" ? run.summary : null;
    const button = h("button", { class: "btn primary", id: "scan-btn", disabled: active, onclick: startScan },
      active ? [h("span", { class: "spinner" }), "Scanning..."] : "Scan now");
    return h("div", { class: "card", id: "scan-card" },
      h("div", { class: "row", style: { justifyContent: "space-between" } },
        h("div", { class: "row" }, button,
          summary ? h("span", { class: "muted small" },
            `Last scan: ${summary.found} listings, ${summary.matched} matched, ${summary.new} new, ${summary.closed} closed`) : null),
        h("span", { class: "faint small" }, "Free: no AI is used while scanning")),
      h("div", { style: { marginTop: "12px" } }, sourceChips(sources)),
      run && run.status === "failed" ? h("div", { class: "notice bad", style: { marginTop: "10px" } }, "The last scan failed. Check the activity log on Overview.") : null,
      notes.length ? h("details", { class: "notes" }, h("summary", {}, `${notes.length} note${notes.length === 1 ? "" : "s"} from the last scan`),
        h("ul", {}, notes.map((n) => h("li", {}, n)))) : null);
  }

  async function startScan() {
    const result = await Forge.api("/api/run", { method: "POST" });
    if (!result.ok && result.status !== 409) { Forge.toast(Forge.detail(result), "error"); return; }
    Forge.refreshRun();
  }

  /* --- paste a URL or text ----------------------------------------------------------------- */

  function pasteCard() {
    const url = h("input", { type: "text", id: "paste-url", placeholder: "Paste a job link (https://...)", "aria-label": "Job link" });
    const text = h("textarea", { id: "paste-text", class: "hidden", placeholder: "Paste the job description text here", "aria-label": "Job description text" });
    const extra = h("div", { id: "paste-extra", class: "row hidden", style: { marginTop: "8px" } },
      h("input", { type: "text", id: "paste-title", placeholder: "Job title (optional)", "aria-label": "Job title" }),
      h("input", { type: "text", id: "paste-company", placeholder: "Company (optional)", "aria-label": "Company" }));
    const message = h("div", { id: "paste-message", class: "small", style: { marginTop: "8px" } });
    const offer = h("div", { id: "paste-offer" });
    const toggle = h("button", { class: "btn small", type: "button", onclick: () => showText(true) }, "Paste text instead");
    const showText = (show) => {
      text.classList.toggle("hidden", !show);
      extra.classList.toggle("hidden", !show);
      toggle.classList.toggle("hidden", show);
    };
    const add = h("button", { class: "btn primary", id: "paste-add", type: "button", onclick: async () => {
      const body = { url: url.value.trim() || null, text: text.value.trim() || null,
        title: ($("#paste-title").value || "").trim() || null, company: ($("#paste-company").value || "").trim() || null };
      if (!body.url && !body.text) { message.textContent = "Paste a link or the job description first."; return; }
      add.disabled = true;
      Forge.clear(message).append(h("span", { class: "spinner" }), "Reading the job...");
      const result = await Forge.api("/api/add-job", { method: "POST", json: body });
      add.disabled = false;
      Forge.clear(message);
      if (!result.ok) {
        message.append(h("span", { class: "notice warn" }, Forge.detail(result)));
        if (result.data && result.data.needs_text) showText(true);
        return;
      }
      const job = result.data.job;
      message.append(h("span", { class: "pill good" }, result.data.created ? "Added" : "Already known"), " ",
        `${job.title} at ${job.company}`, result.data.partial ? " (partial: the page could not be fully read)" : "");
      url.value = ""; text.value = "";
      flashKey = result.data.key;
      if (result.data.offer_add_board) renderOffer(offer, result.data.offer_add_board);
      loadJobs();
    } }, "Add job");
    return h("div", { class: "card" }, h("h2", {}, "Add a job"),
      h("div", { class: "paste-grid" }, url, h("div", { class: "row" }, add, toggle)), text, extra, message, offer);
  }

  function renderOffer(slot, board) {
    Forge.clear(slot).append(h("div", { class: "offer" },
      `Scan the ${SOURCE_LABEL[board.ats] || board.ats} board "${board.slug}" on every scan? `,
      h("button", { class: "btn small", onclick: async () => {
        const result = await Forge.api("/api/add-board", { method: "POST", json: { ats: board.ats, slug: board.slug } });
        Forge.clear(slot);
        Forge.toast(result.ok ? (result.data.added ? "Board added to boards.yaml" : "That board is already listed") : Forge.detail(result), result.ok ? "ok" : "error");
      } }, "Add board"), " ", h("button", { class: "btn small", onclick: () => Forge.clear(slot) }, "No thanks")));
  }

  /* --- filters and table ----------------------------------------------------------------------- */

  function filterBar() {
    return h("div", { class: "row", style: { marginBottom: "12px" } },
      h("input", { type: "search", id: "filter-q", placeholder: "Search title or company", value: filters.q, "aria-label": "Search jobs",
        style: { maxWidth: "260px" }, oninput: (e) => { filters.q = e.target.value; clearTimeout(searchTimer); searchTimer = setTimeout(loadJobs, 250); } }),
      h("label", { class: "check" }, h("input", { type: "checkbox", id: "filter-new", checked: filters.newOnly,
        onchange: (e) => { filters.newOnly = e.target.checked; loadJobs(); } }), "New only"),
      h("label", { class: "check" }, h("input", { type: "checkbox", id: "filter-closed", checked: filters.hideClosed,
        onchange: (e) => { filters.hideClosed = e.target.checked; loadJobs(); } }), "Hide closed"));
  }

  function scoreCell(job) {
    const cls = job.score >= 65 ? "good" : job.score >= 50 ? "warn" : "low";
    return h("span", { class: `score ${cls}`, title: "Match score (0-100)" }, Math.round(job.score));
  }

  function flagsCell(job) {
    return h("div", { class: "flags" },
      job.new ? h("span", { class: "pill info" }, "NEW") : null,
      job.times_seen > 1 ? h("span", { class: "pill dim", title: "Times seen across scans" }, `×${job.times_seen} seen`) : null,
      job.closed ? h("span", { class: "pill bad" }, "CLOSED") : null,
      job.needs_sponsorship ? h("span", { class: "pill warn", title: "The posting mentions no sponsorship, citizenship, or a clearance" }, "⚠ sponsorship") : null,
      job.status === "tailored" ? h("span", { class: "pill good" }, "CV done") : null);
  }

  function statusSelect(job) {
    return h("select", { "aria-label": `Status for ${job.title}`, onchange: async (e) => {
      const previous = job.user_status;
      const result = await Forge.api("/api/job/status", { method: "POST", json: { key: job.key, user_status: e.target.value } });
      if (result.ok) job.user_status = e.target.value;
      else { e.target.value = previous; Forge.toast(Forge.detail(result), "error"); }
    } }, USER_STATUSES.map((s) => h("option", { value: s, selected: s === job.user_status }, s)));
  }

  function actionsCell(job) {
    const link = Forge.safeUrl(job.url);
    const key = encodeURIComponent(job.key);
    return h("div", { class: "actions" },
      h("button", { class: "btn small", onclick: () => tailor(job) }, job.status === "tailored" ? "CV" : "Tailor"),
      h("button", { class: "btn small", onclick: () => openDrawer(job), title: "Job description" }, "JD"),
      job.status === "tailored" ? [
        h("button", { class: "btn small", onclick: () => viewCv(job), title: "View the tailored CV" }, "View"),
        h("a", { class: "btn small", href: `/api/cv-docx?key=${key}`, download: "", title: "Download .docx" }, "DOC"),
        h("a", { class: "btn small", href: `/api/cv-pdf?key=${key}`, download: "", title: "Download .pdf" }, "PDF")] : null,
      link ? h("a", { class: "btn small", href: link, target: "_blank", rel: "noopener noreferrer", title: "Original posting" }, "↗") : null);
  }

  function jobRow(job) {
    return h("tr", { class: `${job.closed ? "closed" : ""}${job.key === flashKey ? " flash" : ""}`, dataset: { key: job.key } },
      h("td", { class: "faint" }, job.rank),
      h("td", {}, scoreCell(job)),
      h("td", {}, h("div", { class: "ellipsis", title: job.title, style: { fontWeight: "600" } }, job.title)),
      h("td", {}, h("div", { class: "ellipsis", title: job.company }, job.company)),
      h("td", {}, h("div", { class: "ellipsis muted", title: job.location }, job.location || "–")),
      h("td", {}, h("span", { class: "faint source", title: SOURCE_LABEL[job.source] || job.source }, SOURCE_LABEL[job.source] || job.source)),
      h("td", {}, statusSelect(job)),
      h("td", {}, flagsCell(job)),
      h("td", {}, actionsCell(job)));
  }

  function table(jobs) {
    if (!jobs.length) return h("div", { class: "empty", id: "jobs-empty" }, "No jobs yet. Press Scan now, or add a job above.");
    const cols = [["rank", "#"], ["score", "Score"], ["role", "Role"], ["company", "Company"], ["location", "Location"],
      ["source", "Source"], ["status", "Status"], ["flags", "Flags"], ["actions", "Actions"]];
    return h("div", { class: "table-wrap" }, h("table", { class: "jobs", id: "jobs-table" },
      h("thead", {}, h("tr", {}, cols.map(([c, label]) => h("th", { class: `col-${c}` }, label)))),
      h("tbody", {}, jobs.map(jobRow))));
  }

  function legend() {
    return h("div", { class: "legend" },
      h("span", { class: "score good" }, "65+"), "strong match", h("span", { class: "score warn" }, "50-64"), "possible match",
      h("span", { class: "pill info" }, "NEW"), "first seen in the last scan", h("span", { class: "pill dim" }, "×N seen"), "seen in N scans",
      h("span", { class: "pill bad" }, "CLOSED"), "no longer on its company board", h("span", { class: "pill warn" }, "⚠ sponsorship"),
      "mentions sponsorship, citizenship or clearance", h("span", { class: "pill good" }, "CV done"), "tailored CV ready");
  }

  async function loadJobs() {
    const params = new URLSearchParams({ limit: "500" });
    if (filters.newOnly) params.set("new_only", "true");
    if (filters.hideClosed) params.set("hide_closed", "true");
    if (filters.q.trim()) params.set("q", filters.q.trim());
    const result = await Forge.api(`/api/jobs?${params}`);
    const slot = container && $("#jobs-slot");
    if (!slot) return;
    Forge.clear(slot);
    if (!result.ok) { slot.append(h("div", { class: "notice bad" }, Forge.detail(result))); return; }
    slot.append(table(result.data), legend());
    flashKey = null;
  }

  function renderScan() {
    const slot = container && $("#scan-slot");
    if (slot) Forge.clear(slot).append(scanCard());
  }

  /* --- job description drawer ------------------------------------------------------------------- */

  function list(items) { return items.length ? h("ul", {}, items.map((i) => h("li", {}, i))) : h("span", { class: "faint" }, "none"); }
  function chips(items, cls) { return items.length ? items.map((i) => h("span", { class: `chip ${cls || ""}` }, i)) : h("span", { class: "faint" }, "none"); }

  async function openDrawer(job) {
    const drawer = Forge.drawer({ title: job.title, body: h("div", { class: "muted" }, h("span", { class: "spinner" }), "Loading") });
    const result = await Forge.api(`/api/jd?key=${encodeURIComponent(job.key)}`);
    Forge.clear(drawer.body);
    if (!result.ok) { drawer.body.append(h("div", { class: "notice bad" }, Forge.detail(result))); return; }
    const d = result.data;
    const link = Forge.safeUrl(d.url);
    const jd = d.jd;
    drawer.body.append(
      h("div", { class: "muted" }, `${d.company} · ${d.location || "location not listed"} · ${SOURCE_LABEL[d.source] || d.source}`),
      h("div", { class: "row", style: { marginTop: "8px" } }, scoreCell({ score: d.score || 0 }), link ? h("a", { href: link, target: "_blank", rel: "noopener noreferrer" }, "Open the original posting ↗") : null),
      !d.read ? h("div", { class: "notice warn", style: { marginTop: "12px" } },
        "This job has not been read yet. Reading happens for free when you press Tailor. The listing text is shown below.") : null,
      jd && jd.partial ? h("div", { class: "notice warn", style: { marginTop: "12px" } }, "Partial: ", jd.note || "only the listing's own description was available.") : null,
      jd && jd.flags && jd.flags.needs_sponsorship_or_citizenship ? h("div", { class: "notice bad", style: { marginTop: "12px" } },
        "Heads up: this posting mentions ", [jd.flags.no_sponsorship ? "no visa sponsorship" : null, jd.flags.citizenship_required ? "citizenship" : null,
          jd.flags.clearance ? "a security clearance" : null].filter(Boolean).join(", "), ".") : null,
      jd ? h("div", {},
        h("div", { class: "sec" }, h("h4", {}, "Required skills"), chips(jd.required_skills)),
        h("div", { class: "sec" }, h("h4", {}, "Preferred skills"), chips(jd.preferred_skills, "pref")),
        h("div", { class: "sec" }, h("h4", {}, "Responsibilities"), list(jd.responsibilities)),
        h("div", { class: "sec" }, h("h4", {}, "Details"),
          h("div", {}, `Remote: ${jd.remote ? "yes" : "no / not stated"}`),
          h("div", {}, `Salary: ${jd.salary ? `${Forge.number(jd.salary.min)} – ${Forge.number(jd.salary.max)} ${jd.salary.currency || ""} ${jd.salary.period || ""}` : "not stated"}`)),
        h("div", { class: "sec" }, h("h4", {}, "Seniority signals"), list(jd.seniority_signals)),
        jd.flags && jd.flags.matches && jd.flags.matches.length ? h("div", { class: "sec" }, h("h4", {}, "Work authorization wording"), list(jd.flags.matches)) : null) : null,
      d.score_breakdown ? h("div", { class: "sec" }, h("h4", {}, `Score breakdown (${d.score_breakdown.mode})`),
        ["title", "skill", "seniority", "location"].map((part) => h("div", { class: "row small" },
          h("span", { style: { width: "80px" } }, part), h("strong", {}, `${d.score_breakdown[part].points} pts`)))) : null,
      h("div", { class: "sec" }, h("h4", {}, "Description"), h("pre", {}, (jd && jd.text) || d.description || "No description stored.")));
  }

  /* --- tailoring (the only paid action) ------------------------------------------------------------- */

  async function tailor(job) {
    if (job.status === "tailored") { viewCv(job); return; }
    const estimate = await Forge.api(`/api/promote/estimate?key=${encodeURIComponent(job.key)}`);
    if (!estimate.ok) { Forge.toast(Forge.detail(estimate), "error", 8000); return; }
    const e = estimate.data;
    if (e.reused) { runPromote(job, true); return; }
    const dialog = Forge.modal({
      title: "Tailor your CV for this job?",
      body: h("div", { class: "stack" },
        h("div", {}, h("strong", {}, job.title), ` at ${job.company}`),
        h("div", { class: "muted" }, "Claude rewrites only your headline, summary and bullet wording. Everything is checked by code against your base CV."),
        h("div", { class: "card" },
          h("div", {}, `Model: ${e.model}`),
          h("div", {}, `Input: about ${Forge.number(e.input_tokens)} tokens (${e.basis})`),
          h("div", {}, `Output: about ${Forge.number(e.output_tokens_estimate)} tokens`),
          h("div", {}, h("strong", {}, `Estimated cost: ${Forge.money(e.cost_usd, e.cost_disabled)}`)),
          h("div", { class: "faint small" }, e.cost_disabled ? (e.note || "") : `Worst case ${Forge.money(e.max_cost_usd)}. ${e.note || ""}`),
          e.monthly_cap_usd ? h("div", { class: "faint small" }, `Monthly cap: $${e.monthly_cap_usd}`) : null)),
      actions: [h("button", { class: "btn", onclick: () => dialog.close() }, "Cancel"),
        h("button", { class: "btn primary", id: "confirm-tailor", onclick: () => { dialog.close(); runPromote(job, true); } }, "Confirm and tailor")],
    });
  }

  async function runPromote(job, confirm) {
    const progress = Forge.modal({ title: "Tailoring...", body: h("div", {}, h("span", { class: "spinner" }), "This can take up to a minute. Please keep this page open."), actions: [h("span")] });
    const result = await Forge.api("/api/promote", { method: "POST", json: { key: job.key, confirm_cost: confirm } });
    progress.close();
    if (!result.ok) { Forge.toast(Forge.detail(result), "error", 9000); return; }
    flashKey = job.key;
    loadJobs();
    showCv(job, result.data);
  }

  async function viewCv(job) {
    const result = await Forge.api(`/api/tailored?key=${encodeURIComponent(job.key)}`);
    if (!result.ok) { Forge.toast(Forge.detail(result), "error"); return; }
    showCv(job, result.data);
  }

  function showCv(job, data) {
    const cv = data.cv;
    const v = data.validation || {};
    const dialog = Forge.modal({
      title: `Tailored CV · ${job.company}`, wide: true,
      body: h("div", { class: "stack cv-preview" },
        data.warning ? h("div", { class: "notice bad" }, h("strong", {}, "Validation failed. "), data.warning,
          h("ul", {}, (v.violations || []).map((x) => h("li", {}, `${x.rule}: "${x.text}" (${x.where})`)))) : h("div", { class: "notice good" },
          data.reused ? "Reused from earlier: no cost." : "Passed validation: every skill, term and number comes from your base CV."),
        cvBlock(cv)),
      actions: [h("a", { class: "btn", href: `/api/cv-docx?key=${encodeURIComponent(job.key)}`, download: "" }, "Download .docx"),
        data.has_pdf ? h("a", { class: "btn", href: `/api/cv-pdf?key=${encodeURIComponent(job.key)}`, download: "" }, "Download .pdf") : null,
        h("button", { class: "btn primary", onclick: () => dialog.close() }, "Close")],
    });
  }

  function cvBlock(cv) { return Forge.cvPreview(cv); }

  /* --- page ----------------------------------------------------------------------------------------- */

  Forge.pages.jobs = {
    title: "Jobs",
    mount(el) {
      container = el;
      el.append(h("div", { class: "stack" }, h("div", { id: "scan-slot" }), pasteCard(),
        h("div", { class: "card" }, h("h2", {}, "Ranked jobs"), filterBar(), h("div", { id: "jobs-slot" }, h("div", { class: "empty" }, h("span", { class: "spinner" }), "Loading")))));
      wasActive = Forge.runStatus.active;
      renderScan();
      loadJobs();
      Forge.refreshRun();
      stopListening = Forge.onRun((status) => {
        renderScan();
        if (status.active || wasActive) loadJobs();  // refresh the table while a scan runs and once it ends
        wasActive = status.active;
      });
    },
    unmount() { clearTimeout(searchTimer); if (stopListening) stopListening(); container = null; },
  };
})();
