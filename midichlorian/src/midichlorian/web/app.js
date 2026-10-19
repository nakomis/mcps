"use strict";
/* midichlorian web UI. Plain DOM, no build step, no external resources.
   Everything that comes from the server is untrusted (log text, task names, reasons),
   so it only ever reaches the page through textContent / text nodes. */

const POLL_MS = 5000;
const PEER_LOG_POLL_MS = 3000;
const CONFIRM_MS = 4000;
const NOTICE_MS = 8000;
const FETCH_TIMEOUT_MS = 8000;
const LOG_MAX_CHARS = 400000;
const REPO_COMMIT_URL = "https://github.com/nakomis/home-infra/commit/";
const TERMINAL = ["succeeded", "failed", "skipped", "cancelled", "interrupted", "already"];
const PENDING = ["queued", "waiting"];
const BADGE_CLASS = {
  succeeded: "ok", already: "ok",
  failed: "bad", interrupted: "bad",
  running: "info", waiting: "warn", queued: "info",
  skipped: "", cancelled: "",
};

const $ = (id) => document.getElementById(id);

const state = {
  localInstance: null,
  hostsOk: true, queueOk: true, historyOk: true,
  peersDown: { hosts: [], queue: [], history: [] },
  seq: { hosts: 0, queue: 0, history: 0, detail: 0 },
  sig: {},             // last rendered signature per section, to skip identical repaints
  detailJob: null,     // cached detail of the selected run
  notice: null,
  hosts: [], queue: [], history: [],
  selected: null,      // {instance, id} whose detail is open
  confirming: null,    // run key whose cancel button is armed
  log: null,           // {key, source, timer, text}
  autoOpened: new Set(),
  stick: true,
};

/* ---------- small helpers ---------- */

function el(tag, cls, ...children) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function replace(container, ...nodes) {
  container.replaceChildren(...nodes);
}

/* Run render only when something visible changed, so focus, selection and scroll survive polling. */
function changed(section, ...parts) {
  const sig = JSON.stringify(parts) + "|" + Math.floor(Date.now() / 60000);  // relative times age per minute
  if (state.sig[section] === sig) return false;
  state.sig[section] = sig;
  return true;
}

function selectedKey() {
  return state.selected ? runKey(state.selected.instance, state.selected.id) : null;
}

function badge(text, kind) {
  return el("span", "badge" + (kind ? " " + kind : ""), text);
}

function stateBadge(st) {
  return badge(st || "unknown", BADGE_CLASS[st] || "");
}

function isLocal(instance) {
  return !instance || instance === state.localInstance;
}

function runKey(instance, id) {
  return (isLocal(instance) ? "" : instance + "-") + id;
}

function hashFor(instance, id) {
  return "#run-" + runKey(instance, id);
}

function instanceQuery(instance) {
  return isLocal(instance) ? "" : "instance=" + encodeURIComponent(instance);
}

function relative(iso) {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const s = Math.round((Date.now() - t) / 1000);
  if (s < 0) return "in the future";
  if (s < 45) return "just now";
  const units = [["day", 86400], ["hour", 3600], ["minute", 60]];
  for (const [name, size] of units) {
    if (s >= size * (name === "minute" ? 0.75 : 1)) {
      const n = Math.max(1, Math.round(s / size));
      return n + " " + name + (n === 1 ? "" : "s") + " ago";
    }
  }
  return "just now";
}

function timeNode(iso) {
  if (!iso) return el("span", "muted", "—");
  const t = new Date(iso);
  const node = el("time", null, relative(iso) || iso);
  node.dateTime = iso;
  node.title = Number.isNaN(t.getTime()) ? iso : t.toLocaleString("en-GB", { timeZoneName: "short" });
  return node;
}

function shaNode(sha) {
  if (!sha) return el("span", "muted", "—");
  const text = /^[0-9a-f]{7,40}$/i.test(sha) ? sha.slice(0, 7) : String(sha).slice(0, 12);
  if (!/^[0-9a-f]{7,40}$/i.test(sha)) return el("code", null, text);
  const a = el("a", "mono", text);
  a.href = REPO_COMMIT_URL + sha;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  a.title = sha;
  a.addEventListener("click", (e) => e.stopPropagation());
  return a;
}

function tagsText(tags) {
  return tags && tags.length ? tags.join(", ") : "full";
}

async function api(path, options) {
  const resp = await fetch(path, { ...options, signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) });
  let body = null;
  try { body = await resp.json(); } catch (_) { /* not JSON */ }
  const isObject = body !== null && typeof body === "object" && !Array.isArray(body);
  if (!resp.ok) {
    const err = new Error((isObject && typeof body.detail === "string" && body.detail) || "HTTP " + resp.status);
    err.status = resp.status;
    throw err;
  }
  if (!isObject) throw new Error("unexpected reply from the API");
  return body;
}

