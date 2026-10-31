"use strict";
/* Overview: counts, funnel, the four agents, and the activity stream. */

(() => {
  const AGENT_LABEL = { "forge": "Forge", "scout": "Scout", "job-reader": "Job Reader", "cv-adapter": "CV Adapter" };
  let timer = null;
  let stopListening = null;
  let container = null;

  function stat(label, value, sub) {
    return h("div", { class: "card stat" }, h("div", { class: "num" }, Forge.number(value)), h("div", { class: "label" }, label),
      h("div", { class: "sub" }, sub || ""));
  }

  function funnel(counts) {
    const stages = [["Found", counts.found], ["Matched", counts.matched], ["Read", counts.read], ["Tailored", counts.tailored]];
    const top = Math.max(1, ...stages.map(([, n]) => n));
    return h("div", { class: "card" }, h("h2", {}, "Funnel"),
      stages.map(([label, n]) => h("div", { class: "funnel-row" }, h("span", { class: "muted" }, label),
        h("div", { class: "funnel-bar" }, h("div", { class: "funnel-fill", style: { width: `${Math.max(n ? 2 : 0, (100 * n) / top)}%` } })),
        h("strong", {}, Forge.number(n)))));
  }

  function agentCard(agent) {
    const rate = agent.success_rate === null ? "–" : `${agent.success_rate}%`;
    const model = agent.last_model && agent.last_model !== "none" ? agent.last_model : "no LLM";
    return h("div", { class: `card agent agent-${agent.name}${agent.status === "WORKING" ? " working" : ""}`, dataset: { agent: agent.name } },
      h("div", { class: "row", style: { justifyContent: "space-between" } },
        h("span", { class: "name" }, AGENT_LABEL[agent.name] || agent.name),
        h("span", { class: `small status-${agent.status.toLowerCase()}` }, `● ${agent.status}`)),
      h("div", { class: "role" }, agent.role),
      h("div", { class: "kv" }, h("span", {}, "Tasks"), h("strong", {}, Forge.number(agent.tasks))),
      h("div", { class: "kv" }, h("span", {}, "Success"), h("strong", {}, rate)),
      h("div", { class: "kv" }, h("span", {}, "Model"), h("strong", {}, model)),
      h("div", { class: "last" }, agent.last_action ? `${agent.last_action} · ${Forge.ago(agent.last_at)}` : "No activity yet"));
  }

  function streamRow(entry) {
    const name = AGENT_LABEL[entry.agent] || entry.agent;
    const model = entry.model && entry.model !== "none" ? entry.model : "–";
    return h("div", { class: "stream-row" },
      h("span", { class: `agent-pill agent-${entry.agent}` }, name),
      h("span", { class: "ellipsis", title: entry.task }, entry.task),
      h("span", { class: "ellipsis faint", title: model }, model),
      h("span", { class: `pill ${entry.status === "completed" ? "good" : "bad"}` }, entry.status),
      h("span", { class: "faint" }, Forge.time(entry.at)));
  }

  async function load() {
    if (!container) return;
    const [overview, agents, activity] = await Promise.all([
      Forge.api("/api/overview"), Forge.api("/api/agents"), Forge.api("/api/activity?limit=60")]);
    if (!container) return;  // the page was left while loading
    if (!overview.ok || !agents.ok || !activity.ok) {
      Forge.clear(container).append(h("div", { class: "notice bad" }, "Could not load the overview. ", Forge.detail(overview.ok ? (agents.ok ? activity : agents) : overview)));
      return;
    }
    const counts = overview.data;
    Forge.clear(container).append(h("div", { class: "stack" },
      h("div", { class: "grid stats" },
        stat("Found", counts.found, "in the last scan"), stat("Matched", counts.matched), stat("Read", counts.read),
        stat("Tailored", counts.tailored), stat("Known jobs", counts.known, counts.new ? `${counts.new} new in the last run` : "")),
      h("div", { class: "grid", style: { gridTemplateColumns: "minmax(0, 1fr)" } }, funnel(counts)),
      h("div", { class: "grid agents" }, agents.data.map(agentCard)),
      h("div", { class: "card" }, h("h2", {}, "Activity"),
        activity.data.length ? h("div", { class: "stream" }, activity.data.map(streamRow))
          : h("div", { class: "empty" }, "Nothing has happened yet. Open Jobs and press Scan now."))));
    clearTimeout(timer);
    if (agents.data.some((a) => a.status === "WORKING")) timer = setTimeout(load, 3000);
  }

  Forge.pages.overview = {
    title: "Overview",
    mount(el) {
      container = el;
      el.append(h("div", { class: "empty" }, h("span", { class: "spinner" }), "Loading"));
      load();
      stopListening = Forge.onRun(() => load());
    },
    unmount() { clearTimeout(timer); if (stopListening) stopListening(); container = null; },
  };
})();
