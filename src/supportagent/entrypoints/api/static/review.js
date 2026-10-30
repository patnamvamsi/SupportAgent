// Keyboard review for /ui/onboarding/graph (ADR-0015 decision 4). Served from /static (CSP: script-src 'self').
// j/k: next/previous fact · c: confirm · r: reject · x: select for a bulk decision.
(function () {
  const cards = Array.from(document.querySelectorAll("article.card"));
  if (!cards.length) return;
  let at = Math.max(0, cards.findIndex((c) => "#" + c.id === location.hash));
  function show(i) {
    cards.forEach((c) => c.classList.remove("active"));
    at = Math.min(Math.max(i, 0), cards.length - 1);
    cards[at].classList.add("active");
    cards[at].focus({ preventScroll: true });
    cards[at].scrollIntoView({ block: "nearest" });
  }
  document.addEventListener("keydown", (e) => {
    if (e.target.closest("input, select, textarea") || e.metaKey || e.ctrlKey || e.altKey) return;
    const card = cards[at];
    if (e.key === "j") show(at + 1);
    else if (e.key === "k") show(at - 1);
    else if (e.key === "x") { const box = card.querySelector("input[type=checkbox]"); box.checked = !box.checked; }
    else if (e.key === "c" || e.key === "r") {
      const next = cards[at + 1];
      const back = document.querySelector("input[name=back]");
      if (back && next) back.value = back.value.split("#")[0] + "#" + next.id;
      card.querySelector("button[data-key=" + e.key + "]").click();
    } else return;
    e.preventDefault();
  });
  show(at);
})();
