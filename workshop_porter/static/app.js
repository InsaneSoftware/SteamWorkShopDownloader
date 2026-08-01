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

refreshQueue();
window.setInterval(refreshQueue, 2500);
