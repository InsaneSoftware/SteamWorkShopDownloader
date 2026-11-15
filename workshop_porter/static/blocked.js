// Countdown on the "You are blocked" page.

(() => {
  const card = document.querySelector("#blockedCard");
  const expires = card && card.dataset.expires;
  if (!expires) return;
  const end = new Date(expires).getTime();
  if (Number.isNaN(end)) return;

  const until = document.querySelector("#blockedUntil");
  until.textContent = `until ${new Date(end).toLocaleString([], {
    weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  })} (your time)`;

  const pad = (value) => String(value).padStart(2, "0");
  let timer = null;

  function tick() {
    const left = Math.max(0, Math.ceil((end - Date.now()) / 1000));
    document.querySelector("#cdHours").textContent = pad(Math.floor(left / 3600));
    document.querySelector("#cdMinutes").textContent = pad(Math.floor((left % 3600) / 60));
    document.querySelector("#cdSeconds").textContent = pad(left % 60);
    if (left > 0) return;
    window.clearInterval(timer);
    document.querySelector("#blockedTimer").hidden = true;
    document.querySelector("#blockedDone").hidden = false;
    // Give the server a moment, then try the page again.
    window.setTimeout(() => window.location.reload(), 3000);
  }

  tick();
  timer = window.setInterval(tick, 1000);
})();
