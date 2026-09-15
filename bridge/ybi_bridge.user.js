// ==UserScript==
// @name         YBI -> agentic-trader bridge
// @namespace    agentic-trader
// @version      1.4
// @description  Watches YBI web-app alert channels you have open and forwards new messages to the local agent (127.0.0.1 only). v1.4: auto-reloads a stale tab so the feed keeps flowing while you're away.
// @match        https://app.youngbullinvestors.com/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// ==/UserScript==

/* Personal-use bridge: it only reads what your own logged-in session already
   displays, and only talks to localhost. Keep the channel you want mirrored
   open in a pinned tab. TOKEN must match [server].bridge_token in config.toml. */

(function () {
  const PORT = 8787;
  const TOKEN = "ybi-tdt-local";
  const seen = new Set();

  // The app renders no h1/header inside <main>; detect the channel from the
  // first lines of the pane text instead (e.g. "STOCKS DESK / 📈│PREMARKET-ALERTS").
  const channelName = () => {
    const head = (document.querySelector("main")?.innerText || "").slice(0, 300).toLowerCase();
    if (/trading.questions/.test(head)) return "trading-questions";
    if (/premarket/.test(head)) return "premarket-alerts";
    if (/intraday/.test(head)) return "intraday-alerts";
    if (/commentary/.test(head)) return "live-commentary";
    if (/watchlist/.test(head)) return "watchlist";
    if (/targets/.test(head)) return "targets";
    return "unknown";
  };

  function post(text) {
    GM_xmlhttpRequest({
      method: "POST",
      url: `http://127.0.0.1:${PORT}/ingest`,
      headers: { "Content-Type": "application/json" },
      data: JSON.stringify({ token: TOKEN, source: "ybi", channel: channelName(), text }),
    });
  }

  // The three always-on channels (plus watchlist): pin one YBI tab per channel and
  // this script mirrors each. Everything in a watched channel is forwarded — the
  // local parser decides what is a setup, a halt call, mentor trade, or commentary.
  const WATCHED = /watchlist|targets|premarket|intraday|commentary|trading-questions/;

  let lastNew = Date.now();   // last time we saw a message we hadn't seen before

  function harvest() {
    if (!WATCHED.test(channelName())) return;
    // leaf message blocks only (author + time + body); fall back to <p> lines
    let els = [...document.querySelectorAll("main [class*='message']")]
      .filter((el) => !el.querySelector("[class*='message']"));
    if (!els.length) els = [...document.querySelectorAll("main p")];
    for (const el of els) {
      const text = (el.innerText || "").trim();
      if (!text || text.length < 6 || text.length > 2000) continue;
      const key = text.slice(0, 400);
      if (seen.has(key)) continue;
      seen.add(key);
      lastNew = Date.now();
      post(text);
    }
  }

  // v1.4 watchdog: Chrome throttles background tabs and the app's live socket can
  // silently die — the page then LOOKS fine but never receives new messages until
  // a refresh. If nothing new has arrived for 12 min during market-ish hours,
  // reload the tab ourselves (at most every 15 min; the server dedupes re-posts).
  // NOTE: this cannot fire if Chrome fully discards the tab — also add the site to
  // chrome://settings/performance -> "Always keep these sites active".
  let lastReload = Date.now();
  const marketish = () => {
    const et = new Date(new Date().toLocaleString("en-US", { timeZone: "America/New_York" }));
    const m = et.getHours() * 60 + et.getMinutes();
    return et.getDay() > 0 && et.getDay() < 6 && m >= 8 * 60 && m < 16 * 60 + 30;
  };
  setInterval(() => {                       // background tabs still get ~1 tick/min
    if (!marketish()) return;
    if (Date.now() - lastNew > 12 * 60 * 1000 && Date.now() - lastReload > 15 * 60 * 1000) {
      lastReload = Date.now();
      console.log("[agentic-trader] feed stale — reloading tab to reconnect");
      location.reload();
    }
  }, 60 * 1000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) harvest(); });

  let pending = null;
  const mo = new MutationObserver(() => {
    clearTimeout(pending);
    pending = setTimeout(harvest, 800); // debounce bursts, rescan whole pane
  });

  window.addEventListener("load", () => {
    setTimeout(() => {
      harvest();
      mo.observe(document.body, { childList: true, subtree: true });
      console.log("[agentic-trader] YBI bridge v1.4 active (stale-tab watchdog on) ->", `127.0.0.1:${PORT}`);
    }, 3000);
  });
})();