/* ---------- banner ---------- */

function renderBanner() {
  const parts = [];
  if (!state.hostsOk || !state.queueOk || !state.historyOk) {
    parts.push("The midichlorian API is unreachable; what follows may be out of date.");
  }
  const down = [...new Set(Object.values(state.peersDown).flat())];
  if (down.length) {
    parts.push("Not reachable: " + down.join(", ") + " (their hosts and runs are missing).");
  }
  if (state.notice) parts.push(state.notice);
  const banner = $("banner");
  banner.hidden = parts.length === 0;
  replace(banner, ...parts.map((p) => el("div", null, p)));
}

function notify(message) {
  state.notice = message;
  renderBanner();
  setTimeout(() => {
    if (state.notice === message) { state.notice = null; renderBanner(); }
  }, NOTICE_MS);
}

/* ---------- Jedi cards ---------- */

function gateBadges(host) {
  const gates = host.gates || {};
  const out = [];
  if (gates.printer_sensitive) {
    const printers = gates.printers || [];
    let busy = false;
    for (const p of printers) {
      if (p.busy) { busy = true; out.push(badge("printer busy — " + p.detail, "warn")); }
      else if (!p.reachable) out.push(badge(p.name + " — " + p.detail, ""));
    }
    if (gates.printers_error) out.push(badge("printer state unknown", "warn"));
    else if (!busy && printers.length) out.push(badge("printers idle", "ok"));
  }
  return out;
}

function deployedLines(deployed) {
  if (!deployed) return [el("div", "muted", "No deploy recorded")];
  const lines = [];
  if (deployed.full) {
    lines.push(el("div", "line", "Full: ", shaNode(deployed.full.sha), timeNode(deployed.full.at)));
  }
  for (const t of deployed.tagged || []) {
    lines.push(el("div", "line", "Tags " + (t.tags || []).join(", ") + ": ", shaNode(t.sha), timeNode(t.at)));
  }
  return lines.length ? lines : [el("div", "muted", "No deploy recorded")];
}

function hostCard(host) {
  const head = el("div", "card-head", el("span", "name", host.name), el("span", "grow"));
  if (host.running) {
    const run = el("a", null, "running #" + host.running);
    run.href = hashFor(host.served_by, host.running);
    head.append(el("span", "badge info", el("span", "dot"), run));
  }
  if (host.queued) head.append(badge(host.queued + " queued", "info"));

  const card = el("div", "card", head, ...deployedLines(host.deployed));
  if (host.last) {
    const last = el("div", "line", "Last run: ", stateBadge(host.last.state));
    const link = el("a", null, "#" + host.last.id);
    link.href = hashFor(host.served_by, host.last.id);
    last.append(link, timeNode(host.last.finished));
    card.append(last);
    if (host.last.reason) card.append(el("div", "muted", host.last.reason));
  }
  const gates = gateBadges(host);
  if (gates.length) card.append(el("div", "badges", ...gates));
  card.append(el("div", "muted", "served by " + host.served_by));
  return card;
}

function renderHosts() {
  if (!changed("hosts", state.hosts, state.hostsOk)) return;
  if (!state.hosts.length) {
    return replace($("hosts"), el("p", "muted", state.hostsOk ? "No hosts reported." : "No data yet."));
  }
  replace($("hosts"), ...state.hosts.map(hostCard));
}

/* ---------- queue and history ---------- */

function cancelButton(job) {
  const key = runKey(job.instance, job.id);
  const armed = state.confirming === key;
  const btn = el("button", "btn" + (armed ? " danger" : ""), armed ? "Are you sure?" : "Cancel");
  btn.type = "button";
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (state.confirming !== key) {
      state.confirming = key;
      rerenderCancel();
      setTimeout(() => {
        if (state.confirming === key) { state.confirming = null; rerenderCancel(); }
      }, CONFIRM_MS);
      return;
    }
    state.confirming = null;
    cancelRun(job);
  });
  return btn;
}

function rerenderCancel() {
  renderQueue();
  if (state.detailJob) renderDetail(state.detailJob);
}

async function cancelRun(job) {
  const q = instanceQuery(job.instance);
  try {
    await api("/api/runs/" + job.id + "/cancel" + (q ? "?" + q : ""), { method: "POST" });
  } catch (err) {
    notify(err.status === 409
      ? "Run #" + job.id + " could not be cancelled: it is no longer queued or waiting."
      : "Cancelling run #" + job.id + " failed: " + err.message);
  }
  refresh();
}

