"use strict";
/* Core helpers shared by every page. External text is only ever inserted with textContent / text nodes. */

const $ = (selector, root = document) => root.querySelector(selector);

/* Element.append() would print "null" or "[object HTMLElement]" for empty values and arrays.
   Make it skip null/undefined/false and flatten arrays, so conditional children are safe everywhere. */
const nativeAppend = Element.prototype.append;
Element.prototype.append = function append(...nodes) {
  nativeAppend.apply(this, nodes.flat(Infinity).filter((node) => node !== null && node !== undefined && node !== false));
};
const Forge = { pages: {}, runStatus: { active: false, run: null, sources: {} }, runListeners: new Set() };

/** Create an element. Children are text nodes or elements; text is never parsed as HTML. */
Forge.h = function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "style") Object.assign(el.style, value);
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2).toLowerCase(), value);
    else if (["value", "checked", "disabled", "selected", "readOnly"].includes(key)) el[key] = value;
    else el.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === undefined || child === null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
};
const h = Forge.h;

/** Only http(s) links are ever rendered as links. */
Forge.safeUrl = (url) => (typeof url === "string" && /^https?:\/\//i.test(url) ? url : null);

Forge.clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); return el; };

/** Call the API. Resolves {ok, status, data}; never throws. Writes carry the required X-Forge-Request header. */
Forge.api = async function api(path, { method = "GET", json, body, headers = {} } = {}) {
  const init = { method, headers: { ...headers } };
  if (method !== "GET") init.headers["X-Forge-Request"] = "1";
  if (json !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(json); }
  if (body !== undefined) init.body = body;
  try {
    const response = await fetch(path, init);
    let data = null;
    try { data = await response.json(); } catch { data = null; }
    return { ok: response.ok, status: response.status, data };
  } catch {
    return { ok: false, status: 0, data: { detail: "Could not reach the app. Is it still running?" } };
  }
};

Forge.detail = (result) => {
  const d = result.data && result.data.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d) && d.length) return d.map((e) => e.msg || "invalid input").join("; ");
  return result.status ? `Request failed (${result.status})` : "Could not reach the app.";
};

Forge.money = (usd) => (usd === null || usd === undefined ? "unknown (set token prices in Settings)" : `$${usd < 0.01 ? usd.toFixed(4) : usd.toFixed(2)}`);
Forge.number = (n) => (n === null || n === undefined ? "–" : Number(n).toLocaleString());
Forge.time = (iso) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
};
Forge.ago = (iso) => {
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (!Number.isFinite(seconds)) return "never";
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return `${Math.floor(seconds / 86400)} d ago`;
};

/* --- toasts and overlays --------------------------------------------------------- */

Forge.toast = (message, kind = "ok", ms = 5000) => {
  const el = h("div", { class: `toast ${kind}`, role: "status" }, message);
  $("#toasts").append(el);
  setTimeout(() => el.remove(), ms);
};

/** A centered dialog. Returns {close, body}. `actions` are buttons for the footer. */
Forge.modal = ({ title, body, actions = [], wide = false }) => {
  const root = $("#overlay-root");
  const close = () => { scrim.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = (event) => { if (event.key === "Escape") close(); };
  const bodyEl = h("div", {}, body);
  const scrim = h("div", { class: "scrim", onclick: (e) => { if (e.target === scrim) close(); } },
    h("div", { class: `modal${wide ? " wide" : ""}`, role: "dialog", "aria-modal": "true", "aria-label": title },
      h("h2", {}, title), bodyEl,
      h("div", { class: "foot" }, actions.length ? actions : h("button", { class: "btn", onclick: close }, "Close"))));
  document.addEventListener("keydown", onKey);
  root.append(scrim);
  return { close, body: bodyEl };
};

/** A right-hand drawer. Returns {close, body}. */
Forge.drawer = ({ title, body }) => {
  const root = $("#overlay-root");
  const close = () => { scrim.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = (event) => { if (event.key === "Escape") close(); };
  const bodyEl = h("div", {}, body);
  const scrim = h("div", { class: "drawer-scrim", onclick: (e) => { if (e.target === scrim) close(); } },
    h("aside", { class: "drawer", role: "dialog", "aria-label": title },
      h("div", { class: "row", style: { justifyContent: "space-between" } }, h("h2", {}, title),
        h("button", { class: "btn small", onclick: close }, "Close")), bodyEl));
  document.addEventListener("keydown", onKey);
  root.append(scrim);
  return { close, body: bodyEl };
};

/* --- run status (the pill) and polling while a run is active --------------------------- */

let pollTimer = null;

Forge.updatePill = () => {
  const { active, run } = Forge.runStatus;
  const pill = $("#run-pill");
  pill.className = "run-pill";
  if (active) { pill.textContent = "RUNNING..."; pill.classList.add("running"); return; }
  if (!run) { pill.textContent = "NO SCAN YET"; return; }
  if (run.status === "failed") { pill.textContent = "LAST FIND FAILED"; pill.classList.add("failed"); return; }
  pill.textContent = `LAST FIND · ${run.summary && run.summary.matched !== undefined ? run.summary.matched : 0} matched`;
  pill.classList.add("done");
};

/** Fetch the run status once, tell listeners, and keep polling every 2.5 s while a run is active. */
Forge.refreshRun = async () => {
  clearTimeout(pollTimer);
  const result = await Forge.api("/api/run-status");
  if (result.ok) Forge.runStatus = result.data;
  Forge.updatePill();
  for (const listener of Forge.runListeners) listener(Forge.runStatus);
  if (Forge.runStatus.active) pollTimer = setTimeout(Forge.refreshRun, 2500);
};

Forge.onRun = (listener) => { Forge.runListeners.add(listener); return () => Forge.runListeners.delete(listener); };

/* --- router ------------------------------------------------------------------------------ */

Forge.navItems = [["overview", "Overview"], ["jobs", "Jobs"], ["cv", "CV"], ["settings", "Settings"]];
let currentPage = null;

Forge.route = () => {
  const name = location.hash.replace(/^#\/?/, "") || "overview";
  const page = Forge.pages[name] || Forge.pages.overview;
  const key = Forge.pages[name] ? name : "overview";
  if (currentPage && currentPage.unmount) currentPage.unmount();
  currentPage = page;
  $("#page-title").textContent = page.title;
  document.title = `Forge · ${page.title}`;
  for (const link of document.querySelectorAll("#nav a")) link.classList.toggle("active", link.dataset.page === key);
  const view = Forge.clear($("#view"));
  page.mount(view);
  view.focus({ preventScroll: true });
};
