import { collectRenderedPosts } from "./extract.js";
import {
  RECEIVER_ORIGIN, SOURCE_NAMES, MAX_PAGE_MS, MAX_RUN_MS,
  normalizeConfig, normalizePost, normalizeSettings, postBatches, sourcePages, validToken,
} from "./protocol.js";

const ALARM_NAME = "oink-social-collection";
const emptySource = () => ({ paused: false, phase: "ready", message: "Not run yet.", collected: 0, accepted: 0 });
let activeRun = null;
let controlQueue = Promise.resolve();

function serializeControl(action) {
  const result = controlQueue.then(action);
  controlQueue = result.catch(() => {});
  return result;
}
// Session storage survives MV3 worker restarts, but not browser restarts where
// tab IDs can be reused for unrelated user tabs.
let ownership = { lease: null, ownedTabs: [] };

function errorText(error) {
  if (error?.name === "AbortError") return "Collection cancelled.";
  return typeof error?.message === "string" ? error.message.slice(0, 500) : "Collection failed.";
}

async function persistOwnership() {
  await chrome.storage.session.set({ collectorOwnership: ownership });
}

async function closeOwnedTab(id) {
  if (!ownership.ownedTabs.includes(id)) return;
  try {
    await chrome.tabs.remove(id);
  } catch {
    // A tab the user already closed is also successfully cleaned up.
    let exists = false;
    try { await chrome.tabs.get(id); exists = true; } catch { /* Already gone. */ }
    if (exists) throw new Error("An owned collection tab could not be closed. It will be cleaned up on the next worker start.");
  }
  ownership.ownedTabs = ownership.ownedTabs.filter((owned) => owned !== id);
  await persistOwnership();
}

async function cleanupOwnedTabs() {
  let failure = "";
  for (const id of [...ownership.ownedTabs]) {
    try { await closeOwnedTab(id); } catch (error) { failure = errorText(error); }
  }
  return failure;
}

async function ensureSchedule(settings) {
  if (!settings.enabled) {
    await chrome.alarms.clear(ALARM_NAME);
    return;
  }
  const current = await chrome.alarms.get(ALARM_NAME);
  if (!current || current.periodInMinutes !== settings.intervalMinutes) {
    await chrome.alarms.create(ALARM_NAME, { delayInMinutes: 1, periodInMinutes: settings.intervalMinutes });
  }
}

async function initialize() {
  await Promise.all([
    chrome.storage.local.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" }),
    chrome.storage.session.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" }),
  ]);
  const [local, session] = await Promise.all([
    chrome.storage.local.get(["settings", "sourceStatus", "runStatus"]),
    chrome.storage.session.get("collectorOwnership"),
  ]);
  const savedOwnership = session.collectorOwnership;
  if (savedOwnership && Array.isArray(savedOwnership.ownedTabs)) {
    ownership.ownedTabs = [...new Set(savedOwnership.ownedTabs.filter(Number.isInteger))];
  }
  const cleanupError = await cleanupOwnedTabs();
  ownership.lease = null;
  await persistOwnership();
  const settings = normalizeSettings(local.settings);
  const sourceStatus = Object.fromEntries(SOURCE_NAMES.map((source) => [source, {
    ...emptySource(), ...(local.sourceStatus?.[source] || {}),
  }]));
  const updates = { settings, sourceStatus };
  if (local.runStatus?.running || cleanupError) {
    updates.runStatus = {
      ...local.runStatus, running: false, lastFinishedAt: new Date().toISOString(),
      message: cleanupError || "The previous worker was interrupted. Owned tabs were closed; the next run starts fresh.",
    };
    for (const source of SOURCE_NAMES) {
      if (sourceStatus[source].phase === "running") {
        sourceStatus[source] = { ...sourceStatus[source], phase: "error", message: updates.runStatus.message };
      }
    }
  }
  await chrome.storage.local.set(updates);
  if (cleanupError) await chrome.alarms.clear(ALARM_NAME);
  else await ensureSchedule(settings);
}

const ready = initialize();
ready.catch((error) => console.error("Oink collector initialization failed:", errorText(error)));

