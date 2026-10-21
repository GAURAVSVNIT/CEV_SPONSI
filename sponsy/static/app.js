/* SPONSY web UI.
 *
 * Talks only to the JSON API in sponsy/web.py. Every value from the database
 * reaches the page through textContent (see h()), never innerHTML: company
 * names, email bodies and fetched page excerpts are all untrusted text.
 */
"use strict";

(() => {
  const TABS = ["queue", "prospects", "activity", "agent"];
  const POLL_MS = 15000;
  const TASK_POLL_MS = 1500;

  const KIND_LABELS = {
    initial: "first touch",
    returning: "returning sponsor",
    nudge: "nudge, day 3",
    new_angle: "new angle, day 7",
    last_touch: "last touch, day 14",
    needs_info: "answering their question",
  };

  const TASK_TITLES = {
    run: "Daily run",
    discover: "Discover prospects",
    "find-contacts": "Find addresses",
    doctor: "Health check",
  };

  const S = {
    tab: "queue",
    overview: null,
    drafts: [],
    prospects: [],
    statuses: [],
    log: null,
    outbox: null,
    filter: "all",
    search: "",
    detailId: null,
    detail: null,
    editing: new Map(),       // draft id -> {mode, subject, body, instruction}
    busy: new Map(),          // draft id -> verdict in flight
    task: null,
    taskTimer: null,
    added: new Set(),         // companies added from a discover result
    used: new Set(),          // prospect ids given an address from find-contacts
    connError: "",
  };

  // ---------------------------------------------------------------- helpers

  const $ = (sel, root = document) => root.querySelector(sel);

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const [key, value] of Object.entries(attrs)) {
        if (value === false || value === null || value === undefined) continue;
        if (key === "class") el.className = value;
        else if (key.startsWith("on") && typeof value === "function") {
          el.addEventListener(key.slice(2), value);
        } else if (key === "value") el.value = value;
        else if (value === true) el.setAttribute(key, "");
        else el.setAttribute(key, String(value));
      }
    }
    add(el, children);
    return el;
  }

  function add(el, children) {
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : String(child));
    }
    return el;
  }

  function safeUrl(url) {
    try {
      const u = new URL(String(url || ""), location.href);
      return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
    } catch (_) {
      return null;
    }
  }

  function extLink(url, text) {
    const href = safeUrl(url);
    if (!href) return h("span", null, text || url || "");
    return h("a", { href, target: "_blank", rel: "noopener noreferrer" }, text || url);
  }

  function hostOf(url) {
    try { return new URL(url).host.replace(/^www\./, ""); } catch (_) { return url || ""; }
  }

  function badge(text, kind) {
    return h("span", { class: "badge" + (kind ? " " + kind : "") }, text);
  }

  function statusBadge(status) {
    return h("span", { class: "badge st-" + status }, status);
  }

  function verdictBadge(d) {
    if (d.sent_at) return badge("sent " + shortDate(d.sent_at), "ok");
    if (!d.verdict) return badge("waiting for you", "violet");
    if (d.send_state === "queued" || d.send_state === "sending") {
      return badge("in the outbox", "info");
    }
    if (d.send_state === "failed" || d.send_state === "cancelled") {
      return badge("approved, not sent", "bad");
    }
    const kinds = { approved: "ok", edited: "ok", skipped: "", rejected: "bad", superseded: "" };
    return badge(d.verdict, kinds[d.verdict] || "");
  }

  const CLOSED = ["declined", "cold", "interested"];

  // An address the outbox or a bounce report proved dead. It is blocked like
  // a "stop" reply, but the fix is different: find another address.
  function bounced(blocked, reason) {
    return !!blocked && /^bounce/.test(reason || "");
  }

  function needsAddress(p) {
    if (CLOSED.includes(p.status)) return false;
    return !(p.email || "").trim() || bounced(p.blocked, p.blocked_reason);
  }

  function blockBadge(blocked, reason) {
    if (!blocked) return null;
    const b = bounced(blocked, reason) ? badge("bounced", "bad") : badge("do-not-contact", "bad");
    if (reason) b.title = reason;
    return b;
  }

  function touches(n) {
    const box = h("span", { class: "touches", title: n + " of 4 touches" });
    for (let i = 0; i < 4; i++) box.append(h("i", { class: i < n ? "on" : "" }));
    return box;
  }

  function parseDate(value) {
    if (!value) return null;
    const s = String(value);
    const d = new Date(s.length === 10 ? s + "T00:00:00" : s);
    return isNaN(d) ? null : d;
  }

  function shortDate(value) {
    const d = parseDate(value);
    if (!d) return value || "";
    return d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
  }

  function dateTime(value) {
    const d = parseDate(value);
    if (!d) return value || "";
    return d.toLocaleString(undefined, { day: "numeric", month: "short",
                                         hour: "2-digit", minute: "2-digit" });
  }

  function ago(value) {
    const d = parseDate(value);
    if (!d) return "";
    const s = Math.round((Date.now() - d.getTime()) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + " min ago";
    if (s < 86400) return Math.floor(s / 3600) + " h ago";
    if (s < 86400 * 30) return Math.floor(s / 86400) + " d ago";
    return shortDate(value);
  }

  function relDay(value) {
    const d = parseDate(value);
    if (!d) return value || "";
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const days = Math.round((d - today) / 86400000);
    if (days === 0) return "today";
    if (days === 1) return "tomorrow";
    if (days === -1) return "yesterday";
    return days > 0 ? "in " + days + " d" : -days + " d overdue";
  }

  function money(n) {
    return "$" + Number(n || 0).toFixed(4);
  }

  function store(key, value) {
    try {
      if (value === undefined) return localStorage.getItem(key);
      if (value === null) localStorage.removeItem(key);
      else localStorage.setItem(key, value);
    } catch (_) { /* private window: fine */ }
    return null;
  }

  function taskRunning() {
    return !!(S.task && S.task.state === "running");
  }

  // ---------------------------------------------------------------- api

  let token = store("sponsy.token") || "";

  async function api(method, path, body) {
    const opts = { method, headers: {} };
    if (token) opts.headers["X-Sponsy-Token"] = token;
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, opts);
    } catch (_) {
      throw new Error("Cannot reach the SPONSY server. Is `python run.py web` still running?");
    }
    if (res.status === 401) {
      askToken();
      throw new Error("This server needs a token.");
    }
    let data = null;
    try { data = await res.json(); } catch (_) { /* not JSON */ }
    if (!res.ok) throw new Error((data && data.error) || "HTTP " + res.status);
    return data;
  }

  // ---------------------------------------------------------------- toast / modal

  function toast(text, isError) {
    const el = h("div", { class: "toast" + (isError ? " error" : "") }, text);
    $("#toasts").append(el);
    setTimeout(() => el.remove(), isError ? 7000 : 4500);
  }

  /** A modal. `content` is a node; `onConfirm` may throw to keep it open. */
  function modal({ title, content, confirm = "OK", cancel = "Cancel", danger = false,
                   onConfirm }) {
    return new Promise((resolve) => {
      const root = $("#modal-root");
      const error = h("p", { class: "hidden" });
      const ok = h("button", { class: "btn " + (danger ? "danger-solid" : "primary"),
                               type: "submit" }, confirm);
      const close = (value) => {
        document.removeEventListener("keydown", onKey);
        root.replaceChildren();
        resolve(value);
      };
      const onKey = (e) => { if (e.key === "Escape") close(false); };
      const form = h("form", { class: "modal", role: "dialog", "aria-modal": "true",
        onsubmit: async (e) => {
          e.preventDefault();
          if (!onConfirm) return close(true);
          ok.disabled = true;
          try {
            close((await onConfirm()) ?? true);
          } catch (err) {
            ok.disabled = false;
            error.textContent = err.message;
            error.className = "badge bad";
          }
        } },
        h("h3", null, title),
        h("div", { class: "modal-body" }, content, error),
        h("div", { class: "modal-actions" },
          cancel ? h("button", { class: "btn ghost", type: "button",
                                 onclick: () => close(false) }, cancel) : null,
          ok));
      root.replaceChildren(h("div", { class: "modal-wrap",
        onclick: (e) => { if (e.target === e.currentTarget) close(false); } }, form));
      document.addEventListener("keydown", onKey);
      const first = form.querySelector("input, textarea");
      (first || ok).focus();
    });
  }

  function askToken() {
    if ($("#modal-root").childElementCount) return;
    const input = h("input", { type: "password", autocomplete: "off",
                               placeholder: "SPONSY_WEB_TOKEN" });
    modal({
      title: "Token required",
      content: [h("p", null, "This server was started with a token. Paste it to continue."),
                input],
      confirm: "Continue",
      cancel: null,
      onConfirm: () => {
        token = input.value.trim();
        store("sponsy.token", token || null);
        setTimeout(refresh, 0);
      },
    });
  }

  // ---------------------------------------------------------------- data

  async function refresh(force) {
    try {
      const [overview, drafts] = await Promise.all([
        api("GET", "/api/overview"), api("GET", "/api/drafts")]);
      S.overview = overview;
      S.drafts = drafts.drafts;
      setTask(overview.task);
      S.connError = "";
    } catch (err) {
      S.connError = err.message;
      renderBanners();
      return;
    }
    renderChrome();
    if (S.tab === "queue") renderQueue(force);
    if (S.tab === "prospects") await loadProspects();
    if (S.tab === "activity") await loadActivity();
    if (S.tab === "agent") renderAgent();
  }

  async function loadProspects() {
    try {
      const data = await api("GET", "/api/prospects");
      S.prospects = data.prospects;
      S.statuses = data.statuses;
    } catch (err) {
      toast(err.message, true);
    }
    renderProspects();
  }

  async function loadActivity() {
    try {
      const [log, outbox] = await Promise.all([api("GET", "/api/log"),
                                               api("GET", "/api/outbox")]);
      S.log = log;
      S.outbox = outbox;
    } catch (err) {
      toast(err.message, true);
    }
    renderActivity();
  }

  function setTask(task) {
    const was = S.task;
    S.task = task;
    if (task && task.state === "running") {
      if (!S.taskTimer) S.taskTimer = setInterval(pollTask, TASK_POLL_MS);
    } else if (S.taskTimer) {
      clearInterval(S.taskTimer);
      S.taskTimer = null;
    }
    if (was && was.state === "running" && task && task.id === was.id &&
        task.state !== "running") {
      if (task.state === "done") toast(taskDoneLine(task));
      else toast(task.name + " failed: " + task.error, true);
      setTimeout(() => refresh(true), 0);
    }
  }

  async function pollTask() {
    try {
      const data = await api("GET", "/api/task");
      setTask(data.task);
      renderChrome();
      if (S.tab === "agent") renderAgent();
    } catch (_) { /* the next poll will tell */ }
  }

  function taskDoneLine(task) {
    const r = task.result || {};
    if (task.name === "run") return "Run finished: " + (r.summary || "done");
    if (task.name === "discover") return "Discover found " + (r.fresh || 0) + " new compan" +
      ((r.fresh || 0) === 1 ? "y" : "ies");
    if (task.name === "find-contacts") return "Contact search finished";
    return "Health check finished";
  }

  // ---------------------------------------------------------------- chrome

  function renderChrome() {
    const ov = S.overview;
    if (!ov) return;
    const ev = ov.event;
    $("#event-name").textContent = [ev.name, ev.date].filter(Boolean).join(" · ");
    document.title = (ov.stats.pending ? "(" + ov.stats.pending + ") " : "") + "SPONSY";

    const chips = Object.entries(ov.channels).map(([name, value]) => {
      const off = ["none", "off", "manual", "fake"].includes(value);
      return h("span", { class: "chip " + (off ? "warn" : "on"), title: name + ": " + value },
               h("span", { class: "dot" }), name, " ", h("b", null, value));
    });
    $("#channels").replaceChildren(...chips);

    const q = $("#count-queue");
    q.textContent = ov.stats.pending;
    q.className = "count" + (ov.stats.pending ? " hot" : "");
    $("#count-prospects").textContent = ov.stats.total;
    $("#count-agent").classList.toggle("hidden", !taskRunning());
    renderBanners();
  }

  function renderBanners() {
    const ov = S.overview;
    const out = [];
    if (S.connError) out.push(h("div", { class: "banner warn" }, h("strong", null, "Offline. "),
                                S.connError));
    if (ov) {
      const mode = ov.mode;
      if (taskRunning()) {
        out.push(h("div", { class: "banner info" },
          h("strong", null, "The agent is running " + S.task.name + ". "),
          "Verdicts open again when it finishes. ",
          h("a", { href: "#agent" }, "Watch it")));
      }
      if (mode.offline) {
        out.push(h("div", { class: "banner info" }, h("strong", null, "Offline mode. "),
          "Stub model, frozen sources, no network. Nothing is sent."));
      } else if (mode.dry_run) {
        out.push(h("div", { class: "banner info" }, h("strong", null, "Dry run. "),
          "Send writes an .eml to the outbox and contacts no mail provider."));
      } else if (ov.channels.mail === "manual") {
        out.push(h("div", { class: "banner info" }, h("strong", null, "No mail provider. "),
          "Send writes an .eml to data/outbox for you to send by hand. Nothing leaves this machine."));
      }
      const sends = !mode.offline && !mode.dry_run && ov.channels.mail !== "manual";
      if (sends && mode.send_only_to && mode.send_only_to.length) {
        out.push(h("div", { class: "banner warn" }, h("strong", null, "SEND_ONLY_TO is set. "),
          "Only " + mode.send_only_to.join(", ") + " will actually receive mail; " +
          "every other Send is held as an .eml."));
      }
      const ob = ov.outbox;
      if (ob && ob.paused) {
        out.push(h("div", { class: "banner warn" }, h("strong", null, "Sending is paused. "),
          ob.paused.reason + (ob.paused.until ? " It resumes on its own at " +
            dateTime(ob.paused.until) + ". " : " "),
          h("button", { class: "btn small", type: "button", onclick: resumeSending },
            "Resume sending")));
      } else if (ob && ob.queued) {
        out.push(h("div", { class: "banner info" },
          h("strong", null, ob.queued + " approved email" + (ob.queued === 1 ? "" : "s") +
            " waiting for a send slot. "),
          "They go out on their own at the paced rate. ",
          h("a", { href: "#activity" }, "See the outbox")));
      }
    }
    $("#banners").replaceChildren(...out);
  }

  function setTab(tab) {
    S.tab = tab;
    for (const t of TABS) {
      $("#view-" + t).classList.toggle("hidden", t !== tab);
    }
    document.querySelectorAll(".tab").forEach((b) => {
      const on = b.dataset.tab === tab;
      b.classList.toggle("active", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
  }

  // ---------------------------------------------------------------- queue

  function renderQueue(force) {
    const root = $("#view-queue");
    const ov = S.overview;
    if (!ov) return;
    // A poll must not wipe a rewrite someone is halfway through typing.
    if (!force && (S.editing.size || S.busy.size) && root.childElementCount) return;
    const n = S.drafts.length;
    root.replaceChildren(
      statsStrip(ov.stats),
      h("div", { class: "grid" },
        h("div", null,
          h("div", { class: "section-head" },
            h("h2", null, "Waiting for your verdict"),
            h("span", { class: "muted" }, n ? n + " draft" + (n === 1 ? "" : "s") +
              ". Nothing goes out until you press Send." : "")),
          n ? h("div", { class: "cards" }, S.drafts.map(draftCard)) : emptyQueue()),
        h("aside", null, upcomingPanel(ov.upcoming), runsPanel(ov.runs))));
  }

  function statsStrip(st) {
    const tile = (label, value, opts = {}) => h("div", {
      class: "stat" + (opts.hot ? " hot" : ""),
      role: opts.href ? "link" : null,
      tabindex: opts.href ? "0" : null,
      onclick: opts.href ? () => { if (opts.filter) S.filter = opts.filter;
                                   location.hash = opts.href; } : null,
    }, h("div", { class: "stat-label" }, label), h("div", { class: "stat-value" }, value));
    return h("div", { class: "stats" },
      tile("Waiting for you", st.pending, { hot: st.pending > 0 }),
      tile("Emails sent", st.sent, { href: "#activity" }),
      tile("Prospects", st.total, { href: "#prospects", filter: "all" }),
      tile("Need an address", st.awaiting_contact, { href: "#prospects", filter: "noaddress" }),
      tile("Do-not-contact", st.blocked));
  }

  function emptyQueue() {
    return h("div", { class: "empty" },
      h("div", { class: "big" }, "Nothing waiting for you"),
      h("div", null, "Run the agent to research and draft the prospects that are due."),
      h("button", { class: "btn primary", type: "button",
                    onclick: () => { location.hash = "#agent"; } }, "Go to the agent"));
  }

  function draftCard(d) {
    const state = S.editing.get(d.id);
    const mode = state ? state.mode : null;
    const unverified = d.failed.length > 0;
    const kind = d.kind === "returning" ? badge(KIND_LABELS.returning, "violet")
      : d.kind === "initial" ? badge(KIND_LABELS.initial)
      : badge(KIND_LABELS[d.kind] || d.kind, "info");

    return h("article", { class: "card" + (unverified ? " unverified" : ""),
                          id: "draft-" + d.id },
      h("div", { class: "card-head" },
        h("div", null,
          h("div", { class: "card-title" },
            h("a", { href: "#prospects/" + encodeURIComponent(d.prospect_id) }, d.company),
            " ", h("span", { class: "faint mono" }, d.prospect_id)),
          h("div", { class: "card-sub" },
            "To ", h("b", null, d.email || "(no address)"),
            d.contact_name ? " · " + d.contact_name : "",
            " · d:" + d.id + " · " + ago(d.created_at))),
        h("div", { class: "badges" }, kind,
          unverified ? badge("unverified", "warn") : badge("verified", "ok"))),
      mode === "edit" ? editEditor(d, state) : [
        h("div", { class: "subject" }, h("span", { class: "label" }, "Subject"), d.subject),
        h("div", { class: "body" }, d.body)],
      draftMeta(d),
      draftIssues(d),
      draftClaims(d),
      mode === "redo" ? redoEditor(d, state) : null,
      draftActions(d, mode));
  }

  function draftMeta(d) {
    const score = d.ai_tell_score;
    return h("div", { class: "meta" },
      h("span", null, h("b", null, d.words), " words"),
      h("span", null, h("b", null, d.grounded + "/" + d.claims.length), " claims traced to ",
        h("b", null, d.sources), " source" + (d.sources === 1 ? "" : "s")),
      h("span", null, "human-score ", h("b", null,
        score === null || score === undefined ? "n/a" : Number(score).toFixed(2))),
      d.humanized ? h("span", null, "humanized") : null,
      d.attachment ? h("span", null, "+ ", h("b", null, d.attachment)) : null);
  }

  function draftIssues(d) {
    const robotic = (d.soft_failed.includes("sounds_human") ||
                     d.failed.includes("sounds_human")) && d.lint.length;
    const risks = d.spam_risks || [];
    if (!d.failed.length && !robotic && !risks.length) return null;
    return h("div", { class: "issues" },
      d.failed.length ? h("div", null, h("b", null, "Unverified. "), "Failed: " +
                          d.failed.join(", ")) : null,
      d.ungrounded.length ? h("ul", null, d.ungrounded.map((c) =>
        h("li", null, "Not in the sources: ", c))) : null,
      robotic ? h("div", null, h("b", null, "Reads a bit robotic: "), d.lint.join("; ")) : null,
      risks.length ? h("div", null, h("b", null, "Spam-filter risk: "), risks.join("; ")) : null);
  }

  function draftClaims(d) {
    if (!d.claims.length) return null;
    return h("details", { class: "claims" },
      h("summary", null, "Claims and where they came from"),
      h("ul", null, d.claims.map((c) => h("li", null,
        c.grounded === false ? badge("not found", "bad")
          : c.grounded ? badge("traced", "ok") : badge("?"),
        h("div", null, c.claim || JSON.stringify(c),
          c.why ? h("div", { class: "why" }, c.why) : null)))));
  }

  function editEditor(d, state) {
    const subject = h("input", { type: "text", value: state.subject,
      "aria-label": "Subject", oninput: (e) => { state.subject = e.target.value; } });
    const body = h("textarea", { class: "tall", "aria-label": "Email body",
      oninput: (e) => { state.body = e.target.value; } });
    body.value = state.body;
    return h("div", { class: "editor" },
      h("label", { class: "field" }, h("span", null, "Subject"), subject),
      h("label", { class: "field" }, h("span", null, "Body"), body),
      h("div", { class: "hint" }, "Your version is sent as written, and the before/after " +
        "pair is kept as a correction the drafter learns from on the next run."));
  }

  function redoEditor(d, state) {
    const input = h("textarea", {
      placeholder: "e.g. shorter, mention their API workshop, drop the second paragraph",
      "aria-label": "What should change",
      oninput: (e) => { state.instruction = e.target.value; } });
    input.value = state.instruction || "";
    return h("div", { class: "editor" },
      h("label", { class: "field" }, h("span", null, "What should change?"), input),
      h("div", { class: "hint" }, "This draft is retired and a new one is written from " +
        "the same sources. It comes back here for a verdict."));
  }

  function draftActions(d, mode) {
    const inflight = S.busy.get(d.id);
    const locked = !!inflight || taskRunning();
    const btn = (label, verdict, cls, onclick) => h("button", {
      class: "btn " + (cls || ""), type: "button", disabled: locked, onclick },
      inflight === verdict ? h("span", { class: "spin" }) : null, label);

    if (mode === "edit") {
      const st = S.editing.get(d.id);
      return h("div", { class: "actions" },
        btn("Save and send", "edit", "primary", () =>
          verdict(d, "edit", { subject: st.subject, body: st.body })),
        btn("Cancel", "", "ghost", () => stopEditing(d)));
    }
    if (mode === "redo") {
      const st = S.editing.get(d.id);
      return h("div", { class: "actions" },
        btn("Write a new draft", "redo", "primary", () =>
          verdict(d, "redo", { instruction: st.instruction || "" })),
        btn("Cancel", "", "ghost", () => stopEditing(d)));
    }
    return h("div", { class: "actions" },
      btn("Send", "send", "primary", () => verdict(d, "send")),
      btn("Edit", "", "", () => startEditing(d, "edit")),
      btn("Redo", "", "", () => startEditing(d, "redo")),
      btn("Skip 3 days", "skip", "", () => verdict(d, "skip")),
      h("span", { class: "spacer" }),
      btn("Never", "never", "danger", () => verdict(d, "never")));
  }

  function startEditing(d, mode) {
    S.editing.set(d.id, { mode, subject: d.subject, body: d.body, instruction: "" });
    replaceCard(d);
    const card = $("#draft-" + d.id);
    const field = card && card.querySelector(mode === "edit" ? "textarea" : ".editor textarea");
    if (field) field.focus();
  }

  function stopEditing(d) {
    S.editing.delete(d.id);
    replaceCard(d);
  }

  function replaceCard(d) {
    const old = $("#draft-" + d.id);
    if (old) old.replaceWith(draftCard(d));
  }

  function sendExplainer(d) {
    const ov = S.overview;
    const mode = ov.mode;
    const mail = ov.channels.mail;
    const to = (d.email || "").toLowerCase();
    if (mode.offline || mode.dry_run) {
      return (mode.offline ? "Offline" : "Dry run") + ": the email is written to the " +
             "outbox as an .eml and no mail provider is contacted.";
    }
    if (mail === "manual") {
      return "Nothing leaves this machine: SPONSY writes an .eml to data/outbox for you " +
             "to send by hand. The ladder still schedules the next touch.";
    }
    if (mode.send_only_to.length && !mode.send_only_to.includes(to)) {
      return "SEND_ONLY_TO is set and " + d.email + " is not on it, so the .eml is " +
             "written and nothing is sent.";
    }
    const ob = ov.outbox || {};
    const room = ob.cap_today ? " Today: " + ob.sent_today + " of " + ob.cap_today +
      " sends used." : "";
    if (ob.paused) {
      return "This approves a real email to " + d.email + ". Sending is paused (" +
             ob.paused.reason + "), so it waits in the outbox until that clears.";
    }
    return "This approves a real email to " + d.email + " through " + mail + ". It " +
           "leaves now if a send slot is free, otherwise at the next one: sends are " +
           "spaced out and capped per day so the account keeps its reputation." + room;
  }

  async function resumeSending() {
    try {
      const out = await api("POST", "/api/outbox/resume", {});
      toast(out.message);
    } catch (err) {
      toast(err.message, true);
    }
    await refresh(true);
  }

  async function verdict(d, which, payload) {
    if (which === "send") {
      const ok = await modal({
        title: "Send to " + d.company + "?",
        content: [h("p", null, h("b", null, d.subject)), h("p", null, sendExplainer(d))],
        confirm: "Send",
      });
      if (!ok) return;
    }
    if (which === "never") {
      const ok = await modal({
        title: "Never contact " + d.company + "?",
        content: h("p", null, "The prospect is marked declined permanently and " +
          (d.email || "their address") + " goes on the do-not-contact list. " +
          "This cannot be undone."),
        confirm: "Never contact", danger: true,
      });
      if (!ok) return;
    }
    S.busy.set(d.id, which);
    replaceCard(d);
    try {
      const out = await api("POST", "/api/drafts/" + d.id + "/" + which, payload || {});
      toast(out.message || "done");
      S.editing.delete(d.id);
    } catch (err) {
      toast(err.message, true);
    } finally {
      S.busy.delete(d.id);
      await refresh(true);
    }
  }

  function upcomingPanel(rows) {
    return h("div", { class: "panel" },
      h("div", { class: "panel-head" }, "Upcoming touches"),
      rows.length ? h("ul", { class: "list" }, rows.map((r) => h("li", null,
        h("a", { class: "grow", href: "#prospects/" + encodeURIComponent(r.id) }, r.company),
        touches(r.touches),
        h("span", { class: "muted nowrap", title: r.next_touch }, relDay(r.next_touch)))))
        : h("div", { class: "panel-body muted" }, "Nothing scheduled yet."));
  }

  function runsPanel(rows) {
    return h("div", { class: "panel" },
      h("div", { class: "panel-head" }, "Recent runs", h("span", { class: "spacer" }),
        h("a", { href: "#activity", class: "muted" }, "All")),
      rows.length ? h("ul", { class: "list" }, rows.map((r) => h("li", null,
        runBadge(r.status),
        h("span", { class: "grow", title: r.summary || r.error || "" },
          r.summary || r.error || ""),
        h("span", { class: "faint nowrap" }, ago(r.started_at)))))
        : h("div", { class: "panel-body muted" }, "No runs yet."));
  }

  function runBadge(status) {
    const kinds = { success: "ok", escalated: "warn", failed: "bad", running: "info" };
    return badge(status || "?", kinds[status] || "");
  }

  // ---------------------------------------------------------------- prospects

  function renderProspects() {
    const root = $("#view-prospects");
    const counts = {};
    let noAddress = 0;
    for (const p of S.prospects) {
      counts[p.status] = (counts[p.status] || 0) + 1;
      if (needsAddress(p)) noAddress++;
    }
    const filter = (key, label, n) => h("button", {
      class: "filter" + (S.filter === key ? " active" : ""), type: "button",
      onclick: () => { S.filter = key; renderProspects(); } },
      label + (n !== undefined ? " " + n : ""));
    const search = h("input", { type: "search", placeholder: "Search company, email, notes",
      value: S.search, "aria-label": "Search prospects",
      oninput: (e) => { S.search = e.target.value; renderProspectRows(); } });

    root.replaceChildren(
      h("div", { class: "section-head" }, h("h2", null, "Prospects"),
        h("span", { class: "muted" }, S.prospects.length + " on the list"),
        h("span", { class: "spacer" }),
        h("button", { class: "btn", type: "button", onclick: exportCsv }, "Export CSV"),
        h("button", { class: "btn primary", type: "button", onclick: addProspect },
          "Add prospect")),
      h("div", { class: "toolbar" }, search,
        h("div", { class: "filters" },
          filter("all", "All", S.prospects.length),
          filter("noaddress", "Needs address", noAddress),
          S.statuses.filter((s) => counts[s]).map((s) => filter(s, s, counts[s])))),
      h("div", { class: "table-wrap" },
        h("table", null,
          h("thead", null, h("tr", null,
            ["ID", "Company", "Contact", "Address", "Status", "Touches", "Next", "Segment"]
              .map((c) => h("th", null, c)))),
          h("tbody", { id: "prospect-rows" }))));
    renderProspectRows();
    // Keep focus in the search box while typing.
    if (document.activeElement && document.activeElement.type === "search") search.focus();
  }

  function renderProspectRows() {
    const body = $("#prospect-rows");
    if (!body) return;
    const q = S.search.trim().toLowerCase();
    const rows = S.prospects.filter((p) => {
      if (S.filter === "noaddress") {
        if (!needsAddress(p)) return false;
      } else if (S.filter !== "all" && p.status !== S.filter) return false;
      if (!q) return true;
      return [p.id, p.company, p.email, p.contact_name, p.notes, p.segment]
        .some((v) => (v || "").toLowerCase().includes(q));
    });
    if (!rows.length) {
      body.replaceChildren(h("tr", null, h("td", { colspan: "8", class: "muted" },
        S.prospects.length ? "No prospects match." :
          "The list is empty. Add one, or let Discover find companies that sponsor events like yours.")));
      return;
    }
    body.replaceChildren(...rows.map((p) => h("tr", { class: "clickable",
      onclick: () => { location.hash = "#prospects/" + encodeURIComponent(p.id); } },
      h("td", { class: "mono faint" }, p.id),
      h("td", null, h("b", null, p.company),
        p.website ? h("div", { class: "faint" }, hostOf(p.website)) : null),
      h("td", null, p.contact_name || h("span", { class: "faint" }, "—")),
      h("td", null, p.email ? h("span", { class: "ellipsis" }, p.email)
        : badge("needs address", "warn"),
        blockBadge(p.blocked, p.blocked_reason)),
      h("td", null, statusBadge(p.status),
        p.pending ? [" ", badge("card waiting", "violet")] : null,
        p.in_outbox ? [" ", badge("in the outbox", "info")] : null,
        p.reply ? h("div", { class: "faint" }, "reply: " + p.reply) : null),
      h("td", { class: "num" }, touches(p.touches)),
      h("td", { class: "num muted", title: p.next_touch || "" },
        p.next_touch ? relDay(p.next_touch) : "—"),
      h("td", { class: "muted" }, p.segment || ""))));
  }

  function addProspect() {
    const f = {};
    const field = (key, label, attrs = {}, wide = false) => {
      f[key] = attrs.textarea ? h("textarea", { rows: "3" })
        : h("input", Object.assign({ type: "text" }, attrs));
      return h("label", { class: "field" + (wide ? " wide" : "") }, h("span", null, label), f[key]);
    };
    return modal({
      title: "Add a prospect",
      content: h("div", { class: "form-grid" },
        field("company", "Company *", { required: true }, true),
        field("email", "Address", { type: "email", placeholder: "partnerships@..." }),
        field("contact_name", "Contact name"),
        field("website", "Website", { placeholder: "https://" }),
        field("segment", "Segment", { placeholder: "devtools, fintech, ..." }),
        field("notes", "Notes", { textarea: true }, true)),
      confirm: "Add",
      onConfirm: async () => {
        const body = {};
        for (const [k, el] of Object.entries(f)) body[k] = el.value.trim();
        const out = await api("POST", "/api/prospects", body);
        toast(out.message);
        await loadProspects();
        refresh();
      },
    });
  }

  async function exportCsv() {
    try {
      const res = await fetch("/api/export.csv", {
        headers: token ? { "X-Sponsy-Token": token } : {} });
      if (!res.ok) throw new Error("export failed: HTTP " + res.status);
      const url = URL.createObjectURL(await res.blob());
      const a = h("a", { href: url, download: "prospects.csv" });
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (err) {
      toast(err.message, true);
    }
  }

  // ---------------------------------------------------------------- drawer

  async function openProspect(pid) {
    if (S.detailId !== pid) {
      S.detailId = pid;
      S.detail = null;
      renderDrawer();
    }
    try {
      S.detail = await api("GET", "/api/prospects/" + encodeURIComponent(pid));
    } catch (err) {
      toast(err.message, true);
      closeDrawer();
      return;
    }
    if (S.detailId === pid) renderDrawer();
  }

  function closeDrawer(navigate = true) {
    if (!S.detailId) return;
    S.detailId = null;
    S.detail = null;
    $("#drawer-root").replaceChildren();
    document.removeEventListener("keydown", drawerKey);
    if (navigate && location.hash.startsWith("#prospects/")) location.hash = "#prospects";
  }

  function drawerKey(e) {
    if (e.key === "Escape" && !$("#modal-root").childElementCount) closeDrawer();
  }

  function renderDrawer() {
    const root = $("#drawer-root");
    const data = S.detail;
    const p = data ? data.prospect : null;
    document.removeEventListener("keydown", drawerKey);
    document.addEventListener("keydown", drawerKey);
    root.replaceChildren(
      h("div", { class: "scrim", onclick: () => closeDrawer() }),
      h("aside", { class: "drawer", role: "dialog", "aria-label": "Prospect detail" },
        h("div", { class: "drawer-head" },
          h("div", null,
            h("h3", null, p ? p.company : S.detailId),
            p ? h("div", { class: "card-sub" }, h("span", { class: "mono" }, p.id), " ",
              statusBadge(p.status), " ",
              blockBadge(data.blocked, data.blocked_reason)) : null),
          h("span", { class: "spacer" }),
          h("button", { class: "icon-btn", type: "button", "aria-label": "Close",
                        onclick: () => closeDrawer() }, "✕")),
        h("div", { class: "drawer-body" },
          data ? drawerBody(data) : h("div", { class: "muted" }, "Loading..."))));
  }

  function drawerBody(data) {
    const p = data.prospect;
    const closed = ["declined", "cold", "interested"].includes(p.status);
    const kv = (k, v) => [h("dt", null, k), h("dd", null, v === "" || v == null ? "—" : v)];
    return [
      h("div", { class: "panel" }, h("div", { class: "panel-body" },
        h("dl", { class: "kv" },
          kv("Contact", p.contact_name),
          kv("Address", p.email || badge("needs address", "warn")),
          kv("Website", p.website ? extLink(p.website, hostOf(p.website)) : ""),
          kv("Segment", p.segment),
          kv("Touches", h("span", null, touches(p.touches), " " + p.touches + " of 4")),
          kv("First touch", p.first_touch),
          kv("Last touch", p.last_touch),
          kv("Next touch", p.next_touch ? p.next_touch + " (" + relDay(p.next_touch) + ")" : ""),
          kv("Source", p.source),
          kv("Notes", p.notes)))),
      addressPanel(p, data.blocked, data.blocked_reason),
      researchPanel(data.research),
      section("Drafts", data.drafts.length, data.drafts.length
        ? h("div", { class: "history" }, data.drafts.map(historyDraft))
        : h("div", { class: "muted" }, "No drafts yet.")),
      data.replies.length ? section("Replies", data.replies.length,
        h("div", { class: "history" }, data.replies.map(replyItem))) : null,
      section("Sources read", data.sources.length, data.sources.length
        ? h("div", { class: "panel" }, data.sources.map((s) => h("div", { class: "source" },
            extLink(s.source_url, s.title || hostOf(s.source_url)),
            h("span", { class: "faint" }, " · " + shortDate(s.fetched_at)),
            h("div", { class: "excerpt" }, s.excerpt || ""))))
        : h("div", { class: "muted" }, "Nothing fetched yet. The research is what the drafter " +
            "is allowed to say about them; nothing else.")),
      data.actions.length ? section("Audit log", data.actions.length,
        h("div", { class: "panel" }, h("ul", { class: "list" }, data.actions.map((a) =>
          h("li", null, h("span", { class: "mono grow" }, a.kind),
            a.human_verdict ? badge(a.human_verdict, a.human_verdict === "rejected" ? "bad" : "ok")
              : null,
            h("span", { class: "faint nowrap" }, dateTime(a.created_at))))))) : null,
      h("div", { class: "actions" },
        h("span", { class: "spacer" }),
        h("button", { class: "btn danger", type: "button", disabled: p.status === "declined",
                      onclick: () => neverProspect(p) },
          p.status === "declined" ? "Declined permanently" : "Never contact")),
      closed ? h("div", { class: "faint" }, "This prospect is " + p.status +
        "; the database refuses any new draft for it.") : null,
    ];
  }

  function section(title, n, content) {
    return h("div", null,
      h("div", { class: "section-head" }, h("h2", null, title),
        n !== undefined ? h("span", { class: "count" }, n) : null),
      content);
  }

  function addressPanel(p, blocked, reason) {
    const dead = bounced(blocked, reason);
    const email = h("input", { type: "email", value: p.email || "",
                               placeholder: "partnerships@company.com", "aria-label": "Address" });
    const contact = h("input", { type: "text", value: p.contact_name || "",
                                 placeholder: "Contact name", "aria-label": "Contact name" });
    const save = h("button", { class: "btn", type: "submit" }, p.email ? "Update" : "Save address");
    return h("form", { class: "panel", onsubmit: async (e) => {
        e.preventDefault();
        save.disabled = true;
        try {
          const out = await api("POST", "/api/prospects/" + encodeURIComponent(p.id) + "/email",
            { email: email.value.trim(), contact_name: contact.value.trim() });
          toast(out.message);
          await openProspect(p.id);
          if (S.tab === "prospects") loadProspects();
          refresh();
        } catch (err) {
          toast(err.message, true);
          save.disabled = false;
        }
      } },
      h("div", { class: "panel-head" }, dead ? "This address bounced"
        : p.email ? "Change the address" : "Add an address"),
      h("div", { class: "panel-body" },
        dead ? h("div", { class: "issues" }, h("b", null, "Undeliverable. "), reason,
          h("div", null, "It is blocked so nothing more goes to it. Save a different " +
            "address and the ladder picks this prospect up again.")) : null,
        h("div", { class: "form-grid" }, email, contact),
        h("div", { class: "actions" }, h("span", { class: "muted" },
          dead ? "Find the right address with Find addresses on the Agent tab."
            : blocked ? "This address is on the do-not-contact list."
            : "Use a published address. Without one, the daily run skips this prospect."),
          h("span", { class: "spacer" }), save)));
  }

  function researchPanel(r) {
    if (!r) return null;
    const rows = [["Hook", r.hook], ["What they do", r.one_liner],
                  ["Why students", r.student_relevance], ["Recent", r.recent_signal],
                  ["Campus history", r.prior_campus_sponsorship]].filter(([, v]) => v);
    return section("Research", undefined, h("div", { class: "panel" },
      h("div", { class: "panel-body" },
        h("dl", { class: "kv" }, rows.map(([k, v]) => [h("dt", null, k), h("dd", null, v)]),
          h("dt", null, "Confidence"),
          h("dd", null, Number(r.confidence || 0).toFixed(2))))));
  }

  function historyDraft(d) {
    return h("details", { class: "history-item",
                          open: (!d.verdict && !d.sent_at) || !!d.send_state },
      h("summary", null, verdictBadge(d), badge(KIND_LABELS[d.kind] || d.kind),
        h("span", { class: "grow" }, d.subject),
        h("span", { class: "faint nowrap" }, "d:" + d.id + " · " + shortDate(d.created_at))),
      h("div", { class: "inner" },
        h("div", { class: "body" }, d.body),
        h("div", { class: "meta" },
          h("span", null, h("b", null, d.words), " words"),
          h("span", null, h("b", null, d.grounded + "/" + d.claims.length), " claims traced"),
          d.channel ? h("span", null, "via ", h("b", null, d.channel)) : null,
          d.failed.length ? h("span", null, "failed: " + d.failed.join(", ")) : null),
        d.send_note ? h("div", { class: "meta" }, h("span", { class: "note" },
          d.send_state === "queued" || d.send_state === "sending" ? "Outbox: " : "Not sent: ",
          d.send_note)) : null,
        !d.verdict && !d.sent_at ? h("div", { class: "actions" },
          h("a", { class: "btn small", href: "#queue" }, "Decide in the queue")) : null));
  }

  function replyItem(r) {
    const kinds = { interested: "ok", declined: "bad", needs_info: "info", out_of_office: "",
                    bounced: "bad", bounce_soft: "warn" };
    return h("details", { class: "history-item", open: true },
      h("summary", null, badge(r.label || "reply", kinds[r.label] || ""),
        h("span", { class: "grow" }, r.subject || "(no subject)"),
        h("span", { class: "faint nowrap" }, shortDate(r.received_at))),
      h("div", { class: "inner" },
        h("div", { class: "muted" }, "From " + (r.from_addr || "?")),
        h("div", { class: "body" }, r.snippet || ""),
        r.extracted_ask ? h("div", { class: "meta" }, h("span", { class: "note" },
          "They asked: ", h("b", null, r.extracted_ask))) : null,
        r.action ? h("div", { class: "meta" }, h("span", { class: "note" },
          "Done: " + r.action)) : null));
  }

  async function neverProspect(p) {
    await modal({
      title: "Never contact " + p.company + "?",
      content: h("p", null, "Marked declined permanently, any waiting draft is cancelled, " +
        "and " + (p.email || "their address") + " goes on the do-not-contact list. " +
        "This cannot be undone."),
      confirm: "Never contact", danger: true,
      onConfirm: async () => {
        const out = await api("POST", "/api/prospects/" + encodeURIComponent(p.id) + "/never", {});
        toast(out.message);
        await openProspect(p.id);
        refresh(true);
      },
    });
  }

  // ---------------------------------------------------------------- activity

  function renderActivity() {
    const root = $("#view-activity");
    const log = S.log;
    if (!log) {
      root.replaceChildren(h("div", { class: "muted" }, "Loading..."));
      return;
    }
    root.replaceChildren(
      S.outbox ? outboxSection(S.outbox) : null,
      section("Emails sent", log.sent.length, log.sent.length ? h("div", { class: "table-wrap" },
        h("table", null,
          h("thead", null, h("tr", null, ["Sent", "Company", "To", "Touch", "Kind", "Via",
                                           "Subject", "Outcome"].map((c) => h("th", null, c)))),
          h("tbody", null, log.sent.map((r) => h("tr", { class: "clickable",
            onclick: () => { location.hash = "#prospects/" + encodeURIComponent(r.id); } },
            h("td", { class: "num muted" }, dateTime(r.sent_at)),
            h("td", null, h("b", null, r.company)),
            h("td", null, h("span", { class: "ellipsis" }, r.email || "")),
            h("td", { class: "num" }, touches(r.touches)),
            h("td", null, KIND_LABELS[r.kind] || r.kind || ""),
            h("td", { class: "muted" }, r.channel || ""),
            h("td", null, h("span", { class: "ellipsis" }, r.subject || "")),
            h("td", null, r.reply ? badge("reply: " + r.reply, "info") : statusBadge(r.status)))))))
        : h("div", { class: "empty" }, h("div", { class: "big" }, "Nothing sent yet"),
            "Every email you approve shows up here, with what came back.")),
      h("div", { class: "section-head" }),
      section("Runs", log.runs.length, log.runs.length ? h("div", { class: "table-wrap" },
        h("table", null,
          h("thead", null, h("tr", null, ["#", "Started", "Status", "Steps", "Cost", "Summary"]
            .map((c) => h("th", null, c)))),
          h("tbody", null, log.runs.map((r) => h("tr", null,
            h("td", { class: "num faint" }, r.id),
            h("td", { class: "num muted" }, dateTime(r.started_at)),
            h("td", null, runBadge(r.status)),
            h("td", { class: "num" }, r.steps || 0),
            h("td", { class: "num" }, money(r.cost_usd)),
            h("td", null, r.summary || h("span", { class: "muted" }, r.error || "")))))))
        : h("div", { class: "muted" }, "No runs yet.")));
  }

  function outboxSection(ob) {
    const health = h("div", { class: "panel" },
      h("div", { class: "panel-head" }, "Sending health",
        ob.paused ? badge("paused", "bad") : ob.paced ? badge("sending", "ok")
          : badge("not used: " + (S.overview ? S.overview.channels.mail : "manual"), "warn"),
        h("span", { class: "spacer" }),
        ob.paused
          ? h("button", { class: "btn small", type: "button", onclick: resumeSending },
              "Resume")
          : h("button", { class: "btn small", type: "button", onclick: pauseSending },
              "Pause"),
        ob.queued.length ? h("button", { class: "btn small", type: "button",
                                         onclick: sendDueNow }, "Send what is due") : null),
      h("div", { class: "panel-body" },
        h("div", { class: "meta" },
          h("span", null, h("b", null, ob.sent_today + " / " + ob.cap_today), " sent today",
            ob.cap_today < ob.daily_cap ? " (warm-up day " + ob.warmup_day + ")" : ""),
          h("span", null, h("b", null, ob.bounces_7d), " bounce" +
            (ob.bounces_7d === 1 ? "" : "s") + " in the last 7 days of ",
            h("b", null, ob.sends_7d), " sent"),
          h("span", null, "one-click unsubscribe ",
            h("b", null, ob.unsubscribe.one_click ? "on" : "off (set PUBLIC_URL)"))),
        ob.paused ? h("div", { class: "issues" }, h("b", null, "Paused: "), ob.paused.reason)
          : null,
        h("div", { class: "muted small" }, "Policy: " + ob.policy)));

    const queued = ob.queued.length ? h("div", { class: "table-wrap" }, h("table", null,
      h("thead", null, h("tr", null, ["#", "Company", "Kind", "To", "When", "Tries", ""]
        .map((c) => h("th", null, c)))),
      h("tbody", null, ob.queued.map((r) => h("tr", null,
        h("td", { class: "num faint" }, r.id),
        h("td", null, h("a", { href: "#prospects/" + encodeURIComponent(r.prospect_id) },
          r.company || r.prospect_id)),
        h("td", null, KIND_LABELS[r.kind] || r.kind),
        h("td", null, h("span", { class: "ellipsis" }, r.to_addr || "")),
        h("td", null, r.state === "sending" ? badge("sending", "info") : r.wait,
          r.last_error ? h("div", { class: "faint small" }, r.last_error) : null),
        h("td", { class: "num" }, r.attempts || 0),
        h("td", null, r.state === "queued" ? h("button", { class: "btn small danger",
          type: "button", onclick: () => cancelQueued(r) }, "Cancel") : null))))))
      : h("div", { class: "muted" }, "Nothing waiting. Approved emails wait here for " +
          "their send slot.");

    const recent = ob.recent.length ? h("ul", { class: "list" }, ob.recent.map((r) =>
      h("li", null, badge(r.state, r.state === "failed" ? "bad" : "warn"),
        h("a", { class: "grow", href: "#prospects/" + encodeURIComponent(r.prospect_id) },
          r.company || r.prospect_id),
        h("span", { class: "muted" }, r.reason || "")))) : null;

    return h("div", null,
      section("Outbox", ob.queued.length, h("div", null, health, queued)),
      recent ? section("Stopped before sending", ob.recent.length, recent) : null,
      h("div", { class: "section-head" }));
  }

  async function outboxCall(path, body) {
    try {
      const out = await api("POST", path, body || {});
      toast(out.message);
      if (out.outbox) S.outbox = out.outbox;
    } catch (err) {
      toast(err.message, true);
    }
    await refresh(true);
  }

  async function pauseSending() {
    const ok = await modal({
      title: "Pause sending?",
      content: h("p", null, "Approved emails stay in the outbox and nothing leaves " +
        "until you resume. Drafting and approvals carry on."),
      confirm: "Pause",
    });
    if (ok) await outboxCall("/api/outbox/pause", { reason: "paused from the web page" });
  }

  async function sendDueNow() {
    await outboxCall("/api/outbox/send");
  }

  async function cancelQueued(r) {
    const ok = await modal({
      title: "Cancel this email to " + (r.company || r.prospect_id) + "?",
      content: h("p", null, "It will not be sent. The draft stays in the history, and " +
        "the next run drafts this touch again. Use Never to stop for good."),
      confirm: "Cancel the email", cancel: "Keep it", danger: true,
    });
    if (ok) await outboxCall("/api/outbox/" + r.id + "/cancel");
  }

  // ---------------------------------------------------------------- agent

  function renderAgent() {
    const root = $("#view-agent");
    const ov = S.overview;
    if (!ov) return;
    // Don't clobber a half-typed limit while a task is being polled.
    const active = document.activeElement;
    const keepFocus = active && root.contains(active) && active.tagName === "INPUT";
    if (keepFocus && taskRunning()) {
      const slot = $("#task-slot");
      if (slot) slot.replaceChildren(taskPanel());
      return;
    }
    const running = taskRunning();
    const limits = {};
    const tool = (name, title, blurb, def, extra) => {
      limits[name] = def ? h("input", { type: "number", min: "1", max: "50",
        value: store("sponsy.limit." + name) || def, "aria-label": title + " limit",
        onchange: (e) => store("sponsy.limit." + name, e.target.value) }) : null;
      return h("div", { class: "panel tool" },
        h("div", { class: "panel-head" }, title),
        h("div", { class: "panel-body" },
          h("p", null, blurb),
          extra || null,
          h("div", { class: "row" },
            limits[name] ? [h("span", { class: "muted" }, "Limit"), limits[name]] : null,
            h("button", { class: "btn primary", type: "button", disabled: running,
              onclick: () => startTask(name, limits[name] ? limits[name].value : undefined) },
              running && S.task.name === name ? h("span", { class: "spin" }) : null,
              buttonLabel(name)))));
    };
    root.replaceChildren(
      h("div", { class: "section-head" }, h("h2", null, "The agent"),
        h("span", { class: "muted" }, "Everything here researches or drafts. " +
          "Only a Send in the queue writes an email.")),
      h("div", { class: "agent-grid" },
        tool("run", "Daily run",
          "Applies Telegram taps, reads replies, marks anyone past day 21 cold, then " +
          "researches and drafts the prospects that are due. Drafts land in the queue.",
          ov.mode.limit),
        tool("discover", "Discover prospects",
          "Reads the sponsor pages of events like yours and lists the companies on them. " +
          "Nothing is added until you pick.", 15),
        tool("find-contacts", "Find addresses",
          "Looks for a published role mailbox for prospects that have none, and shows " +
          "where it found each one. You choose which to keep.", 10,
          h("p", null, h("b", null, ov.stats.awaiting_contact), " prospect" +
            (ov.stats.awaiting_contact === 1 ? "" : "s") + " waiting on an address.")),
        tool("doctor", "Health check",
          "Checks every key and integration live, like python run.py doctor.", 0)),
      h("div", { id: "task-slot" }, taskPanel()));
  }

  function buttonLabel(name) {
    return { run: "Run now", discover: "Discover", "find-contacts": "Search",
             doctor: "Run checks" }[name];
  }

  async function startTask(name, limit) {
    try {
      const body = limit ? { limit: Number(limit) } : {};
      const out = await api("POST", "/api/tasks/" + name, body);
      setTask(out.task);
      renderChrome();
      renderAgent();
    } catch (err) {
      toast(err.message, true);
    }
  }

  function taskPanel() {
    const t = S.task;
    if (!t) return null;
    const kinds = { running: "info", done: "ok", failed: "bad" };
    const elapsed = t.started_at ? Math.round(((t.finished_at ? parseDate(t.finished_at)
      : new Date()) - parseDate(t.started_at)) / 1000) : 0;
    return h("div", { class: "panel" },
      h("div", { class: "panel-head" },
        t.state === "running" ? h("span", { class: "spin" }) : null,
        TASK_TITLES[t.name] || t.name,
        badge(t.state, kinds[t.state]),
        h("span", { class: "spacer" }),
        h("span", { class: "faint" }, "started " + dateTime(t.started_at) + " · " + elapsed + "s")),
      h("div", { class: "panel-body" },
        t.error ? h("div", { class: "issues" }, t.error) : null,
        t.state === "done" ? taskResult(t) : null,
        t.state === "running" ? h("div", { class: "muted" },
          "Working. This can take a few minutes on a free-tier key.") : null),
      t.log ? h("pre", { class: "task-log" }, t.log) : null);
  }

  function taskResult(t) {
    const r = t.result || {};
    if (t.name === "run") {
      return h("div", null,
        h("p", null, h("b", null, r.summary || "done")),
        h("div", { class: "meta" },
          h("span", null, h("b", null, r.drafts || 0), " new draft(s)"),
          h("span", null, h("b", null, r.steps || 0), " steps"),
          h("span", null, h("b", null, money(r.cost_usd))),
          r.unverified && r.unverified.length ? h("span", null, "unverified: " +
            r.unverified.join(", ")) : null),
        r.reason ? h("div", { class: "issues" }, r.reason) : null,
        r.drafts ? h("div", { class: "actions" },
          h("a", { class: "btn primary", href: "#queue" }, "Review the queue")) : null);
    }
    if (t.name === "discover") return discoverResult(r);
    if (t.name === "find-contacts") return contactsResult(r);
    if (t.name === "doctor") {
      return h("div", null, (r.checks || []).map((c) => h("div", { class: "check" },
        h("div", null, badge(c.status, c.ok ? "ok" : c.optional ? "warn" : "bad")),
        h("div", null, h("b", null, c.name), c.detail ? h("span", { class: "muted" },
          " " + c.detail) : null,
          !c.ok && c.hint ? h("div", { class: "hint" }, c.hint) : null))));
    }
    return null;
  }

  function discoverResult(r) {
    const leads = r.leads || [];
    if (!leads.length) {
      return h("p", { class: "muted" }, "Found " + (r.found || 0) + " compan" +
        (r.found === 1 ? "y" : "ies") + ", none new to your list.");
    }
    const boxes = new Map();
    const list = h("div", { class: "result-list" }, leads.map((lead) => {
      const done = S.added.has(lead.company);
      const box = h("input", { type: "checkbox", checked: !done, disabled: done,
                               "aria-label": "Add " + lead.company });
      if (!done) boxes.set(lead, box);
      return h("label", { class: "result" },
        h("div", { class: "line" }, box,
          h("b", { class: "grow" }, lead.company),
          done ? badge("added", "ok") : null,
          badge("seen on " + lead.strength + " page" + (lead.strength === 1 ? "" : "s"))),
        lead.sources.map((s) => h("div", { class: "src" }, extLink(s))));
    }));
    const addBtn = h("button", { class: "btn primary", type: "button",
      disabled: !boxes.size, onclick: async () => {
        const chosen = [...boxes].filter(([, b]) => b.checked).map(([lead]) => lead);
        if (!chosen.length) return toast("Nothing selected.");
        addBtn.disabled = true;
        try {
          const out = await api("POST", "/api/prospects/bulk", {
            leads: chosen.map((l) => ({ company: l.company, website: l.website,
                                        notes: l.notes })) });
          chosen.forEach((l) => S.added.add(l.company));
          toast(out.message);
          refresh(true);
        } catch (err) {
          toast(err.message, true);
          addBtn.disabled = false;
        }
      } }, "Add selected");
    return h("div", null,
      h("p", { class: "muted" }, (r.found || 0) + " found, " + (r.fresh || 0) +
        " not already on your list. They are added with no address; Find addresses " +
        "comes next."),
      list,
      h("div", { class: "actions" }, h("span", { class: "spacer" }), addBtn));
  }

  function contactsResult(r) {
    const findings = r.findings || [];
    if (!findings.length) return h("p", { class: "muted" }, "Every prospect already has an address.");
    return h("div", { class: "result-list" },
      findings.map((f) => h("div", { class: "result" },
        h("div", { class: "line" }, h("b", { class: "grow" }, f.company),
          h("span", { class: "mono faint" }, f.prospect_id),
          S.used.has(f.prospect_id) ? badge("address saved", "ok") : null),
        f.candidates.length ? f.candidates.map((c) => h("div", { class: "line" },
          h("span", { class: "grow" }, h("span", { class: "mono" }, c.email),
            h("span", { class: "faint" }, " score " + c.score + (c.note ? " · " + c.note : "")),
            h("div", { class: "src" }, "from ", extLink(c.source_url))),
          h("button", { class: "btn small", type: "button",
            disabled: S.used.has(f.prospect_id), onclick: () => useAddress(f, c) }, "Use")))
          : f.form_urls.length
            ? h("div", { class: "src" }, "No public address. Apply through their page: ",
                f.form_urls.map((u) => [extLink(u), " "]))
            : h("div", { class: "src" }, "No public address and no sponsorship page found. " +
                "Try LinkedIn or X for their DevRel lead."))),
      r.remaining ? h("p", { class: "muted" }, r.remaining + " more waiting; search again " +
        "for the next batch.") : null);
  }

  async function useAddress(finding, cand) {
    try {
      const out = await api("POST", "/api/prospects/" + encodeURIComponent(finding.prospect_id) +
        "/email", { email: cand.email, source_url: cand.source_url });
      S.used.add(finding.prospect_id);
      toast(out.message);
      refresh(true);
    } catch (err) {
      toast(err.message, true);
    }
  }

  // ---------------------------------------------------------------- routing

  function route() {
    const [tab, sub] = location.hash.replace(/^#/, "").split("/");
    const next = TABS.includes(tab) ? tab : "queue";
    const changed = next !== S.tab || !S.booted;
    setTab(next);
    if (changed) {
      if (next === "queue") renderQueue(true);
      if (next === "prospects") loadProspects();
      if (next === "activity") loadActivity();
      if (next === "agent") renderAgent();
    }
    if (next === "prospects" && sub) openProspect(decodeURIComponent(sub));
    else closeDrawer(false);
  }

  function initTheme() {
    const saved = store("sponsy.theme");
    if (saved) document.documentElement.dataset.theme = saved;
    $("#theme-toggle").addEventListener("click", () => {
      const root = document.documentElement;
      const dark = root.dataset.theme
        ? root.dataset.theme === "dark"
        : matchMedia("(prefers-color-scheme: dark)").matches;
      root.dataset.theme = dark ? "light" : "dark";
      store("sponsy.theme", root.dataset.theme);
    });
  }

  document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => {
    location.hash = "#" + b.dataset.tab;
  }));
  window.addEventListener("hashchange", route);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);

  initTheme();
  refresh(true).then(() => { route(); S.booted = true; });
})();
