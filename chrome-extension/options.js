const element = (id) => document.getElementById(id);
const controls = ["save", "run-now", "enabled", "token", "clear-token", "interval"];
const sourceLabels = { reddit: "Reddit", x_twitter: "X" };
let state = null;
let busy = false;
let dirty = false;

function feedback(message, error = false) {
  element("feedback").textContent = message;
  element("feedback").className = error ? "error" : "";
}

function updateControls() {
  for (const id of controls) element(id).disabled = busy;
  element("run-now").disabled = busy || dirty || !state?.settings.enabled || state?.runStatus.running;
  element("token").disabled = busy || element("clear-token").checked;
  for (const button of document.querySelectorAll("button[data-source]")) {
    button.disabled = busy || !state?.sourceStatus?.[button.dataset.source]?.paused;
  }
  for (const button of document.querySelectorAll("button[data-run-source]")) {
    button.disabled = element("run-now").disabled || Boolean(state?.sourceStatus?.[button.dataset.runSource]?.paused);
  }
}

function formatDate(value) {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString() : "Never";
}

function render(next, populateForm = false) {
  state = next;
  element("extension-id").textContent = next.extensionId;
  if (populateForm) {
    element("enabled").checked = next.settings.enabled;
    element("interval").value = String(next.settings.intervalMinutes);
    element("token").value = "";
    element("clear-token").checked = false;
    dirty = false;
  }
  element("token").placeholder = next.settings.tokenConfigured ? "Token stored; leave blank to keep it" : "Paste the receiver token";
  const run = next.runStatus;
  const runParts = [run.running ? "Running." : "Idle.", run.message || ""];
  if (run.lastStartedAt) runParts.push(`Last start: ${formatDate(run.lastStartedAt)}.`);
  if (run.lastFinishedAt) runParts.push(`Last finish: ${formatDate(run.lastFinishedAt)}.`);
  element("run-status").textContent = runParts.join(" ");
  const container = element("source-statuses");
  container.replaceChildren();
  for (const [source, label] of Object.entries(sourceLabels)) {
    const status = next.sourceStatus[source] || {};
    const card = document.createElement("article");
    const heading = document.createElement("h3");
    heading.textContent = `${label}: ${status.phase === "disabled" ? "disabled" : status.paused ? "paused" : status.phase || "ready"}`;
    const message = document.createElement("p");
    message.textContent = status.message || "Not run yet.";
    const details = document.createElement("p");
    details.className = "hint";
    details.textContent = `Last run: ${formatDate(status.lastRun)}. Last success: ${formatDate(status.lastSuccess)}. Collected: ${Number(status.collected) || 0}; accepted: ${Number(status.accepted) || 0}.`;
    const resume = document.createElement("button");
    resume.type = "button";
    resume.dataset.source = source;
    resume.textContent = `Resume ${label}`;
    resume.addEventListener("click", () => perform(async () => {
      const response = await request({ type: "resumeSource", source });
      render(response.state);
      feedback(`${label} resumed. Use Run now or wait for the scheduled collection.`);
    }));
    const runOne = document.createElement("button");
    runOne.type = "button";
    runOne.dataset.runSource = source;
    runOne.textContent = `Run ${label} now`;
    runOne.addEventListener("click", () => perform(async () => {
      const response = await request({ type: "runNow", source });
      render(response.state);
      feedback(response.started ? `${label} collection started.` : "A collection run is already active.");
    }));
    card.append(heading, message, details, resume, runOne);
    container.append(card);
  }
  updateControls();
}

async function request(message) {
  const response = await chrome.runtime.sendMessage(message);
  if (!response || response.error) throw new Error(response?.error || "The collection worker did not respond. Try reloading the extension.");
  return response;
}

async function perform(action) {
  if (busy) return;
  busy = true;
  updateControls();
  try { await action(); }
  catch (error) { feedback(error.message || "The request failed.", true); }
  finally { busy = false; updateControls(); }
}

for (const id of ["enabled", "token", "clear-token", "interval"]) {
  element(id).addEventListener("input", () => {
    dirty = true;
    if (element("clear-token").checked) element("enabled").checked = false;
    updateControls();
  });
}

element("settings-form").addEventListener("submit", (event) => {
  event.preventDefault();
  perform(async () => {
    const response = await request({
      type: "saveSettings",
      settings: {
        token: element("token").value,
        clearToken: element("clear-token").checked,
        enabled: element("enabled").checked,
        intervalMinutes: Number(element("interval").value),
      },
    });
    render(response.state, true);
    feedback("Settings saved. Paused sources have been resumed.");
  });
});

element("run-now").addEventListener("click", () => perform(async () => {
  const response = await request({ type: "runNow" });
  render(response.state);
  feedback(response.started ? "Collection started. Per-source results appear below." : "A collection run is already active.");
}));

// Storage events keep results current without a timer or worker keepalive.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local" || !(changes.sourceStatus || changes.runStatus || changes.settings)) return;
  request({ type: "getState" }).then((response) => render(response.state), (error) => feedback(error.message, true));
});

perform(async () => {
  await chrome.storage.local.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" });
  await chrome.storage.session.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" });
  const response = await request({ type: "getState" });
  render(response.state, true);
});
