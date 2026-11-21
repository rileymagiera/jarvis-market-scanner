/* Jarvis instructional-panel preferences: browser-only, keyed per tab.
 * Does not alter scanner state, paper positions, or remote settings.
 */
(function () {
  "use strict";
  const STORAGE_KEY = "jarvisHelpPanelsV1";

  function initJarvisHelpPanels(root, storage) {
    const target = root || document;
    const panels = Array.from(target.querySelectorAll('details[data-jarvis-help]'));
    let store = storage;
    if (store === undefined) {
      try { store = window.localStorage; } catch (_) { store = null; }
    }
    let saved = {};
    try {
      const parsed = JSON.parse(store ? (store.getItem(STORAGE_KEY) || "{}") : "{}");
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) saved = parsed;
    } catch (_) {}
    panels.forEach(function (panel) {
      const key = String(panel.dataset.jarvisHelp || "");
      if (!key) return;
      if (typeof saved[key] === "boolean") panel.open = saved[key];
      panel.addEventListener("toggle", function () {
        saved[key] = panel.open === true;
        try { if (store) store.setItem(STORAGE_KEY, JSON.stringify(saved)); } catch (_) {}
      });
    });
    return panels.length;
  }

  // Expose only to make pure-browser behavior testable; no trading actions.
  window.initJarvisHelpPanels = initJarvisHelpPanels;
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      initJarvisHelpPanels(document);
    }, { once: true });
  } else {
    initJarvisHelpPanels(document);
  }
})();
