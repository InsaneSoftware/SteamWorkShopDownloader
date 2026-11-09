const tokenInput = document.querySelector("#adminToken");
const jobForm = document.querySelector("#jobForm");
const formMessage = document.querySelector("#formMessage");
const jobList = document.querySelector("#jobList");
const emptyState = document.querySelector("#queueEmpty");
const jobTemplate = document.querySelector("#jobTemplate");
const workerLabel = document.querySelector("#workerLabel");
const workerDot = document.querySelector(".worker-dot");
const submitButton = jobForm.querySelector(".submit-button");

tokenInput.value = localStorage.getItem("workshopPorterToken") || "";

document.querySelector("#toggleToken").addEventListener("click", (event) => {
  const visible = tokenInput.type === "text";
  tokenInput.type = visible ? "password" : "text";
  event.currentTarget.textContent = visible ? "Show" : "Hide";
});

document.querySelector("#refreshButton").addEventListener("click", refreshQueue);

jobForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const token = tokenInput.value.trim();
  localStorage.setItem("workshopPorterToken", token);
  showMessage("", "");
  submitButton.disabled = true;
  try {
    const response = await fetch("/api/jobs", {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        url: document.querySelector("#workshopUrl").value,
        app_id: Number(new FormData(jobForm).get("app_id")),
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not queue this item");
    const resultMessage = data.created
      ? "Workshop item added to the queue."
      : "That item is already in the active queue.";
    showMessage(resultMessage, "success");
    document.querySelector("#workshopUrl").value = "";
    await refreshQueue();
  } catch (error) {
    showMessage(error.message, "error");
  } finally {
    submitButton.disabled = false;
  }
});

function showMessage(message, style) {
  formMessage.textContent = message;
  formMessage.className = `form-message ${style}`;
}

async function refreshQueue() {
  const token = tokenInput.value.trim();
  if (!token) {
    workerLabel.textContent = "Token needed";
    workerDot.className = "worker-dot offline";
    jobList.replaceChildren();
    emptyState.hidden = false;
    return;
  }
  localStorage.setItem("workshopPorterToken", token);
  try {
    const [response, adminResponse] = await Promise.all([
      fetch("/api/jobs", {
        headers: { "Authorization": `Bearer ${token}` },
        cache: "no-store",
      }),
      fetch("/api/admin", { cache: "no-store" }),
    ]);
    const data = await response.json();
    const adminData = await adminResponse.json();
    if (!response.ok) throw new Error(data.error || "Queue unavailable");
    renderJobs(data.jobs, adminData.admin);
    document.querySelector("#adminLink").hidden = !adminData.admin;
    const busy = data.jobs.some((job) =>
      ["downloading", "packing"].includes(job.status)
    );
    workerLabel.textContent = busy ? "Processing" : "Standing by";
    workerDot.className = busy ? "worker-dot busy" : "worker-dot";
  } catch (error) {
    workerLabel.textContent = offlineText(error);
    workerDot.className = "worker-dot offline";
  }
}

function offlineText(error) {
  return error.message.includes("token") ? "Token rejected" : "Offline";
}

