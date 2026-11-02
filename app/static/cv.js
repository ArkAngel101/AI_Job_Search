"use strict";
/* CV page: upload, review detected contact details, preview what is sent to the AI model, confirm, build. */

(() => {
  const KINDS = ["name", "email", "phone", "address", "linkedin", "github", "url"];
  let container = null;
  let timer = null;
  let status = null;
  let previewShown = false;

  /** Shared with the Jobs page: render a CV (base or tailored) as read-only blocks. */
  Forge.cvPreview = function cvPreview(cv) {
    const section = (title, ...children) => h("div", { class: "sec" }, h("h4", {}, title), children);
    const c = cv.contact || {};
    const contact = [...(c.addresses || []).slice(0, 1), ...(c.phones || []).slice(0, 1), ...(c.emails || []).slice(0, 1),
      ...(c.linkedin || []), ...(c.github || []), ...(c.urls || [])].join("  |  ");
    return h("div", { class: "cv-preview" },
      h("h3", {}, cv.name), cv.headline ? h("div", { class: "muted" }, cv.headline) : null, contact ? h("div", { class: "faint small" }, contact) : null,
      cv.summary ? section("Summary", h("div", {}, cv.summary)) : null,
      (cv.competencies || []).length ? section("Core competencies", cv.competencies.map((x) => h("span", { class: "chip" }, x))) : null,
      (cv.experience || []).length ? section("Experience", cv.experience.map((job) => h("div", {},
        h("div", {}, h("strong", {}, job.role), ` — ${job.company}`),
        h("div", { class: "faint small" }, [job.location, [job.start, job.end].filter(Boolean).join(" – ")].filter(Boolean).join(" · ")),
        h("ul", {}, (job.bullets || []).map((b) => h("li", {}, b)))))) : null,
      (cv.education || []).length ? section("Education", cv.education.map((s) => h("div", {},
        h("div", {}, h("strong", {}, s.degree), ` — ${s.institution}`),
        h("div", { class: "faint small" }, [s.location, [s.start, s.end].filter(Boolean).join(" – ")].filter(Boolean).join(" · ")),
        (s.details || []).length ? h("ul", {}, s.details.map((d) => h("li", {}, d))) : null))) : null,
      (cv.skills || []).length ? section("Skills", cv.skills.map((g) => h("div", {}, h("strong", {}, `${g.category}: `), g.items.join(", ")))) : null,
      (cv.projects || []).length ? section("Projects", cv.projects.map((p) => h("div", {}, h("strong", {}, p.name),
        p.description ? ` — ${p.description}` : "", (p.bullets || []).length ? h("ul", {}, p.bullets.map((b) => h("li", {}, b))) : null))) : null,
      (cv.languages || []).length ? section("Languages", cv.languages.map((l) => h("span", { class: "chip" }, l.level ? `${l.lang} (${l.level})` : l.lang))) : null);
  };

  /* --- upload ----------------------------------------------------------------------------- */

  async function upload(file) {
    if (!file) return;
    const message = $("#cv-upload-message");
    Forge.clear(message).append(h("span", { class: "spinner" }), `Reading ${file.name}...`);
    const result = await Forge.api(`/api/cv?filename=${encodeURIComponent(file.name)}`, {
      method: "POST", body: file, headers: { "Content-Type": "application/octet-stream" } });
    Forge.clear(message);
    if (!result.ok) { message.append(h("span", { class: "notice bad" }, Forge.detail(result))); return; }
    previewShown = false;
    status = result.data;
    render();
  }

  function dropzone() {
    const input = h("input", { type: "file", id: "cv-file", class: "hidden", accept: ".pdf,.docx,.md,.txt", onchange: (e) => upload(e.target.files[0]) });
    const zone = h("div", { class: "dropzone", id: "cv-drop", tabindex: "0", role: "button", "aria-label": "Upload your CV",
      onclick: () => input.click(), onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") input.click(); },
      ondragover: (e) => { e.preventDefault(); zone.classList.add("over"); },
      ondragleave: () => zone.classList.remove("over"),
      ondrop: (e) => { e.preventDefault(); zone.classList.remove("over"); upload(e.dataTransfer.files[0]); } },
      h("div", { style: { fontWeight: "700", fontSize: "16px", color: "var(--text)" } }, "Drop your CV here, or click to choose a file"),
      h("div", { class: "small", style: { marginTop: "4px" } }, "PDF, DOCX, Markdown or text · up to 5 MB · uploading is free"));
    return h("div", { class: "card" }, h("h2", {}, "Your CV"), zone, input, h("div", { id: "cv-upload-message", class: "small", style: { marginTop: "8px" } }));
  }

  /* --- review contact details --------------------------------------------------------------------- */

  async function correct(change) {
    const result = await Forge.api("/api/cv/contacts", { method: "POST", json: change });
    if (!result.ok) { Forge.toast(Forge.detail(result), "error", 7000); return; }
    status = result.data;
    render();
  }

  function itemRow(item) {
    return h("div", { class: "item", dataset: { kind: item.kind } },
      h("span", { class: "pill info" }, item.kind), h("span", { class: "ellipsis", title: item.value }, item.value),
      h("code", {}, item.placeholder),
      h("button", { class: "btn small danger", title: "Not private: send this as written", onclick: () => correct({ remove: [item.value] }) }, "Remove"));
  }

  function addForm() {
    const kind = h("select", { id: "add-kind", "aria-label": "Kind" }, KINDS.map((k) => h("option", { value: k }, k)));
    const value = h("input", { type: "text", id: "add-value", placeholder: "Text exactly as it appears in your CV", "aria-label": "Text to hide" });
    return h("div", { class: "row", style: { marginTop: "10px" } }, kind, value,
      h("button", { class: "btn", id: "add-item", onclick: () => { if (value.value.trim()) correct({ add: [{ kind: kind.value, value: value.value.trim() }] }); } }, "Hide this too"));
  }

  function nameBox(pending) {
    const input = h("input", { type: "text", id: "name-input", value: pending.name || "", placeholder: "Your full name as written in the CV", "aria-label": "Your name" });
    return h("div", { class: "field" }, h("label", { for: "name-input" }, "Your name (confirm or correct)"),
      h("div", { class: "row" }, input, h("button", { class: "btn", id: "name-save", onclick: () => { if (input.value.trim()) correct({ name: input.value.trim() }); } }, "Save name")),
      pending.name ? h("div", { class: "hint" }, `Detected: ${pending.name}. The AI model only ever sees [NAME].`) :
        h("div", { class: "notice warn" }, "No name was detected. Enter it so it can be kept out of what is sent."));
  }

  /* --- preview, estimate, confirm, build -------------------------------------------------------------- */

  async function togglePreview() {
    previewShown = !previewShown;
    const slot = $("#preview-slot");
    Forge.clear(slot);
    if (!previewShown) return;
    const result = await Forge.api("/api/cv/preview");
    if (!result.ok) { slot.append(h("div", { class: "notice bad" }, Forge.detail(result))); return; }
    const p = result.data;
    const mark = (text) => {  // highlight placeholders without ever parsing the text as HTML
      const pre = h("pre", { class: "sanitized", id: "sanitized-text", tabindex: "0" });
      text.split(/(\[[A-Z]+(?:_\d+)?\])/g).forEach((part) => pre.append(/^\[[A-Z]+(?:_\d+)?\]$/.test(part) ? h("mark", {}, part) : part));
      return pre;
    };
    slot.append(
      h("div", { class: "muted small", style: { margin: "8px 0" } }, `This is exactly the text sent to the AI model (${Forge.number(p.chars)} characters). Highlighted tokens replaced your details.`),
      p.possible_leftovers.length ? h("div", { class: "notice warn", style: { marginBottom: "8px" } }, "These may still be contact details: ", p.possible_leftovers.join(", ")) : null,
      mark(p.text));
  }

  async function showEstimate() {
    const slot = $("#estimate-slot");
    Forge.clear(slot).append(h("span", { class: "spinner" }), "Estimating...");
    const result = await Forge.api("/api/cv/estimate");
    Forge.clear(slot);
    if (!result.ok) { slot.append(h("div", { class: "notice bad" }, Forge.detail(result))); return; }
    const e = result.data;
    slot.append(h("div", { class: "notice", id: "estimate-box" },
      h("div", {}, `Model: ${e.model}`), h("div", {}, `Input about ${Forge.number(e.input_tokens)} tokens (${e.basis}); output about ${Forge.number(e.output_tokens_estimate)} tokens`),
      h("div", {}, h("strong", {}, `Estimated cost: ${Forge.money(e.cost_usd, e.cost_disabled)}`)),
      h("div", { class: "faint small" }, `Spent this month: $${Number(e.spent_this_month_usd).toFixed(2)}${e.monthly_cap_usd ? ` of $${e.monthly_cap_usd}` : ""}`)));
  }

  async function build() {
    const result = await Forge.api("/api/cv/build", { method: "POST", json: { confirm_cost: true, confirm_contacts: true } });
    if (!result.ok) { Forge.toast(Forge.detail(result), "error", 9000); return; }
    await refresh();
  }

  function buildCard(pending) {
    const costOk = h("input", { type: "checkbox", id: "confirm-cost" });
    const contactsOk = h("input", { type: "checkbox", id: "confirm-contacts" });
    const go = h("button", { class: "btn primary", id: "build-btn", disabled: true, onclick: build }, "Build my base CV");
    const sync = () => { go.disabled = !(costOk.checked && contactsOk.checked); };
    costOk.addEventListener("change", sync); contactsOk.addEventListener("change", sync);
    return h("div", { class: "card", id: "pending-card" }, h("h2", {}, `Review before sending: ${pending.filename}`),
      h("div", { class: "muted small", style: { marginBottom: "10px" } },
        "Your name, email, phone, links and street address are replaced with tokens before anything is sent to the AI model (Groq), and put back by this app afterwards."),
      nameBox(pending),
      h("div", { class: "items", id: "contact-items" }, pending.items.length ? pending.items.map(itemRow) : h("div", { class: "empty" }, "No contact details were detected.")),
      addForm(),
      h("div", { class: "row", style: { marginTop: "14px" } },
        h("button", { class: "btn", id: "preview-btn", onclick: togglePreview }, "Preview sanitized text"),
        h("button", { class: "btn", id: "estimate-btn", onclick: showEstimate }, "Estimate cost")),
      h("div", { id: "preview-slot" }), h("div", { id: "estimate-slot", style: { marginTop: "10px" } }),
      h("div", { class: "stack", style: { marginTop: "14px" } },
        h("label", { class: "check" }, costOk, "I have seen the estimated cost"),
        h("label", { class: "check" }, contactsOk, "I checked the detected details and the preview"),
        h("div", {}, go)));
  }

  /* --- page ------------------------------------------------------------------------------------------------- */

  function render() {
    if (!container) return;
    const s = status;
    const stack = [dropzone()];
    if (s.building) stack.push(h("div", { class: "card", id: "building" }, h("span", { class: "spinner" }), "Building your base CV with the AI model. This usually takes under a minute."));
    if (s.error) stack.push(h("div", { class: "notice bad", id: "cv-error" }, s.error, s.has_base ? " Your previous base CV was kept." : ""));
    if (s.pending && !s.building) stack.push(buildCard(s.pending));
    if (s.base_cv) stack.push(h("div", { class: "card" }, h("h2", {}, "Base CV"), Forge.cvPreview(s.base_cv)));
    else if (!s.pending) stack.push(h("div", { class: "card" }, h("div", { class: "empty" }, "No base CV yet. Upload your CV above.")));
    Forge.clear(container).append(h("div", { class: "stack" }, stack));
    if (previewShown && s.pending) { previewShown = false; togglePreview(); }
    clearTimeout(timer);
    if (s.building) timer = setTimeout(refresh, 2500);
  }

  async function refresh() {
    const result = await Forge.api("/api/cv");
    if (!container) return;
    if (!result.ok) { Forge.clear(container).append(h("div", { class: "notice bad" }, Forge.detail(result))); return; }
    status = result.data;
    render();
  }

  Forge.pages.cv = {
    title: "CV",
    mount(el) { container = el; previewShown = false; el.append(h("div", { class: "empty" }, h("span", { class: "spinner" }), "Loading")); refresh(); },
    unmount() { clearTimeout(timer); container = null; },
  };
})();
