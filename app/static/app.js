"use strict";
/* Start the app: build the navigation, the footer credits, and the router. */

(async () => {
  const nav = $("#nav");
  for (const [name, label] of Forge.navItems) nav.append(h("a", { href: `#/${name}`, dataset: { page: name } }, label));
  window.addEventListener("hashchange", Forge.route);

  const credits = await Forge.api("/api/attributions");
  const footer = $("#footer");
  footer.append(h("span", {}, "Job data from public sources:"));
  if (credits.ok) {
    for (const credit of Object.values(credits.data)) {
      const url = Forge.safeUrl(credit.url);
      footer.append(url ? h("a", { href: url, target: "_blank", rel: "noopener" }, credit.text) : h("span", {}, credit.text));
    }
  }
  footer.append(h("span", {}, "Greenhouse, Lever, Ashby, Workable and Adzuna listings link to the original posting."));

  Forge.route();
  Forge.refreshRun();
})();
