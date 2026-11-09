const rows = document.querySelector("#accountRows");
const emptyState = document.querySelector("#adminEmpty");
const message = document.querySelector("#adminMessage");
let filter = "all";
let accounts = [];

document.querySelectorAll("#filterTabs button").forEach((button) => {
  button.addEventListener("click", () => {
    filter = button.dataset.filter;
    document.querySelectorAll("#filterTabs button").forEach((b) =>
      b.classList.toggle("active", b === button));
    render();
  });
});

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    cache: "no-store",
    headers: {
      "X-Requested-With": "fetch",
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  if (response.status === 403) {
    window.location.href = "/";
    throw new Error("Admin session expired");
  }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Request failed");
  return data;
}

async function load() {
  try {
    const data = await api("/api/admin/accounts");
    accounts = data.accounts;
    renderStats(data);
    renderCoverage(data.games);
    document.querySelector("#intervalNote").textContent =
      `working accounts rechecked every ${data.check_interval_minutes} min`;
    render();
  } catch (error) {
    showMessage(error.message, "error");
  }
}

function renderStats(data) {
  const count = (status) => data.accounts.filter((a) => a.status === status).length;
  document.querySelector("#statWorking").textContent = count("working");
  document.querySelector("#statBroken").textContent = count("broken");
  document.querySelector("#statPending").textContent = count("pending");
  document.querySelector("#statDisabled").textContent = count("disabled");
  const covered = data.games.filter((g) => g.accounts > 0).length;
  document.querySelector("#statGames").textContent = `${covered}/${data.games.length}`;
}

function renderCoverage(games) {
  const body = document.querySelector("#coverageRows");
  body.replaceChildren();
  games
    .slice()
    .sort((a, b) => a.accounts - b.accounts || a.name.localeCompare(b.name))
    .forEach((game) => {
      const row = el("tr", game.accounts ? "" : "unsupported");
      row.append(el("td", "", `${game.name} (${game.app_id})`));
      row.append(el("td", "count", game.accounts ? `${game.accounts} working` : "no account"));
      body.append(row);
    });
}

function render() {
  const visible = accounts.filter((a) => filter === "all" || a.status === filter);
  rows.replaceChildren();
  emptyState.hidden = visible.length > 0;
  visible.forEach((account) => rows.append(renderRow(account)));
}

function renderRow(account) {
  const row = el("tr", `status-${account.status}`);

  const who = el("td");
  who.append(el("div", "account-name", account.display_name));
  who.append(el("div", "mono", account.username));
  if (account.steam_id) {
    const library = account.library_visible === true ? "library public"
      : account.library_visible === false ? "library private" : "library not scanned";
    who.append(el("div", "account-meta", `${account.steam_id} · ${library}`));
  }
  who.append(el("div", "account-meta", `added ${formatTime(account.created_at)}`));
  row.append(who);

  const status = el("td");
  status.append(el("span", `job-status ${account.status}`, account.status));
  if (account.status_reason) status.append(el("div", "reason", account.status_reason));
  if (account.broken_since && account.status === "broken") {
    status.append(el("div", "account-meta", `broken since ${formatTime(account.broken_since)}`));
  }
  if (account.consecutive_failures) {
    status.append(el("div", "account-meta", `${account.consecutive_failures} failed checks in a row`));
  }
  if (account.last_error) {
    const details = el("details", "job-error-wrap visible");
    details.append(el("summary", "", "SteamCMD output"));
    details.append(el("pre", "job-error", account.last_error));
    status.append(details);
  }
  row.append(status);

  row.append(el("td", "games-cell",
    account.games.length ? account.games.map((g) => g.name).join(", ") : "–"));

  const checks = el("td", "checks-cell");
  checks.append(el("div", "", `last: ${formatTime(account.last_checked_at)}`));
  checks.append(el("div", "", `ok: ${formatTime(account.last_ok_at)}`));
  checks.append(el("div", "", `next: ${account.enabled ? formatTime(account.next_check_at) : "paused"}`));
  checks.append(el("div", "", `used: ${formatTime(account.last_used_at)}`));
  row.append(checks);

  const actions = el("td", "actions-col");
  const recheck = el("button", "btn btn-sm btn-outline-secondary", "Recheck now");
  recheck.disabled = !account.enabled;
  recheck.addEventListener("click", () => act(
    `/api/admin/accounts/${account.id}/recheck`, { method: "POST" },
    "Recheck queued. It runs between downloads."));
  const toggle = el("button", "btn btn-sm btn-outline-secondary",
    account.enabled ? "Disable" : "Enable");
  toggle.addEventListener("click", () => act(
    `/api/admin/accounts/${account.id}/enabled`,
    { method: "POST", body: JSON.stringify({ enabled: !account.enabled }) },
    account.enabled ? "Account disabled." : "Account enabled and queued for a check."));
  const remove = el("button", "btn btn-sm btn-outline-danger", "Delete");
  remove.addEventListener("click", () => {
    if (!window.confirm(`Delete "${account.display_name}" and its stored credentials? This cannot be undone.`)) return;
    act(`/api/admin/accounts/${account.id}`, { method: "DELETE" }, "Account deleted.");
  });
  const stack = el("div", "action-stack");
  stack.append(recheck, toggle, remove);
  actions.append(stack);
  row.append(actions);
  return row;
}

async function act(path, options, success) {
  try {
    await api(path, options);
    showMessage(success, "success");
    await load();
  } catch (error) {
    showMessage(error.message, "error");
  }
}

function showMessage(text, style) {
  message.textContent = text;
  message.className = `form-message admin-message ${style}`;
}

function formatTime(value) {
  if (!value) return "never";
  const seconds = Math.round((new Date(value).getTime() - Date.now()) / 1000);
  const formatter = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  for (const [unit, divisor] of [["day", 86400], ["hour", 3600], ["minute", 60]]) {
    if (Math.abs(seconds) >= divisor) return formatter.format(Math.round(seconds / divisor), unit);
  }
  return formatter.format(seconds, "second");
}

load();
window.setInterval(load, 10000);
