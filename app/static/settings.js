"use strict";
/* Settings page (completed in Phase 10). */

(() => {
  Forge.pages.settings = {
    title: "Settings",
    mount(el) { el.append(h("div", { class: "card" }, h("div", { class: "empty" }, "Settings are coming next."))); },
    unmount() {},
  };
})();
