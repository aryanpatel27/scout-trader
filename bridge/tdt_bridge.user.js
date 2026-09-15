// ==UserScript==
// @name         TDT Software -> agentic-trader bridge
// @namespace    agentic-trader
// @version      1.0
// @description  Watches software.thedailytraders.com signal cards and forwards new ones to the local agent (127.0.0.1 only).
// @match        https://software.thedailytraders.com/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// ==/UserScript==

/* Personal-use bridge for your own TDT membership. Keep /signals (or /home)
   open in a pinned tab. TOKEN must match [server].bridge_token in config.toml. */

(function () {
  const PORT = 8787;
  const TOKEN = "ybi-tdt-local";
  const seen = new Set();

  function post(text) {
    GM_xmlhttpRequest({
      method: "POST",
      url: `http://127.0.0.1:${PORT}/ingest`,
      headers: { "Content-Type": "application/json" },
      data: JSON.stringify({ token: TOKEN, source: "tdt", channel: "signals", text }),
    });
  }

  function harvest() {
    // Signal cards contain "$<strike> · <n>d" plus Call/Put or Bull/Bear badges.
    for (const el of document.querySelectorAll("main div, main article, main li")) {
      const text = (el.innerText || "").replace(/\s+/g, " ").trim();
      if (!text || text.length < 30 || text.length > 400) continue;
      if (!/\$[\d.]+\s*·\s*\d+d/.test(text)) continue;
      if (!/\b(Call|Put|Bull|Bear)\b/.test(text)) continue;
      if (el.querySelector("div, article, li")?.innerText?.length > 25) continue; // want leaf cards
      const key = text.slice(0, 200);
      if (seen.has(key)) continue;
      seen.add(key);
      post(text);
    }
  }

  window.addEventListener("load", () => {
    setTimeout(() => {
      harvest();
      new MutationObserver(() => harvest()).observe(document.body, { childList: true, subtree: true });
      console.log("[agentic-trader] TDT bridge active ->", `127.0.0.1:${PORT}`);
    }, 3000);
  });
})();