function renderJobs(jobs, canDelete) {
  jobList.replaceChildren();
  emptyState.hidden = jobs.length > 0;
  jobs.forEach((job) => {
    const card = jobTemplate.content.firstElementChild.cloneNode(true);
    card.classList.add(job.status);
    card.querySelector(".job-game").textContent = job.game_name;
    const status = card.querySelector(".job-status");
    status.textContent = job.status;
    status.classList.add(job.status);
    card.querySelector(".job-title").textContent =
      job.item_name || `Workshop #${job.workshop_id}`;
    card.querySelector(".job-message").textContent = job.error
      ? "Download failed - see the error below"
      : job.message;
    card.querySelector(".progress-bar").style.width = `${job.progress}%`;
    card.querySelector(".job-percent").textContent = `${job.progress}%`;
    card.querySelector(".job-created").textContent =
      `Added ${formatRelativeTime(job.created_at)}`;
    card.querySelector(".job-size").textContent = job.archive_bytes
      ? formatBytes(job.archive_bytes)
      : "";
    card.querySelector(".job-expiry").textContent = job.expires_at
      ? `Expires ${formatRelativeTime(job.expires_at)}`
      : "";

    const errorWrap = card.querySelector(".job-error-wrap");
    if (job.error) {
      errorWrap.classList.add("visible");
      card.querySelector(".job-error").textContent = job.error;
    }

    const copyButton = card.querySelector(".copy-button");
    const downloadButton = card.querySelector(".download-button");
    const deleteButton = card.querySelector(".delete-button");
    const ready = job.status === "ready" && job.download_url;
    copyButton.hidden = !ready;
    downloadButton.hidden = !ready;
    deleteButton.hidden = !canDelete;
    if (ready) {
      downloadButton.href = job.download_url;
      copyButton.addEventListener("click", async () => {
        await navigator.clipboard.writeText(job.download_url);
        copyButton.textContent = "COPIED";
        window.setTimeout(() => { copyButton.textContent = "COPY LINK"; }, 1600);
      });
    }
    if (canDelete) {
      deleteButton.addEventListener("click", async () => {
        if (!window.confirm(`Delete ${job.item_name || "this entry"}?`)) return;
        const response = await fetch(`/api/jobs/${job.id}`, { method: "DELETE" });
        const data = await response.json();
        if (!response.ok) {
          showMessage(data.error || "Delete failed", "error");
          return;
        }
        await refreshQueue();
      });
    }
    jobList.append(card);
  });
}