function jobRow(job, withCancel) {
  const row = el("div", "row clickable");
  row.tabIndex = 0;
  const key = runKey(job.instance, job.id);
  if (state.selected && runKey(state.selected.instance, state.selected.id) === key) row.classList.add("selected");
  const meta = el("div", "meta", shaNode(job.sha), " · " + tagsText(job.tags));
  if (job.state === "waiting" && job.detail && job.detail.gate) {
    meta.append(" · ", badge(job.detail.gate, "warn"));
  } else if (job.reason || (job.detail && job.detail.reason)) {
    meta.append(" · " + (job.reason || job.detail.reason));
  }
  const when = job.finished || job.started || job.created;
  meta.append(" · ", timeNode(when));
  if (job.requesters && job.requesters.length) meta.append(" · " + job.requesters.join(", "));
  if (!isLocal(job.instance)) meta.append(" · on " + job.instance);

  const actions = el("div", "actions");
  if (withCancel && PENDING.includes(job.state)) actions.append(cancelButton(job));
  row.append(el("div", "mono", "#" + job.id), el("div", "cell-host", job.host), stateBadge(job.state), meta, actions);

  const open = () => { location.hash = hashFor(job.instance, job.id); };
  row.addEventListener("click", open);
  row.addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") && e.target === row) { e.preventDefault(); open(); }
  });
  return row;
}

function renderQueue() {
  if (!changed("queue", state.queue, state.queueOk, state.confirming, selectedKey())) return;
  if (!state.queue.length) {
    return replace($("queue"), el("p", "muted", state.queueOk ? "Nothing queued or running." : "No data yet."));
  }
  replace($("queue"), ...state.queue.map((j) => jobRow(j, true)));
}

function renderHistory() {
  if (!changed("history", state.history, state.historyOk, selectedKey())) return;
  if (!state.history.length) {
    return replace($("history"), el("p", "muted", state.historyOk ? "No runs yet." : "No data yet."));
  }
  replace($("history"), ...state.history.map((j) => jobRow(j, false)));
}

/* ---------- run detail ---------- */

function kv(label, ...value) {
  return [el("dt", null, label), el("dd", null, ...value)];
}

function recapNodes(recap) {
  if (!recap) return null;
  const names = ["ok", "changed", "unreachable", "failed", "skipped"];
  const parts = names.filter((n) => recap[n] !== null && recap[n] !== undefined).map((n) => {
    const bad = (n === "failed" || n === "unreachable") && recap[n] > 0;
    return badge(n + " " + recap[n], bad ? "bad" : "");
  });
  return parts.length ? el("div", "recap", ...parts) : null;
}

function renderDetail(job) {
  if (!changed("detail", job, state.confirming)) return;
  const d = job.detail || {};
  const body = $("detail-body");
  const pairs = [];
  pairs.push(...kv("Host", job.host + (isLocal(job.instance) ? "" : " (via " + job.instance + ")")));
  pairs.push(...kv("State", stateBadge(job.state)));
  if (d.reason) pairs.push(...kv("Reason", d.reason));
  if (d.gate) pairs.push(...kv("Waiting on", badge(d.gate, "warn")));
  pairs.push(...kv("Requested", shaNode(d.requested_sha || job.sha)));
  if (d.deployed_sha) pairs.push(...kv("Deployed", shaNode(d.deployed_sha)));
  pairs.push(...kv("Tags", tagsText(job.tags)));
  if (job.requesters && job.requesters.length) pairs.push(...kv("Requested by", job.requesters.join(", ")));
  if (job.override_printer) pairs.push(...kv("Printer check", badge("overridden", "warn")));
  pairs.push(...kv("Created", timeNode(job.created)));
  if (job.started) pairs.push(...kv("Started", timeNode(job.started)));
  if (job.finished) pairs.push(...kv("Finished", timeNode(job.finished)));
  if (d.failing_task) pairs.push(...kv("Failing task", el("code", null, d.failing_task)));

  const parts = [el("dl", "kv", ...pairs)];

  const recap = recapNodes(d.recap);
  if (recap) parts.push(el("h3", "detail-h", "Recap"), recap);

  if (Array.isArray(d.checks) && d.checks.length) {
    const items = d.checks.map((c) => el("li", null, badge(c.ok ? "ok" : "fail", c.ok ? "ok" : "bad"), " ",
      el("strong", null, c.name), c.detail ? " — " + c.detail : ""));
    parts.push(el("h3", "detail-h", "Checks"), el("ul", "checks", ...items));
  }
  if (Array.isArray(d.warnings) && d.warnings.length) {
    parts.push(el("h3", "detail-h", "Warnings"), el("ul", "plain", ...d.warnings.map((w) => el("li", null, w))));
  }
  if (d.log_tail) {
    const tail = Array.isArray(d.log_tail) ? d.log_tail.join("\n") : String(d.log_tail);
    parts.push(el("h3", "detail-h", "Log tail"), el("pre", "tail", tail));
  }

  const actions = el("div", "actions");
  const logBtn = el("button", "btn", job.state === "running" ? "Follow log" : "Show full log");
  logBtn.type = "button";
  logBtn.addEventListener("click", () => openLog(job, true));
  actions.append(logBtn);
  if (PENDING.includes(job.state)) actions.append(" ", cancelButton(job));
  parts.push(el("div", "detail-h", actions));

  $("detail-h").textContent = "Run #" + job.id + " on " + job.host;
  replace(body, ...parts);
  $("detail").hidden = false;
}