async function setSourceStatus(source, patch) {
  const stored = await chrome.storage.local.get("sourceStatus");
  const statuses = stored.sourceStatus || {};
  statuses[source] = { ...emptySource(), ...statuses[source], ...patch };
  await chrome.storage.local.set({ sourceStatus: statuses });
}

function checkRun(run) {
  if (run.controller.signal.aborted) throw new DOMException("Collection cancelled.", "AbortError");
  if (Date.now() >= run.deadline) throw new Error("The collection run reached its ten-minute deadline.");
}

async function sleep(ms, run) {
  checkRun(run);
  await new Promise((resolve, reject) => {
    const onAbort = () => { clearTimeout(timer); reject(new DOMException("Collection cancelled.", "AbortError")); };
    const timer = setTimeout(() => {
      run.controller.signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    run.controller.signal.addEventListener("abort", onAbort, { once: true });
  });
  checkRun(run);
}

async function receiverRequest(path, settings, run, payload = undefined) {
  checkRun(run);
  const controller = new AbortController();
  const onAbort = () => controller.abort();
  run.controller.signal.addEventListener("abort", onAbort, { once: true });
  const timeout = setTimeout(() => controller.abort(), Math.min(10_000, run.deadline - Date.now()));
  try {
    const response = await fetch(`${RECEIVER_ORIGIN}${path}`, {
      method: payload === undefined ? "GET" : "POST",
      headers: {
        Authorization: `Bearer ${settings.token}`,
        ...(payload === undefined ? {} : { "Content-Type": "application/json" }),
      },
      ...(payload === undefined ? {} : { body: JSON.stringify(payload) }),
      credentials: "omit", cache: "no-store", redirect: "error", referrerPolicy: "no-referrer",
      signal: controller.signal,
    });
    if (!response.ok) {
      const guidance = response.status === 401 || response.status === 403
        ? " Check OINK_SOCIAL_BROWSER_TOKEN and OINK_SOCIAL_BROWSER_EXTENSION_ID."
        : " Check the local receiver.";
      throw new Error(`Local receiver returned HTTP ${response.status}.${guidance}`);
    }
    const body = await response.text();
    if (body.length > 100_000) throw new Error("Local receiver response is too large.");
    try { return JSON.parse(body); } catch { throw new Error("Local receiver did not return valid JSON."); }
  } catch (error) {
    if (run.controller.signal.aborted) throw new DOMException("Collection cancelled.", "AbortError");
    if (error?.name === "AbortError") throw new Error("Local receiver did not respond within ten seconds.");
    if (error instanceof TypeError) throw new Error("Cannot reach the local receiver. Check its service, token, and configured extension ID.");
    throw error;
  } finally {
    clearTimeout(timeout);
    run.controller.signal.removeEventListener("abort", onAbort);
  }
}

async function inspectOwnedTab(tabId, expectedOrigin, run) {
  checkRun(run);
  if (!ownership.ownedTabs.includes(tabId)) throw new Error("The collection tab is no longer owned by this run.");
  const tab = await chrome.tabs.get(tabId);
  const destination = tab.pendingUrl || tab.url || "";
  if (destination && destination !== "about:blank") {
    let origin;
    try { origin = new URL(destination).origin; } catch { origin = ""; }
    if (origin !== expectedOrigin) return { tab, pause: "The collection tab navigated to another origin. Check the site manually, then explicitly resume." };
  }
  return { tab, pause: null };
}

async function collectPage(source, url, config, run, accumulated, options = {}) {
  checkRun(run);
  const deadline = Math.min(Date.now() + MAX_PAGE_MS, run.deadline);
  const tab = await chrome.tabs.create({ url, active: false });
  if (!Number.isInteger(tab.id)) throw new Error("Chrome did not create a collection tab.");
  ownership.ownedTabs.push(tab.id);
  try {
    await persistOwnership();
    const expectedOrigin = new URL(url).origin;
    const rememberPause = async (reason) => {
      run.pauses[source] = reason;
      await setSourceStatus(source, { paused: true, phase: "paused", message: reason, collected: accumulated.size, accepted: 0 });
      return reason;
    };
    const scan = async (scroll = false) => {
      if (Date.now() >= deadline) throw new Error("The source page reached its 45-second deadline.");
      const inspected = await inspectOwnedTab(tab.id, expectedOrigin, run);
      if (inspected.pause) return { posts: [], pause: await rememberPause(inspected.pause) };
      const results = await chrome.scripting.executeScript({
        target: { tabId: tab.id, allFrames: false }, world: "ISOLATED",
        func: collectRenderedPosts, args: [source, { limit: 100, scroll, detailPostId: options.detailPostId }],
      });
      checkRun(run);
      const result = results.find((entry) => entry.frameId === 0)?.result;
      if (!result || !Array.isArray(result.posts)) throw new Error("The source page did not expose readable rendered posts.");
      if (new URL(result.url).origin !== expectedOrigin) {
        return { posts: [], pause: await rememberPause("The collection page changed origin. Explicitly resume after checking the site.") };
      }
      if (result.pause) await rememberPause(result.pause);
      for (const raw of result.posts.slice(0, 100)) {
        const post = normalizePost(source, raw, config);
        if (!post || (options.detailPostId && post.id !== options.detailPostId)) continue;
        const previous = accumulated.get(post.id);
        if (!previous && (options.detailPostId || accumulated.size >= config.max_per_source)) continue;
        if (!previous) {
          accumulated.set(post.id, post);
          continue;
        }
        // Repeated/virtualized cards must not erase a body already captured,
        // and reaching the count cap must still allow details to fill that ID.
        const expandedLink = post.external_url && previous.external_url
          && new URL(previous.external_url).hostname === "t.co"
          && new URL(post.external_url).hostname !== "t.co";
        accumulated.set(post.id, {
          ...post,
          title: previous.title.length > post.title.length ? previous.title : post.title,
          text: previous.text.length > post.text.length ? previous.text : post.text,
          display_name: post.display_name || previous.display_name,
          external_url: expandedLink ? post.external_url : previous.external_url || post.external_url,
        });
      }
      return result;
    };
    // Poll only this owned tab for real navigation readiness, not as a worker
    // keepalive. Chrome may still suspend a run; initialization cleans it up.
    const loadDeadline = Math.min(deadline - 10_000, Date.now() + 20_000);
    while (Date.now() < loadDeadline) {
      const inspected = await inspectOwnedTab(tab.id, expectedOrigin, run);
      if (inspected.pause) return rememberPause(inspected.pause);
      if (inspected.tab.status === "complete") break;
      await sleep(600, run);
    }
    await sleep(1200, run);
    const hydrationDeadline = Math.min(deadline - 7000, Date.now() + 10_000);
    let snapshot;
    do {
      snapshot = await scan();
      if (snapshot.pause) return snapshot.pause;
      const captured = options.detailPostId
        ? snapshot.posts.some((post) => post.id === options.detailPostId && (post.text?.trim() || post.external_url))
        : snapshot.posts.length > 0;
      if (captured || Date.now() >= hydrationDeadline) break;
      await sleep(1000, run);
    } while (true);
    // Detail pages contain comments and recommendations, not another listing.
    // A title alone is not a hydrated body; a link-only post needs no selftext.
    if (options.detailPostId) return null;
    for (let scroll = 0; scroll < 3 && accumulated.size < config.max_per_source; scroll += 1) {
      // Every scroll captures the current rendered cards first, then the next
      // snapshot captures new cards, retaining virtualized cards in the Map.
      snapshot = await scan(true);
      if (snapshot.pause) return snapshot.pause;
      await sleep(1500, run);
      snapshot = await scan();
      if (snapshot.pause) return snapshot.pause;
    }
    return null;
  } finally {
    await closeOwnedTab(tab.id);
  }
}

// `budgetEnd` is this source's share of the run, so an earlier source (Reddit's
// per-post detail pages) cannot consume the whole run and starve later ones.
async function collectSource(source, config, settings, run, budgetEnd = run.deadline) {
  checkRun(run);
  const stored = await chrome.storage.local.get("sourceStatus");
  const pages = sourcePages(source, config);
  if (!pages.length) {
    await setSourceStatus(source, { phase: "disabled", message: "No list is configured for this source on the receiver.", collected: 0, accepted: 0 });
    return;
  }
  if (stored.sourceStatus?.[source]?.paused) return;
  const startedAt = new Date().toISOString();
  await setSourceStatus(source, { phase: "running", message: "Reading rendered source pages…", lastRun: startedAt, collected: 0, accepted: 0 });
  const posts = new Map();
  let pause = null;
  let failure = "";
  let accepted = 0;
  try {
    for (const url of pages) {
      checkRun(run);
      if (posts.size >= config.max_per_source) break;
      if (url !== pages[0] && Date.now() + MAX_PAGE_MS >= budgetEnd) break;
      try {
        pause = await collectPage(source, url, config, run, posts);
      } catch (error) {
        if (run.controller.signal.aborted) throw error;
        failure = errorText(error);
        break;
      }
      if (pause) break;
    }
    if (source === "reddit" && !pause && !failure) {
      await setSourceStatus(source, { message: "Reading captured Reddit post bodies…" });
      // Discover the combined newest listing first, so slow detail pages cannot
      // prevent discovery from the other configured subreddits. Keep one page
      // window for detail and another for submitting observations before expiry.
      for (const post of posts.values()) {
        if (post.text) continue;
        if (Date.now() + MAX_PAGE_MS * 2 >= budgetEnd) break;
        try {
          pause = await collectPage(source, post.url, config, run, posts, { detailPostId: post.id });
        } catch (error) {
          if (run.controller.signal.aborted) throw error;
          failure = errorText(error);
          break;
        }
        if (pause) break;
      }
    }
    // A later page may be blocked: keep and submit earlier observed posts.
    for (const batch of postBatches(source, [...posts.values()])) {
      const response = await receiverRequest("/v1/posts", settings, run, { source, posts: batch });
      if (!Number.isInteger(response.accepted) || response.accepted < 0 || response.accepted > batch.length) {
        throw new Error("Local receiver returned an invalid accepted count.");
      }
      accepted += response.accepted;
    }
  } catch (error) {
    failure = errorText(error);
  }
  pause ||= run.pauses[source];
  await setSourceStatus(source, {
    paused: Boolean(pause), phase: pause ? "paused" : failure ? "error" : "success",
    message: [pause, failure].filter(Boolean).join(" ") || `Received ${accepted} of ${posts.size} collected posts.`,
    collected: posts.size, accepted, lastRun: startedAt,
    ...(!failure && !pause ? { lastSuccess: new Date().toISOString() } : {}),
  });
  if (ownership.ownedTabs.length) throw new Error("A collection tab could not be closed; stopping before opening another source.");
  checkRun(run);
}

async function performRun(run, trigger) {
  const { settings: rawSettings } = await chrome.storage.local.get("settings");
  const settings = normalizeSettings(rawSettings);
  if (!settings.enabled) return;
  if (ownership.ownedTabs.length) throw new Error("An earlier owned collection tab still needs cleanup. Reload the extension to retry cleanup.");
  if (ownership.lease && ownership.lease.expiresAt > Date.now()) return;
  ownership.lease = { id: run.id, expiresAt: run.deadline + 60_000 };
  await persistOwnership();
  await chrome.storage.local.set({ runStatus: { running: true, trigger, lastStartedAt: new Date().toISOString(), message: "Fetching local collection configuration…" } });
  let message = "Collection finished.";
  try {
    const config = normalizeConfig(await receiverRequest("/v1/config", settings, run));
    for (const [index, source] of run.sources.entries()) {
      // Split the remaining time evenly; time a source leaves unused rolls over.
      const share = (run.deadline - Date.now()) / (run.sources.length - index);
      await collectSource(source, config, settings, run, Date.now() + share);
    }
  } catch (error) {
    message = errorText(error);
    if (!run.controller.signal.aborted) {
      for (const source of SOURCE_NAMES) {
        const current = await chrome.storage.local.get("sourceStatus");
        if (!current.sourceStatus?.[source]?.paused) {
          await setSourceStatus(source, { phase: "error", message });
        }
      }
    }
  } finally {
    const cleanupError = await cleanupOwnedTabs();
    if (cleanupError) message = cleanupError;
    ownership.lease = null;
    await persistOwnership();
    const stored = await chrome.storage.local.get("runStatus");
    await chrome.storage.local.set({ runStatus: { ...stored.runStatus, running: false, lastFinishedAt: new Date().toISOString(), message } });
  }
}

function startRun(trigger, sources = SOURCE_NAMES) {
  if (activeRun) return false;
  const run = { id: crypto.randomUUID(), controller: new AbortController(), deadline: Date.now() + MAX_RUN_MS, pauses: {}, done: null, sources };
  activeRun = run;
  run.done = performRun(run, trigger).catch(async (error) => {
    await chrome.storage.local.set({ runStatus: { running: false, lastFinishedAt: new Date().toISOString(), message: errorText(error) } });
  }).finally(() => { if (activeRun === run) activeRun = null; });
  return true;
}

async function stopActiveRun() {
  if (!activeRun) return;
  const run = activeRun;
  run.controller.abort();
  await run.done;
}

async function publicState() {
  const data = await chrome.storage.local.get(["settings", "sourceStatus", "runStatus"]);
  const settings = normalizeSettings(data.settings);
  return {
    extensionId: chrome.runtime.id,
    settings: { enabled: settings.enabled, intervalMinutes: settings.intervalMinutes, tokenConfigured: validToken(settings.token) },
    sourceStatus: data.sourceStatus || {}, runStatus: data.runStatus || { running: false, message: "Not run yet." },
  };
}

async function handleMessage(message) {
  await ready;
  if (!message || typeof message !== "object") throw new Error("Invalid options request.");
  if (message.type === "getState") return { state: await publicState() };
  return serializeControl(() => applyMessage(message));
}

async function applyMessage(message) {
  if (message.type === "saveSettings") {
    const { settings: previous } = await chrome.storage.local.get("settings");
    const supplied = message.settings;
    if (!supplied || typeof supplied !== "object") throw new Error("Invalid settings.");
    const token = supplied.clearToken === true ? "" : supplied.token || previous?.token || "";
    const interval = Number(supplied.intervalMinutes);
    if (!Number.isInteger(interval) || interval < 15 || interval > 1440) throw new Error("Interval must be a whole number from 15 to 1440 minutes.");
    if (token && !validToken(token)) throw new Error("The token must contain 32–512 printable ASCII characters, without spaces.");
    if (supplied.enabled === true && !validToken(token)) throw new Error("Set a receiver token of at least 32 characters before enabling collection.");
    await stopActiveRun();
    const settings = { enabled: supplied.enabled === true && supplied.clearToken !== true, token, intervalMinutes: interval };
    await chrome.storage.local.set({ settings });
    for (const source of SOURCE_NAMES) {
      await setSourceStatus(source, { paused: false, phase: settings.enabled ? "ready" : "disabled", message: settings.enabled ? "Settings saved; source resumed for the next run." : "Collection is disabled." });
    }
    await ensureSchedule(settings);
    return { state: await publicState() };
  }
  if (message.type === "resumeSource") {
    if (!SOURCE_NAMES.includes(message.source)) throw new Error("Unknown source.");
    await stopActiveRun();
    await setSourceStatus(message.source, { paused: false, phase: "ready", message: "Source resumed. Use Run now or wait for the next scheduled run." });
    return { state: await publicState() };
  }
  if (message.type === "runNow") {
    const { settings } = await chrome.storage.local.get("settings");
    if (!normalizeSettings(settings).enabled) throw new Error("Save enabled collection with a valid receiver token first.");
    if (message.source !== undefined && !SOURCE_NAMES.includes(message.source)) throw new Error("Unknown source.");
    const sources = message.source === undefined ? SOURCE_NAMES : [message.source];
    return { started: startRun("manual", sources), state: await publicState() };
  }
  throw new Error("Unknown options request.");
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (sender.id !== chrome.runtime.id || sender.url !== chrome.runtime.getURL("options.html")) return false;
  handleMessage(message).then(sendResponse, (error) => sendResponse({ error: errorText(error) }));
  return true;
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== ALARM_NAME) return;
  ready.then(() => serializeControl(async () => {
    const { settings } = await chrome.storage.local.get("settings");
    if (normalizeSettings(settings).enabled) startRun("alarm");
    else await chrome.alarms.clear(ALARM_NAME);
  })).catch((error) => console.error("Oink collector alarm failed:", errorText(error)));
});

for (const event of [chrome.runtime.onInstalled, chrome.runtime.onStartup]) {
  event.addListener(() => { ready.catch(() => {}); });
}
chrome.action.onClicked.addListener(() => { chrome.runtime.openOptionsPage(); });