function formatBytes(bytes) {
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

function formatRelativeTime(value) {
  const seconds = Math.round((new Date(value).getTime() - Date.now()) / 1000);
  const formatter = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  const ranges = [
    ["day", 86400],
    ["hour", 3600],
    ["minute", 60],
  ];
  for (const [unit, divisor] of ranges) {
    if (Math.abs(seconds) >= divisor) return formatter.format(Math.round(seconds / divisor), unit);
  }
  return formatter.format(seconds, "second");
}

// ----- Community account pool ------------------------------------------

const accountForm = document.querySelector("#accountForm");
const accountMessage = document.querySelector("#accountMessage");
const gameSelect = document.querySelector("#gameSelect");
const STATUS_LABELS = {
  working: "working",
  pending: "validating",
  broken: "broken",
  disabled: "disabled",
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function refreshPool() {
  const token = tokenInput.value.trim();
  if (!token) return;
  try {
    const headers = { "Authorization": `Bearer ${token}` };
    const [gamesResponse, accountsResponse] = await Promise.all([
      fetch("/api/games", { headers, cache: "no-store" }),
      fetch("/api/accounts", { headers, cache: "no-store" }),
    ]);
    if (!gamesResponse.ok || !accountsResponse.ok) return;
    const { games } = await gamesResponse.json();
    const { accounts, check_interval_minutes } = await accountsResponse.json();
    renderGames(games);
    renderAccounts(accounts, check_interval_minutes);
  } catch (error) {
    // Queue status already shows connectivity problems.
  }
}

function renderGames(games) {
  const rows = document.querySelector("#gameRows");
  rows.replaceChildren();
  games
    .slice()
    .sort((a, b) => b.accounts - a.accounts || a.name.localeCompare(b.name))
    .forEach((game) => {
      const row = el("tr", game.accounts ? "" : "unsupported");
      row.append(el("td", "", game.name));
      const count = el("td", "count",
        game.accounts ? `${game.accounts} account${game.accounts === 1 ? "" : "s"}` : "none yet");
      row.append(count);
      rows.append(row);
    });

  // Mark games without a working account in the request form.
  const counts = new Map(games.map((game) => [String(game.app_id), game]));
  [...gameSelect.options].forEach((option) => {
    const game = counts.get(option.value);
    if (!game) return;
    option.textContent = game.accounts
      ? `${game.name} (${game.accounts} account${game.accounts === 1 ? "" : "s"})`
      : `${game.name} (no account yet)`;
    option.disabled = game.accounts === 0;
  });
  if (gameSelect.selectedOptions[0]?.disabled) {
    const firstEnabled = [...gameSelect.options].find((option) => !option.disabled);
    if (firstEnabled) gameSelect.value = firstEnabled.value;
  }
}

function renderAccounts(accounts, interval) {
  const list = document.querySelector("#accountList");
  const working = accounts.filter((a) => a.status === "working").length;
  document.querySelector("#accountSummary").textContent =
    `${working} of ${accounts.length} working · checked every ${interval} min`;
  list.replaceChildren();
  if (!accounts.length) {
    list.append(el("div", "empty-state", "No accounts shared yet. Be the first!"));
    return;
  }
  accounts.forEach((account) => {
    const item = el("article", `account-row ${account.status}`);
    const head = el("div", "account-head");
    head.append(el("span", "account-name", account.display_name));
    head.append(el("span", `job-status ${account.status}`,
      STATUS_LABELS[account.status] || account.status));
    item.append(head);

    const sub = el("div", "account-sub");
    if (account.status === "working") {
      sub.textContent = account.games.map((g) => g.name).join(", ");
    } else {
      sub.textContent = account.status_reason || "";
    }
    item.append(sub);
    if (account.last_checked_at) {
      item.append(el("div", "account-meta",
        `Checked ${formatRelativeTime(account.last_checked_at)}`));
    }
    list.append(item);
  });
}

accountForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const token = tokenInput.value.trim();
  if (!token) {
    showAccountMessage("Enter the site password at the top first.", "error");
    return;
  }
  const button = accountForm.querySelector(".submit-button");
  button.disabled = true;
  showAccountMessage("Submitting…", "");
  try {
    const response = await fetch("/api/accounts", {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        display_name: document.querySelector("#displayName").value,
        username: document.querySelector("#steamLogin").value,
        password: document.querySelector("#steamPassword").value,
        guard_code: document.querySelector("#guardCode").value,
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not add this account");
    // Do not keep credentials in the page longer than needed.
    ["#steamLogin", "#steamPassword", "#guardCode"].forEach((selector) => {
      document.querySelector(selector).value = "";
    });
    showAccountMessage("Account queued. Logging in with SteamCMD…", "");
    await refreshPool();
    const final = await waitForValidation(data.account.id, token);
    if (final.status === "working") {
      showAccountMessage(
        `Thanks! Account works and owns: ${final.games.map((g) => g.name).join(", ")}.`,
        "success",
      );
    } else if (final.status === "pending") {
      showAccountMessage(
        "Still validating (the server may be busy downloading). Check the list later.",
        "",
      );
    } else {
      showAccountMessage(`Not added: ${final.status_reason}`, "error");
    }
    await refreshPool();
  } catch (error) {
    showAccountMessage(error.message, "error");
  } finally {
    button.disabled = false;
  }
});

async function waitForValidation(accountId, token) {
  const deadline = Date.now() + 5 * 60 * 1000;
  let account = null;
  while (Date.now() < deadline) {
    await new Promise((resolve) => window.setTimeout(resolve, 3000));
    const response = await fetch(`/api/accounts/${accountId}`, {
      headers: { "Authorization": `Bearer ${token}` },
      cache: "no-store",
    });
    if (!response.ok) break;
    account = (await response.json()).account;
    if (account.status !== "pending") return account;
  }
  return account || { status: "pending" };
}

function showAccountMessage(message, style) {
  accountMessage.textContent = message;
  accountMessage.className = `form-message ${style}`;
}

refreshQueue();
refreshPool();
window.setInterval(refreshQueue, 2500);
window.setInterval(refreshPool, 15000);