async function loadDetail(sel, scroll) {
  if (!state.localInstance) return;
  const n = ++state.seq.detail;
  const q = instanceQuery(sel.instance);
  const current = () => state.selected === sel && n === state.seq.detail;
  try {
    const job = await api("/api/runs/" + sel.id + (q ? "?" + q : ""));
    if (!current()) return;
    state.detailJob = job;
    renderDetail(job);
    if (scroll) $("detail").scrollIntoView({ behavior: "smooth", block: "nearest" });
    const key = runKey(job.instance, job.id);
    if (job.state === "running" && isLocal(job.instance) && !state.autoOpened.has(key)) {
      state.autoOpened.add(key);
      openLog(job, false);
    }
  } catch (err) {
    if (!current()) return;
    state.detailJob = null;
    if (changed("detail", "error", sel, err.message)) {
      $("detail-h").textContent = "Run #" + sel.id;
      replace($("detail-body"), el("p", "muted", "Could not load this run: " + err.message));
    }
    $("detail").hidden = false;
  }
}

function applyHash() {
  const m = /^#run-(?:(.+)-)?(\d+)$/.exec(location.hash);
  state.detailJob = null;
  delete state.sig.detail;
  if (!m) {
    state.selected = null;
    $("detail").hidden = true;
  } else {
    state.selected = { instance: m[1] || null, id: Number(m[2]) };
    loadDetail(state.selected, true);  // waits for the instance name if it is not known yet
  }
  renderQueue();
  renderHistory();
}

/* ---------- live log ---------- */

