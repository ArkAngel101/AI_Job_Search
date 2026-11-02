"use strict";
/* Settings: profile and targeting, search, scoring, sources, boards.yaml, Claude, system status. */

(() => {
  const SENIORITY = ["entry", "mid", "senior", "lead", "principal", "manager", "any"];
  const PROFILE_FIELDS = ["seniority", "titles", "keywords", "scoring_skills", "first_cut", "final", "pages", "max_age_days", "results_per_scan", "enabled_sources"];
  const NUMBER_LIMITS = { pages: [1, 5], max_age_days: [1, 365], results_per_scan: [10, 300] };

  let container = null;
  let saved = null;   // what the server has
  let draft = null;   // what the form shows
  let boardsText = "";

  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const clone = (v) => JSON.parse(JSON.stringify(v));

  function makeDraft(settings) {
    return {
      profile: clone(settings.profile),
      prices_in: settings.claude.prices ? String(settings.claude.prices.input_per_mtok) : "",
      prices_out: settings.claude.prices ? String(settings.claude.prices.output_per_mtok) : "",
      cap: settings.claude.monthly_cap_usd ? String(settings.claude.monthly_cap_usd) : "",
      cost_disabled: !!settings.claude.cost_limits_disabled,
    };
  }

  /* --- what changed, and whether it can be saved ------------------------------------------------ */

  function changes() {
    const profile = {};
    for (const key of PROFILE_FIELDS) if (!same(draft.profile[key], saved.profile[key])) profile[key] = draft.profile[key];
    const claude = {};
    const before = makeDraft(saved);
    if (draft.prices_in !== before.prices_in || draft.prices_out !== before.prices_out) {
      claude.prices = draft.prices_in === "" && draft.prices_out === "" ? null
        : { input_per_mtok: Number(draft.prices_in), output_per_mtok: Number(draft.prices_out) };
    }
    if (draft.cap !== before.cap) claude.monthly_cap_usd = draft.cap === "" ? null : Number(draft.cap);
    if (draft.cost_disabled !== before.cost_disabled) claude.cost_limits_disabled = draft.cost_disabled;
    const body = {};
    if (Object.keys(profile).length) body.profile = profile;
    if (Object.keys(claude).length) body.claude = claude;
    return body;
  }

  function problem() {
    const p = draft.profile;
    if (!p.enabled_sources.length) return "Turn on at least one source.";
    if (!p.titles.length) return "Add at least one target title.";
    if (!p.keywords.length) return "Add at least one search keyword.";
    if (draft.cost_disabled) return null;  // prices and the cap are switched off, so they are not checked
    const priceIn = draft.prices_in.trim(), priceOut = draft.prices_out.trim();
    if ((priceIn === "") !== (priceOut === "")) return "Enter both token prices, or clear both.";
    for (const v of [priceIn, priceOut, draft.cap.trim()]) if (v !== "" && !(Number(v) >= 0)) return "Prices and the cap must be numbers (0 or more).";
    return null;
  }

  function refreshBar() {
    const body = changes();
    const dirty = Object.keys(body).length > 0;
    const issue = problem();
    const bar = $("#save-bar");
    if (!bar) return;
    Forge.clear(bar).append(
      issue ? h("span", { class: "pill warn", id: "save-issue" }, issue) : null,
      dirty ? h("span", { class: "pill info", id: "dirty-flag" }, "Unsaved changes") : h("span", { class: "faint small" }, "All changes saved"),
      h("button", { class: "btn", id: "discard-btn", disabled: !dirty, onclick: () => { draft = makeDraft(saved); render(); } }, "Discard"),
      h("button", { class: "btn primary", id: "save-btn", disabled: !dirty || !!issue, onclick: save }, "Save changes"));
  }

  async function save() {
    const result = await Forge.api("/api/settings", { method: "POST", json: changes() });
    if (!result.ok) { Forge.toast(Forge.detail(result), "error", 8000); return; }
    saved = result.data;
    draft = makeDraft(saved);
    Forge.toast("Settings saved. The next scan uses them.");
    render();
  }

  async function resetField(key) {
    const result = await Forge.api("/api/settings", { method: "POST", json: { reset: [key] } });
    if (!result.ok) { Forge.toast(Forge.detail(result), "error"); return; }
    saved = result.data;
    draft.profile[key] = clone(saved.profile[key]);
    Forge.toast("Back to the default (including suggestions from your CV).");
    render();
  }

  /* --- form pieces ------------------------------------------------------------------------------------ */

  function field(label, hint, ...content) {
    return h("div", { class: "field" }, h("span", { class: "lab" }, label), content, hint ? h("div", { class: "hint" }, hint) : null);
  }

  function chipsEditor(key, label, hint, placeholder) {
    const list = draft.profile[key];
    const input = h("input", { type: "text", placeholder, "aria-label": `Add to ${label}`, dataset: { chips: key },
      onkeydown: (e) => {
        if ((e.key === "Enter" || e.key === ",") && input.value.trim()) {
          e.preventDefault();
          const value = input.value.trim().replace(/,$/, "");
          if (value && !list.some((x) => x.toLowerCase() === value.toLowerCase())) list.push(value);
          input.value = "";
          renderChips();
          refreshBar();
        }
      } });
    const box = h("div", { class: "chips-edit", dataset: { field: key } });
    const renderChips = () => {
      Forge.clear(box).append(...list.map((item, i) => h("span", { class: "chip" }, item,
        h("button", { type: "button", "aria-label": `Remove ${item}`, onclick: () => { list.splice(i, 1); renderChips(); refreshBar(); } }, "×"))), input);
    };
    renderChips();
    const customized = saved.customized.includes(key);
    return h("div", { class: "field" },
      h("div", { class: "row", style: { justifyContent: "space-between" } }, h("span", { class: "lab" }, label),
        customized ? h("button", { class: "btn small", type: "button", onclick: () => resetField(key), title: "Go back to the default list" }, "Reset to default") : null),
      box, h("div", { class: "hint" }, hint));
  }

  function segmented() {
    const wrap = h("div", { class: "seg", id: "seniority", role: "group", "aria-label": "Seniority" });
    const draw = () => Forge.clear(wrap).append(...SENIORITY.map((level) => h("button", {
      type: "button", class: level === draft.profile.seniority ? "on" : "", dataset: { level },
      onclick: () => { draft.profile.seniority = level; draw(); refreshBar(); } }, level)));
    draw();
    return wrap;
  }

  function slider(key, label) {
    const out = h("strong", { id: `${key}-value` }, String(draft.profile[key]));
    const input = h("input", { type: "range", id: `${key}-range`, min: "0", max: "100", step: "1", value: String(draft.profile[key]), "aria-label": label,
      oninput: () => {
        let value = Number(input.value);
        const p = draft.profile;
        if (key === "first_cut" && value > p.final) p.final = value;     // the final threshold never sits below the first cut
        if (key === "final" && value < p.first_cut) value = p.first_cut;
        p[key] = value;
        input.value = String(value);
        $("#first_cut-value").textContent = String(p.first_cut); $("#final-value").textContent = String(p.final);
        $("#first_cut-range").value = String(p.first_cut); $("#final-range").value = String(p.final);
        refreshBar();
      } });
    return field(label, null, h("div", { class: "row" }, input, out));
  }

  function numberField(key, label, hint) {
    const [lo, hi] = NUMBER_LIMITS[key];
    const input = h("input", { type: "number", id: `num-${key}`, min: String(lo), max: String(hi), value: String(draft.profile[key]), "aria-label": label,
      style: { width: "120px" },
      onchange: () => {
        const parsed = Math.round(Number(input.value));
        draft.profile[key] = Number.isFinite(parsed) ? Math.max(lo, Math.min(hi, parsed)) : saved.profile[key];
        input.value = String(draft.profile[key]);
        refreshBar();
      } });
    return field(label, `${hint} (${lo}-${hi})`, input);
  }

  /* --- cards -------------------------------------------------------------------------------------------- */

  function profileCard() {
    return h("div", { class: "card" }, h("h2", {}, "Profile and targeting"),
      field("Seniority", "Entry roles and internships get full credit when this is \"entry\". \"any\" turns the level preference off.", segmented()),
      chipsEditor("titles", "Target titles", "Press Enter to add. Jobs whose titles match these score higher.", "e.g. Machine Learning Engineer"),
      chipsEditor("scoring_skills", "Skills used for scoring", "Skills from your CV are added automatically until you edit this list.", "e.g. PyTorch"));
  }

  function searchCard() {
    return h("div", { class: "card" }, h("h2", {}, "Search"),
      chipsEditor("keywords", "Search keywords", "One or two words each. Used by keyword-search sources such as Adzuna.", "e.g. machine learning"),
      h("div", { class: "row", style: { alignItems: "flex-start", gap: "24px" } },
        numberField("pages", "Pages per keyword", "How many pages of newest results"),
        numberField("max_age_days", "Maximum posting age (days)", "Older postings are ignored"),
        numberField("results_per_scan", "Results per scan", "Best-scoring jobs kept each scan")));
  }

  function scoringCard() {
    return h("div", { class: "card" }, h("h2", {}, "Scoring thresholds"),
      slider("first_cut", "First cut: jobs below this score are dropped"),
      slider("final", "Final: strong matches (green)"),
      h("div", { class: "hint faint small" }, "Changes apply from the next scan."));
  }

  function sourcesCard() {
    const rows = saved.sources.map((source) => {
      const toggle = h("input", { type: "checkbox", id: `src-${source.name}`, checked: draft.profile.enabled_sources.includes(source.name), "aria-label": `Use ${source.label}`,
        onchange: () => {
          const on = new Set(draft.profile.enabled_sources);
          if (toggle.checked) on.add(source.name); else on.delete(source.name);
          draft.profile.enabled_sources = saved.sources.map((s) => s.name).filter((n) => on.has(n));
          refreshBar();
        } });
      return h("div", { class: "toggle-row" },
        h("label", { class: "check" }, toggle, h("strong", {}, source.label), h("span", { class: "faint small" }, source.kind)),
        h("div", { class: "row" },
          source.min_hours_between_fetches ? h("span", { class: "faint small" }, `at most every ${source.min_hours_between_fetches} h`) : null,
          source.needs_key ? h("span", { class: `pill ${source.key_set ? "good" : "warn"}`, dataset: { key: source.name } },
            source.key_set ? "key set" : `key not set (${source.key_names.join(", ")})`) : h("span", { class: "pill dim" }, "no key needed")));
    });
    return h("div", { class: "card" }, h("h2", {}, "Sources"), rows,
      h("div", { class: "hint faint small", style: { marginTop: "8px" } }, "Keys live in .env, never here. This page only shows whether a key is set."));
  }

  function boardsCard() {
    const area = h("textarea", { class: "mono", id: "boards-yaml", spellcheck: "false", "aria-label": "boards.yaml",
      oninput: () => { boardsText = area.value; saveBtn.disabled = boardsText === saved.boards_yaml; } });
    area.value = boardsText;
    const message = h("div", { id: "boards-message", class: "small", style: { marginTop: "8px" } });
    const saveBtn = h("button", { class: "btn primary", id: "boards-save", disabled: boardsText === saved.boards_yaml, onclick: async () => {
      const result = await Forge.api("/api/boards", { method: "POST", json: { yaml: boardsText } });
      Forge.clear(message);
      if (!result.ok) { message.append(h("span", { class: "notice bad" }, Forge.detail(result))); return; }
      saved.boards_yaml = boardsText.endsWith("\n") ? boardsText : `${boardsText}\n`;
      boardsText = saved.boards_yaml;
      message.append(h("span", { class: "pill good" }, "Saved"), " ", Object.entries(result.data.boards).map(([a, n]) => `${a}: ${n}`).join(", "));
      saveBtn.disabled = true;
      area.value = boardsText;
    } }, "Save boards");
    const ats = h("select", { id: "check-ats", "aria-label": "ATS" }, ["greenhouse", "lever", "ashby", "workable"].map((a) => h("option", { value: a }, a)));
    const slug = h("input", { type: "text", id: "check-slug", placeholder: "company slug", "aria-label": "Company slug", style: { maxWidth: "200px" } });
    const result = h("span", { id: "check-result", class: "small" });
    const check = h("button", { class: "btn", id: "check-btn", onclick: async () => {
      if (!slug.value.trim()) return;
      Forge.clear(result).append(h("span", { class: "spinner" }), "Checking...");
      const r = await Forge.api("/api/boards/check", { method: "POST", json: { ats: ats.value, slug: slug.value.trim() } });
      Forge.clear(result).append(r.ok ? h("span", { class: `pill ${r.data.ok ? "good" : "bad"}` }, r.data.detail) : h("span", { class: "pill bad" }, Forge.detail(r)));
    } }, "Check slug");
    return h("div", { class: "card" }, h("h2", {}, "Company boards"),
      h("div", { class: "hint faint small", style: { marginBottom: "8px" } }, "One list per ATS (greenhouse, lever, ashby, workable). Each entry is a slug or {slug, name}. The shipped examples are unverified: use Check slug."),
      area, h("div", { class: "row", style: { marginTop: "8px" } }, saveBtn), message,
      h("div", { class: "row", style: { marginTop: "12px" } }, ats, slug, check, result));
  }

  function claudeCard() {
    const c = saved.claude;
    const bind = (key, id, label, placeholder) => h("input", { type: "number", id, min: "0", step: "any", value: draft[key], placeholder, "aria-label": label,
      disabled: draft.cost_disabled,
      oninput: (e) => { draft[key] = e.target.value; refreshBar(); } });
    return h("div", { class: "card" }, h("h2", {}, "Claude"),
      field("Model", c.model_from_env ? "Set by CLAUDE_MODEL in .env." : "The default. Set CLAUDE_MODEL in .env to change it.", h("code", { id: "claude-model" }, c.model)),
      field("API key", "Set ANTHROPIC_API_KEY in .env. It is never shown here.", h("span", { class: `pill ${c.key_set ? "good" : "warn"}`, id: "claude-key" }, c.key_set ? "key set" : "key not set")),
      field("Cost limits", "Tick this if you have no per-token cost limit (for example a plan without one). Cost estimates and the spend cap are switched off; the values below are kept and come back when you untick it.",
        h("label", { class: "check" }, h("input", { type: "checkbox", id: "cost-disabled", checked: draft.cost_disabled,
          onchange: (e) => { draft.cost_disabled = e.target.checked; render(); } }), "Not applicable: disable cost tracking and the cap")),
      h("div", { class: "row", style: { alignItems: "flex-start", gap: "16px" } },
        field("Input price ($ per million tokens)", null, bind("prices_in", "price-in", "Input price", "e.g. 2.00")),
        field("Output price ($ per million tokens)", null, bind("prices_out", "price-out", "Output price", "e.g. 10.00"))),
      h("div", { class: "hint faint small", style: { margin: "-6px 0 12px" } }, "Copy these from the Anthropic Console. They are used for cost estimates and the cap."),
      field("Monthly spend cap ($)", "Hard stop: a paid call that would pass the cap is refused. Leave empty for no cap. Needs prices above.", bind("cap", "monthly-cap", "Monthly cap", "no cap")),
      h("div", { class: "kv row", style: { justifyContent: "space-between" } }, h("span", { class: "muted" }, "Spent this month"),
        h("strong", { id: "spent" }, `$${Number(c.spent_this_month_usd).toFixed(2)}`)));
  }

  function systemCard() {
    const s = saved.system;
    const row = (k, v) => h("div", { class: "toggle-row" }, h("span", { class: "muted" }, k), h("span", {}, v));
    return h("div", { class: "card" }, h("h2", {}, "System"),
      row("Data folder", s.data_dir), row("Database", `${(s.database_bytes / 1024).toFixed(0)} KB`),
      row("Jobs remembered", Forge.number(s.jobs)), row("Runs", Forge.number(s.runs)), row("Activity entries", Forge.number(s.activity_entries)),
      row("Listening on", s.listening_on), row("Scheduler", s.scheduler), row("Python", s.python));
  }

  /* --- page ------------------------------------------------------------------------------------------------ */

  function render() {
    if (!container) return;
    boardsText = boardsText || saved.boards_yaml;
    Forge.clear(container).append(h("div", { class: "stack" },
      h("div", { class: "grid settings-grid" }, h("div", { class: "stack" }, profileCard(), scoringCard(), sourcesCard()),
        h("div", { class: "stack" }, searchCard(), claudeCard(), boardsCard(), systemCard())),
      h("div", { class: "save-bar card", id: "save-bar" })));
    refreshBar();
  }

  async function load() {
    const result = await Forge.api("/api/settings");
    if (!container) return;
    if (!result.ok) { Forge.clear(container).append(h("div", { class: "notice bad" }, Forge.detail(result))); return; }
    saved = result.data;
    draft = makeDraft(saved);
    boardsText = saved.boards_yaml;
    render();
  }

  Forge.pages.settings = {
    title: "Settings",
    mount(el) { container = el; el.append(h("div", { class: "empty" }, h("span", { class: "spinner" }), "Loading")); load(); },
    unmount() { container = null; },
  };
})();
