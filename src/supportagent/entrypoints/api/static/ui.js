// Small enhancements on every page. Served from /static (CSP: script-src 'self').
// The pages work without it: this adds keyboard triage and the replay's filters.
(function () {
  "use strict";
  const typing = (e) => e.target.closest("input, select, textarea") || e.metaKey || e.ctrlKey || e.altKey;
  const store = {
    get(k) { try { return JSON.parse(sessionStorage.getItem(k) || "null"); } catch (_) { return null; } },
    set(k, v) { try { sessionStorage.setItem(k, JSON.stringify(v)); } catch (_) { /* private mode */ } },
  };

  // --- the cases queue: J/K to move, Enter to open; the order is kept for the case page ----
  const queue = document.querySelector("[data-queue]");
  if (queue) {
    const rows = Array.from(queue.querySelectorAll("tbody tr[data-href]"));
    store.set("sa.queue", rows.map((r) => r.dataset.href));
    let at = -1;
    const show = (i) => {
      if (!rows.length) return;
      rows.forEach((r) => r.classList.remove("is-current"));
      at = Math.min(Math.max(i, 0), rows.length - 1);
      rows[at].classList.add("is-current");
      rows[at].scrollIntoView({ block: "nearest" });
    };
    rows.forEach((r, i) => r.addEventListener("click", (e) => {
      if (e.target.closest("a, button")) return;
      location.href = r.dataset.href;
    }));
    document.addEventListener("keydown", (e) => {
      if (typing(e)) return;
      if (e.key === "j") show(at + 1);
      else if (e.key === "k") show(at - 1);
      else if (e.key === "Enter" && at >= 0) location.href = rows[at].dataset.href;
      else return;
      e.preventDefault();
    });
  }

  // --- a case page: J/K walk the queue the person came from ---------------------------------
  const head = document.querySelector("[data-case]");
  const order = store.get("sa.queue");
  if (head && Array.isArray(order)) {
    const here = order.indexOf(location.pathname);
    if (here >= 0) {
      const keys = document.querySelector("[data-case-keys]");
      if (keys) keys.hidden = false;
      document.addEventListener("keydown", (e) => {
        if (typing(e)) return;
        const next = e.key === "j" ? order[here + 1] : e.key === "k" ? order[here - 1] : null;
        if (next) { e.preventDefault(); location.href = next; }
      });
    }
  }

  // --- the replay: bars on one time axis, kind toggles, a text filter, expand a step -------
  const replay = document.querySelector("[data-replay]");
  if (replay) {
    const list = replay.querySelector("ol.steps-list");
    const items = Array.from(list.children);
    const total = Math.max(1, Number(list.dataset.total) || 0, ...items.map((li) => Number(li.dataset.at) + Number(li.dataset.ms)));
    items.forEach((li) => {
      const bar = li.querySelector(".bar i");
      bar.style.setProperty("--x", (100 * Number(li.dataset.at)) / total + "%");
      bar.style.setProperty("--w", (100 * Number(li.dataset.ms)) / total + "%");
      const toggle = () => li.classList.toggle("open");
      li.addEventListener("click", toggle);
      li.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
    });
    const filter = replay.querySelector("[data-replay-filter]");
    const count = replay.querySelector("[data-replay-count]");
    const kinds = Array.from(replay.querySelectorAll("button[data-kind]"));
    const apply = () => {
      const q = filter.value.trim().toLowerCase();
      const off = new Set(kinds.filter((b) => b.getAttribute("aria-pressed") === "false").map((b) => b.dataset.kind));
      let n = 0;
      items.forEach((li) => {
        const hide = off.has(li.dataset.kind) || (q && !li.textContent.toLowerCase().includes(q));
        li.hidden = !!hide;
        if (!hide) n++;
      });
      count.textContent = n === items.length ? n + " steps" : n + " of " + items.length + " steps";
    };
    kinds.forEach((b) => b.addEventListener("click", () => {
      b.setAttribute("aria-pressed", String(b.getAttribute("aria-pressed") === "false"));
      apply();
    }));
    filter.addEventListener("input", apply);
  }
})();