function stripAnsi(text) {
  return text.replace(/\u001b\[[0-9;?]*[A-Za-z]/g, "");
}

function closeLog() {
  const log = state.log;
  if (log) {
    if (log.source) log.source.close();
    if (log.timer) clearTimeout(log.timer);
    if (log.frame) cancelAnimationFrame(log.frame);
  }
  state.log = null;
  $("log-panel").hidden = true;
}

function appendLog(text) {
  const log = state.log;
  log.pending = (log.pending || "") + text;
  if (log.pending.length > LOG_MAX_CHARS) log.pending = log.pending.slice(-LOG_MAX_CHARS);
  if (log.frame) return;
  log.frame = requestAnimationFrame(() => {
    log.frame = null;
    if (state.log !== log || !log.pending) return;
    const out = $("log-output");
    if (!log.node) {
      log.node = document.createTextNode("");
      out.replaceChildren(log.node);
    }
    log.node.appendData(stripAnsi(log.pending));
    log.pending = "";
    if (log.node.length > LOG_MAX_CHARS) log.node.deleteData(0, log.node.length - LOG_MAX_CHARS);
    if (state.stick) out.scrollTop = out.scrollHeight;
  });
}

function logStatus(text) {
  $("log-status").textContent = text;
}

function openLog(job, scroll) {
  closeLog();
  const key = runKey(job.instance, job.id);
  const log = { key, source: null, timer: null, node: null, job };
  state.log = log;
  state.stick = true;
  $("log-title").textContent = "Log for run #" + job.id + " on " + job.host;
  $("log-output").replaceChildren();
  $("log-panel").hidden = false;
  if (job.state === "running" && isLocal(job.instance)) followLog(log);
  else pageLog(log, 0);
  if (scroll) $("log-panel").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function followLog(log) {
  logStatus("following…");
  const source = new EventSource("/api/runs/" + log.job.id + "/log?follow=1");
  log.source = source;
  source.onmessage = (e) => { if (state.log === log) appendLog(e.data + "\n"); };
  source.addEventListener("end", (e) => {
    source.close();
    if (state.log !== log) return;
    logStatus("finished (" + e.data + ")");
    refresh();
  });
  source.onerror = () => {
    // EventSource would resume from the start and repeat lines, so start over by hand.
    source.close();
    if (state.log !== log) return;
    logStatus("connection lost, retrying…");
    log.timer = setTimeout(() => {
      if (state.log !== log) return;
      log.node = null;
      log.pending = "";
      $("log-output").replaceChildren();
      followLog(log);
    }, PEER_LOG_POLL_MS);
  };
}

async function pageLog(log, since) {
  if (state.log !== log) return;
  let next = since;
  let done = false;
  try {
    const q = instanceQuery(log.job.instance);
    const page = await api("/api/runs/" + log.job.id + "/log?since=" + since + (q ? "&" + q : ""));
    if (state.log !== log) return;
    if (page.text) appendLog(page.text);
    next = page.next;
    const job = await api("/api/runs/" + log.job.id + (q ? "?" + q : ""));
    done = TERMINAL.includes(job.state) && next === since && !page.text;
    logStatus(done ? "finished (" + job.state + ")" : "updating every 3 s…");
  } catch (err) {
    if (state.log !== log) return;
    const permanent = err.status >= 400 && err.status < 500 && err.status !== 408 && err.status !== 429;
    if (permanent) return logStatus("log not available (" + err.message + ")");
    logStatus("log unavailable: " + err.message + "; retrying…");
  }
  if (state.log !== log || done) return;
  const more = next !== since;
  log.timer = setTimeout(() => pageLog(log, next), more ? 0 : PEER_LOG_POLL_MS);
}

function autoOpenRunning() {
  if (state.log || !state.localInstance) return;
  const running = state.queue.find((j) => j.state === "running" && isLocal(j.instance));
  if (!running) return;
  const key = runKey(running.instance, running.id);
  if (state.autoOpened.has(key)) return;
  state.autoOpened.add(key);
  openLog(running, false);
}

/* ---------- polling ---------- */

/* One refresh at a time: a call while one is in flight asks for another straight after it. */
let refreshing = null;
let refreshAgain = false;

function refresh() {
  if (refreshing) { refreshAgain = true; return refreshing; }
  refreshing = doRefresh().finally(() => {
    refreshing = null;
    if (refreshAgain) { refreshAgain = false; refresh(); }
  });
  return refreshing;
}

async function loadInfo() {
  try {
    const info = await api("/api/info");
    if (typeof info.instance !== "string" || !info.instance) throw new Error("no instance name");
    state.localInstance = info.instance;
    $("instance-name").textContent = "· " + info.instance;
    applyHash();  // run keys and follow-or-page decisions need the instance name
    return true;
  } catch (_) {
    return false;
  }
}

/* Each endpoint is applied as it arrives; a sequence number drops a reply that a newer request overtook. */
function load(name, path, apply) {
  const n = ++state.seq[name];
  return api(path).then(
    (v) => {
      if (n !== state.seq[name]) return;
      state[name + "Ok"] = true;
      apply(v);
      state.peersDown[name] = Array.isArray(v.peers_unreachable) ? v.peers_unreachable : [];
      renderBanner();
      render(name);
    },
    () => {
      if (n !== state.seq[name]) return;
      state[name + "Ok"] = false;
      renderBanner();
      render(name);
    },
  );
}

function render(name) {
  ({ hosts: renderHosts, queue: renderQueue, history: renderHistory })[name]();
}

async function doRefresh() {
  if (!state.localInstance && !(await loadInfo())) {
    state.hostsOk = state.queueOk = state.historyOk = false;
    return renderBanner();
  }
  await Promise.all([
    load("hosts", "/api/hosts", (v) => { state.hosts = Array.isArray(v.hosts) ? v.hosts : []; }),
    load("queue", "/api/queue", (v) => { state.queue = Array.isArray(v.jobs) ? v.jobs : []; }),
    load("history", "/api/runs?limit=50", (v) => { state.history = Array.isArray(v.runs) ? v.runs : []; }),
  ]);
  autoOpenRunning();
  if (state.selected) loadDetail(state.selected, false);
}

async function poll() {
  try { await refresh(); } finally { setTimeout(poll, POLL_MS); }
}

function start() {
  applyHash();
  poll();
}

$("detail-close").addEventListener("click", () => {
  history.replaceState(null, "", location.pathname + location.search);
  applyHash();
});
$("log-close").addEventListener("click", closeLog);
$("log-output").addEventListener("scroll", () => {
  const o = $("log-output");
  state.stick = o.scrollTop + o.clientHeight >= o.scrollHeight - 24;
});
window.addEventListener("hashchange", applyHash);
start();
