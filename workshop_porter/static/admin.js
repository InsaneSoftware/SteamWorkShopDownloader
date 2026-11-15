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
  const shown = data.games.filter((g) => !g.hidden);
  const covered = shown.filter((g) => g.accounts > 0).length;
  document.querySelector("#statGames").textContent = `${covered}/${shown.length}`;
  document.querySelector("#accountsCount").textContent =
    `${count("working")}/${data.accounts.length}`;
}

function renderCoverage(games) {
  const body = document.querySelector("#coverageRows");
  body.replaceChildren();
  games
    .slice()
    .sort((a, b) => a.accounts - b.accounts || a.name.localeCompare(b.name))
    .forEach((game) => {
      const row = el("tr", game.hidden ? "unsupported game-hidden" : (game.accounts ? "" : "unsupported"));
      const name = el("td");
      const link = el("a", "game-link", `${game.name} (${game.app_id})`);
      link.href = `https://steamcommunity.com/app/${game.app_id}/workshop/`;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.title = "Open its Steam Workshop";
      name.append(link);
      if (game.hidden) name.append(el("span", "hidden-tag", "hidden"));
      row.append(name);
      row.append(el("td", "count", game.accounts ? `${game.accounts} working` : "no account"));
      const actions = el("td", "actions-col");
      const toggle = el("button", "btn btn-sm btn-outline-secondary", game.hidden ? "Show" : "Hide");
      toggle.title = game.hidden
        ? "Show this game on the site again"
        : "Hide this game from the site, the game picker and account lists";
      toggle.addEventListener("click", () => act(
        `/api/admin/games/${game.app_id}/hidden`,
        { method: "POST", body: JSON.stringify({ hidden: !game.hidden }) },
        `${game.name} is ${game.hidden ? "visible again" : "hidden"}.`,
      ));
      actions.append(toggle);
      row.append(actions);
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
  if (account.kind === "public") {
    who.append(el("div", "account-meta", "built-in · SteamCMD anonymous login"));
  } else {
    who.append(el("div", "mono", account.username));
  }
  if (account.steam_id) {
    const library = account.library_visible === true ? "library read from login"
      : account.library_visible === false ? "library not readable" : "library not scanned";
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

  const gamesCell = el("td", "games-cell");
  fillGameList(gamesCell, account.games.map((g) => g.name), 20);
  row.append(gamesCell);

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
  const rename = el("button", "btn btn-sm btn-outline-secondary", "Rename");
  rename.addEventListener("click", () => {
    const name = window.prompt("New public display name (2-32 characters):", account.display_name);
    if (name === null || name.trim() === account.display_name) return;
    act(`/api/admin/accounts/${account.id}/name`,
      { method: "POST", body: JSON.stringify({ display_name: name }) },
      "Account renamed.");
  });
  stack.append(recheck, rename, toggle, remove);
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
  message.className = `form-message admin-message top-message ${style}`;
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

// ----- Views (menu bar) ------------------------------------------------

const VIEWS = ["accounts", "ips", "agents", "log", "blocks"];
const ACTIVITY_VIEWS = ["ips", "agents", "log"];

function currentView() {
  let requested = window.location.hash.slice(1);
  if (requested === "activity") requested = "ips";
  return VIEWS.includes(requested) ? requested : "accounts";
}

function showView() {
  const view = currentView();
  document.querySelectorAll(".view").forEach((section) => {
    section.hidden = section.dataset.view !== view;
  });
  document.querySelectorAll(".menu-link[data-view]").forEach((link) => {
    link.classList.toggle("active", link.dataset.view === view);
  });
  refreshCurrent();
}

function refreshCurrent() {
  const view = currentView();
  if (ACTIVITY_VIEWS.includes(view)) loadActivity();
  if (view === "blocks") loadBlocks();
  if (view === "accounts") load();
}

window.addEventListener("hashchange", showView);

// ----- Activity --------------------------------------------------------

let activityHours = 24;
let ipFilter = "";
const ACTION_LABELS = {
  request: "Requested item",
  download: "Downloaded",
  share: "Shared account",
  bad_password: "Wrong site password",
  admin_login_failed: "Failed admin login",
  blocked: "Blocked visit",
};

document.querySelectorAll(".window-tabs button").forEach((button) => {
  button.addEventListener("click", () => {
    activityHours = Number(button.dataset.hours);
    document.querySelectorAll(".window-tabs button").forEach((b) =>
      b.classList.toggle("active", Number(b.dataset.hours) === activityHours));
    loadActivity();
  });
});

let filterTimer = null;
document.querySelector("#ipFilter").addEventListener("input", (event) => {
  window.clearTimeout(filterTimer);
  filterTimer = window.setTimeout(() => {
    ipFilter = event.target.value.trim();
    loadActivity();
  }, 300);
});

async function loadActivity() {
  try {
    const query = new URLSearchParams({ hours: activityHours });
    if (ipFilter) query.set("ip", ipFilter);
    if (uaFilter !== null) query.set("ua", uaFilter);
    const data = await api(`/api/admin/activity?${query}`);
    document.querySelector("#yourIp").textContent = data.your_ip;
    document.querySelector("#yourUa").textContent = shortUa(data.your_user_agent);
    document.querySelector("#yourUa").title = data.your_user_agent;
    document.querySelector("#uaFilterClear").hidden = uaFilter === null;
    renderUaRows(data.user_agents);
    const ips = data.ips.map((row) => row.ip);
    document.querySelector("#proxyWarning").hidden =
      !(ips.length > 0 && ips.every(isLocalIp));
    renderIpRows(data.ips);
    renderLog(data.events);
  } catch (error) {
    showMessage(error.message, "error");
  }
}

function isLocalIp(ip) {
  return /^(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|::1$|fc|fd|fe80)/i.test(ip);
}

function ipButton(ip, title, onClick) {
  const button = el("button", "link-button mono", ip);
  button.type = "button";
  button.title = title;
  button.addEventListener("click", onClick);
  return button;
}

function renderIpRows(ips) {
  const body = document.querySelector("#ipRows");
  body.replaceChildren();
  document.querySelector("#ipEmpty").hidden = ips.length > 0;
  ips.forEach((row) => {
    const tr = el("tr", row.blocked ? "status-disabled" : "");
    const ipCell = el("td");
    ipCell.append(ipButton(row.ip, "Show this IP in the log", () => setIpFilter(row.ip)));
    if (row.blocked) {
      ipCell.append(el("div", "reason", `blocked ${untilText(row.blocked.expires_at)}`));
    }
    tr.append(ipCell);
    tr.append(el("td", "num", row.requests || "–"));
    tr.append(el("td", row.rejected ? "num warn" : "num", row.rejected || "–"));
    tr.append(el("td", "num strong", row.downloads || "–"));
    tr.append(el("td", "num", row.bytes ? formatBytes(row.bytes) : "–"));
    tr.append(el("td", "num", row.shares || "–"));
    tr.append(el("td", row.bad_passwords ? "num warn" : "num", row.bad_passwords || "–"));
    tr.append(el("td", row.user_agents > 3 ? "num warn" : "num", row.user_agents || "–"));
    tr.append(el("td", "checks-cell", formatTime(row.last_seen)));
    const actions = el("td", "actions-col");
    if (isLocalIp(row.ip)) {
      actions.append(el("span", "account-meta", "local"));
    } else if (row.blocked) {
      const unblock = el("button", "btn btn-sm btn-outline-secondary", "Unblock");
      unblock.addEventListener("click", () => removeBlock(row.blocked.network));
      actions.append(unblock);
    } else {
      const block = el("button", "btn btn-sm btn-outline-danger", "Block");
      block.addEventListener("click", () => openBlockForm(row.ip));
      actions.append(block);
    }
    tr.append(actions);
    body.append(tr);
  });
}

function renderLog(events) {
  const body = document.querySelector("#logRows");
  body.replaceChildren();
  document.querySelector("#logEmpty").hidden = events.length > 0;
  events.forEach((event) => {
    const tr = el("tr", `log-${event.status}`);
    tr.append(el("td", "checks-cell", formatClock(event.at)));
    const ipCell = el("td");
    ipCell.append(ipButton(event.ip, "Show only this IP", () => setIpFilter(event.ip)));
    tr.append(ipCell);
    const uaCell = el("td", "ua-cell");
    uaCell.append(uaButton(event.user_agent || ""));
    tr.append(uaCell);
    tr.append(el("td", "", ACTION_LABELS[event.action] || event.action));
    tr.append(el("td", "log-status", event.status.replace("_", " ")));
    const detail = event.bytes ? `${event.detail} · ${formatBytes(event.bytes)}` : event.detail;
    tr.append(el("td", "log-detail", detail));
    body.append(tr);
  });
}

let uaFilter = null;

document.querySelector("#uaFilterClear").addEventListener("click", () => {
  uaFilter = null;
  loadActivity();
});

function shortUa(ua) {
  if (!ua) return "(no user agent)";
  const browsers = [
    [/Edg\/(\d+)/, "Edge"], [/OPR\/(\d+)/, "Opera"], [/Firefox\/(\d+)/, "Firefox"],
    [/Chrome\/(\d+)/, "Chrome"], [/Version\/(\d+).*Safari/, "Safari"],
  ];
  const systems = [
    [/Windows/, "Windows"], [/iPhone|iPad/, "iOS"], [/Android/, "Android"],
    [/Mac OS X/, "macOS"], [/Linux/, "Linux"],
  ];
  const found = browsers.map(([re, name]) => {
    const match = ua.match(re);
    return match && `${name} ${match[1]}`;
  }).find(Boolean);
  const os = (systems.find(([re]) => re.test(ua)) || [])[1];
  if (found) return os ? `${found} · ${os}` : found;
  return ua.length > 40 ? `${ua.slice(0, 40)}…` : ua;
}

function uaButton(ua) {
  const button = el("button", "link-button ua-short", shortUa(ua));
  button.type = "button";
  button.title = `${ua || "(no user agent)"}\nClick to show only this browser`;
  button.addEventListener("click", () => setUaFilter(ua));
  return button;
}

function setUaFilter(ua) {
  uaFilter = ua;
  if (currentView() === "log") loadActivity();
  else window.location.hash = "#log";
}

function renderUaRows(rows) {
  const body = document.querySelector("#uaRows");
  body.replaceChildren();
  document.querySelector("#uaEmpty").hidden = rows.length > 0;
  rows.forEach((row) => {
    const tr = el("tr", row.blocked ? "status-disabled" : "");
    const cell = el("td", "ua-cell");
    cell.append(uaButton(row.user_agent));
    cell.append(el("div", "ua-full mono", row.user_agent || "(none)"));
    if (row.blocked) {
      cell.append(el("div", "reason", `blocked ${untilText(row.blocked.expires_at)}`));
    }
    tr.append(cell);
    tr.append(el("td", row.ips > 5 ? "num warn" : "num", row.ips));
    tr.append(el("td", "num", row.requests || "–"));
    tr.append(el("td", "num strong", row.downloads || "–"));
    tr.append(el("td", "num", row.bytes ? formatBytes(row.bytes) : "–"));
    tr.append(el("td", row.bad_passwords ? "num warn" : "num", row.bad_passwords || "–"));
    tr.append(el("td", "checks-cell", formatTime(row.last_seen)));
    const actions = el("td", "actions-col");
    if (row.blocked) {
      const unblock = el("button", "btn btn-sm btn-outline-secondary", "Unblock");
      unblock.addEventListener("click", () => removeUaBlock(row.blocked.pattern, row.blocked.match));
      actions.append(unblock);
    } else {
      const block = el("button", "btn btn-sm btn-outline-danger", "Block");
      block.addEventListener("click", () => openUaBlockForm(row.user_agent));
      actions.append(block);
    }
    tr.append(actions);
    body.append(tr);
  });
}

function openUaBlockForm(ua) {
  window.location.hash = "#blocks";
  document.querySelector("#uaPattern").value = ua;
  document.querySelector("#uaMatch").value = "exact";
  document.querySelector("#uaReason").focus();
}

const uaHours = document.querySelector("#uaHours");
const uaCustom = document.querySelector("#uaCustom");
uaHours.addEventListener("change", () => {
  uaCustom.hidden = uaHours.value !== "custom";
  if (!uaCustom.hidden) uaCustom.focus();
});

document.querySelector("#uaBlockForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const pattern = document.querySelector("#uaPattern").value.trim();
  const match = document.querySelector("#uaMatch").value;
  const custom = uaHours.value === "custom";
  const hours = custom ? Number(uaCustom.value) : Number(uaHours.value);
  if (custom && !(hours >= 1)) {
    showMessage("Enter the number of hours.", "error");
    return;
  }
  const label = hours ? `for ${durationText(hours)}` : "permanently";
  const what = pattern
    ? (match === "exact" ? `the user agent "${shortUa(pattern)}"` : `user agents containing "${pattern}"`)
    : "requests without a user agent";
  if (!window.confirm(`Block ${what} ${label}?`)) return;
  try {
    await api("/api/admin/ua-blocks", {
      method: "POST",
      body: JSON.stringify({
        pattern, match, hours: hours || null,
        reason: document.querySelector("#uaReason").value,
      }),
    });
    showMessage(`Blocked ${what} ${label}.`, "success");
    event.target.reset();
    uaCustom.hidden = true;
    loadBlocks();
  } catch (error) {
    showMessage(error.message, "error");
  }
});

async function removeUaBlock(pattern, match) {
  if (!window.confirm(`Unblock the user agent "${shortUa(pattern)}"?`)) return;
  try {
    await api("/api/admin/ua-blocks/remove", {
      method: "POST",
      body: JSON.stringify({ pattern, match }),
    });
    showMessage("User agent unblocked.", "success");
    refreshCurrent();
  } catch (error) {
    showMessage(error.message, "error");
  }
}

function setIpFilter(ip) {
  const input = document.querySelector("#ipFilter");
  input.value = ip;
  ipFilter = ip;
  if (currentView() === "log") loadActivity();
  else window.location.hash = "#log";
}

// ----- Blocks ----------------------------------------------------------

const blockHours = document.querySelector("#blockHours");
const blockCustom = document.querySelector("#blockCustom");
blockHours.addEventListener("change", () => {
  blockCustom.hidden = blockHours.value !== "custom";
  if (!blockCustom.hidden) blockCustom.focus();
});

function openBlockForm(ip) {
  window.location.hash = "#blocks";
  document.querySelector("#blockTarget").value = ip;
  document.querySelector("#blockReason").focus();
}

document.querySelector("#blockForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const target = document.querySelector("#blockTarget").value.trim();
  const custom = blockHours.value === "custom";
  const hours = custom ? Number(blockCustom.value) : Number(blockHours.value);
  if (custom && !(hours >= 1)) {
    showMessage("Enter the number of hours.", "error");
    return;
  }
  const label = hours ? `for ${durationText(hours)}` : "permanently";
  if (!window.confirm(`Block ${target} ${label}?`)) return;
  try {
    await api("/api/admin/blocks", {
      method: "POST",
      body: JSON.stringify({
        target,
        hours: hours || null,
        reason: document.querySelector("#blockReason").value,
      }),
    });
    showMessage(`${target} is blocked ${label}.`, "success");
    event.target.reset();
    blockCustom.hidden = true;
    loadBlocks();
  } catch (error) {
    showMessage(error.message, "error");
  }
});

async function loadBlocks() {
  try {
    const [blocks, activity] = await Promise.all([
      api("/api/admin/blocks"),
      api("/api/admin/activity?hours=1"),
    ]);
    document.querySelector("#yourIp").textContent = activity.your_ip;
    document.querySelector("#yourUa").textContent = shortUa(activity.your_user_agent);
    document.querySelector("#yourUa").title = activity.your_user_agent;
    renderBlocks(blocks.blocks, blocks.ua_blocks);
  } catch (error) {
    showMessage(error.message, "error");
  }
}

function renderBlocks(blocks, uaBlocks = []) {
  document.querySelector("#blocksCount").textContent = (blocks.length + uaBlocks.length) || "";
  const body = document.querySelector("#blockRows");
  body.replaceChildren();
  document.querySelector("#blockEmpty").hidden = blocks.length + uaBlocks.length > 0;
  uaBlocks.forEach((block) => {
    const tr = el("tr");
    const cell = el("td", "ua-cell");
    const label = block.match === "exact" ? "User agent" : "User agent contains";
    cell.append(el("div", "account-meta", label));
    cell.append(el("div", "ua-full mono", block.pattern || "(no user agent)"));
    tr.append(cell);
    tr.append(el("td", "", block.expires_at ? untilText(block.expires_at) : "permanent"));
    tr.append(el("td", "", block.reason || "–"));
    tr.append(el("td", "checks-cell", formatTime(block.created_at)));
    const actions = el("td", "actions-col");
    const unblock = el("button", "btn btn-sm btn-outline-secondary", "Unblock");
    unblock.addEventListener("click", () => removeUaBlock(block.pattern, block.match));
    actions.append(unblock);
    tr.append(actions);
    body.append(tr);
  });
  blocks.forEach((block) => {
    const tr = el("tr");
    const ipCell = el("td");
    ipCell.append(ipButton(block.network, "Show this IP's activity", () => {
      setIpFilter(block.network.split("/")[0]);
    }));
    tr.append(ipCell);
    tr.append(el("td", "", block.expires_at ? untilText(block.expires_at) : "permanent"));
    tr.append(el("td", "", block.reason || "–"));
    tr.append(el("td", "checks-cell", formatTime(block.created_at)));
    const actions = el("td", "actions-col");
    const unblock = el("button", "btn btn-sm btn-outline-secondary", "Unblock");
    unblock.addEventListener("click", () => removeBlock(block.network));
    actions.append(unblock);
    tr.append(actions);
    body.append(tr);
  });
}

async function removeBlock(network) {
  if (!window.confirm(`Unblock ${network}?`)) return;
  try {
    await api("/api/admin/blocks/remove", {
      method: "POST",
      body: JSON.stringify({ network }),
    });
    showMessage(`${network} is unblocked.`, "success");
    refreshCurrent();
  } catch (error) {
    showMessage(error.message, "error");
  }
}

// ----- Formatting ------------------------------------------------------

function formatBytes(bytes) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

function formatClock(value) {
  const date = new Date(value);
  const sameDay = date.toDateString() === new Date().toDateString();
  return sameDay
    ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })
    : date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function untilText(expiresAt) {
  if (!expiresAt) return "permanently";
  return `until ${new Date(expiresAt).toLocaleString([], {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  })}`;
}

function durationText(hours) {
  if (hours % 24 === 0) return `${hours / 24} day${hours === 24 ? "" : "s"}`;
  return `${hours} hour${hours === 1 ? "" : "s"}`;
}

// The blocks count in the menu, on every tab.
api("/api/admin/blocks").then((data) => {
  document.querySelector("#blocksCount").textContent =
    (data.blocks.length + data.ua_blocks.length) || "";
}).catch(() => {});

showView();
window.setInterval(refreshCurrent, 15000);

// Long game lists (the built-in Public Steam account has hundreds) show the
// first few names with a link to expand.
function fillGameList(container, names, limit = 30) {
  container.replaceChildren();
  if (!names.length) {
    container.textContent = "–";
    return;
  }
  const shown = document.createElement("span");
  shown.textContent = names.slice(0, limit).join(", ");
  container.append(shown);
  if (names.length > limit) {
    const more = document.createElement("button");
    more.type = "button";
    more.className = "link-button more-games";
    more.textContent = ` +${names.length - limit} more`;
    more.addEventListener("click", () => {
      shown.textContent = names.join(", ");
      more.remove();
    });
    container.append(more);
  }
}
