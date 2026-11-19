// waku dashboard — subtab/db helpers, SQL console, Memory/Tools sub-views, VIEWS.
// Split out of app.js: classic <script>, shared global scope (no build
// step, no modules). Load order + rules: static/README.md.

// --- sub-tabs: keep long pages short by splitting them into hash-routed tabs
// (#memory/semantic, #tools/hooks). Each tab is a plain link, so it's
// bookmarkable and the architecture cards can deep-link straight to one.
function subtabBar(view, tabs, active){
  return uiTabs(tabs.map(([key,label,n]) =>
    ({label: esc(label), href: `#${view}/${key}`, on: key===active, count: n})));
}

// A raw SQLite table, scrollable, with the column names AS the sticky headers
// so the schema lines up over its data instead of floating above it.
function dbTable(t){
  if (!t.sample.length) return uiCard(`<span class="empty">empty — no rows yet</span>`);
  const cols = t.columns.map(c => `${esc(c)}${
    t.types&&t.types[c]?`<small>${esc(t.types[c].toLowerCase())}</small>`:""}`);
  const rows = t.sample.map(r => t.columns.map(c =>
    `<span class="dbcell">${esc(String(r[c]??"").slice(0,120))}</span>`));
  return `<div class="scrolly">${uiTable(cols, rows)}</div>
    <div class="meta" style="margin-top:calc(var(--spacing) * 1.5)">showing ${t.sample.length} of ${t.count} row${t.count===1?"":"s"} (newest first)</div>`;
}
// The raw SQLite rows behind a page, closed until asked for. Each page that
// lists a table's rows carries its own raw view, so no page exists only to
// show the same rows a second time (the old Database page). `rawOpen` keeps
// a view open across the 5-second refresh, which rebuilds the page.
const rawOpen = {};
function rawToggle(name, el){ rawOpen[name] = el.open; }
function rawRows(d, name, note = ""){
  const t = ((d.db || {}).tables || []).find(x => x.name === name);
  if (!t) return "";
  return `<details class="raw" ${rawOpen[name] ? "open" : ""} ontoggle="rawToggle('${esc(name)}', this)">
    <summary>Raw rows · <code>${esc(name)}</code> table · ${t.count}</summary>
    <div class="meta" style="margin:var(--space-2) 0">${note || "These are the same rows as above, as SQLite stores them in <code>state.db</code>."}
      Ask your own question in the ${uiLink("SQL console", "#sql")}.</div>${dbTable(t)}</details>`;
}
const DB_DESC = {
  calendar_events: "events the create_event tool wrote (the flagship task)",
  facts: "semantic memory — durable facts",
  episodes: "episodic memory — dated summaries",
  chat_log: "every message, tagged by session_id — consolidation reads from here",
};
// The page that shows each table's rows, with their raw view at its foot.
const DB_HOME = {
  calendar_events: ["Tools ▸ Results", "#tools/results"],
  facts: ["Memory ▸ Semantic", "#memory/semantic"],
  episodes: ["Memory ▸ Episodic", "#memory/episodic"],
  chat_log: ["Gateway", "#gateway"],
};
const QUERY_EXAMPLES = [
  "SELECT role, content FROM chat_log ORDER BY id DESC LIMIT 10",
  "SELECT subject, content FROM facts",
  "SELECT session_id, COUNT(*) FROM chat_log GROUP BY session_id",
];
function dbQueryView(){
  return `<div class="meta" style="margin-bottom:var(--space-2)">A read-only SQL console over <code>state.db</code>
      (the Supabase-editor idea, scoped down). Only <code>SELECT</code> runs — the file is opened read-only,
      so nothing here can change your data.</div>
    <textarea class="sqlbox" id="sqlbox" spellcheck="false" onfocus="markEditing()" oninput="markEditing()">${esc(QUERY_EXAMPLES[0])}</textarea>
    <div style="margin:var(--space-2) 0">${uiButton("Run", {level: "primary", onclick: "runQuery()"})}
      <span class="meta" style="margin-left:var(--space-3)">try: ${QUERY_EXAMPLES.map(q=>`<span class="qexample" onclick="qFill(this.textContent)">${esc(q)}</span>`).join(" &nbsp; ")}</span></div>
    <div id="qout"></div>`;
}

// --- read-only SQL console (item: "a simple query editor like Supabase")
function qFill(sql){ const b=document.getElementById("sqlbox"); if(b){ b.value=sql; runQuery(); } }
async function runQuery(){
  editing = true;   // keep the 5s refresh from wiping the query + results
  const sql = (document.getElementById("sqlbox")||{}).value || "";
  const out = document.getElementById("qout");
  out.innerHTML = `<div class="meta">running…</div>`;
  const r = await postJSON("/api/query", {sql});
  if (r.error){ out.innerHTML = uiCard(`<span class="empty" style="color:var(--bad)">${esc(r.error)}</span>`); return; }
  if (!r.rows.length){ out.innerHTML = uiCard(`<span class="empty">0 rows</span>`); return; }
  out.innerHTML = `<div class="scrolly">${uiTable(r.columns.map(esc),
    r.rows.map(row => row.map(v => `<span class="dbcell">${esc(String(v).slice(0,120))}</span>`)))}</div>
    <div class="meta" style="margin-top:calc(var(--spacing) * 1.5)">${r.rows.length} row(s)</div>`;
}

// --- Memory sub-tabs. Memory is the friendly, per-pillar view of what persists;
// the Data tab shows the SAME rows as raw SQLite tables (see the explainer).
// Which memory is which: this agent's own, on this machine, beside the
// outside one every agent of the person's reads.
function memWhich(d){
  const w = d.waku_memory || {};
  const n = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;
  const local = uiCard(`<span>Waku Agent's own memory, in ${reveal("","the .waku folder")}.
      ${n(d.facts.length, "fact")}, ${n(d.episodes.length, "episode")}, ${n(d.skills.length, "skill")}.
      It works offline, and only this agent reads it.</span>`, {title: "On this machine", cls: "box"});
  const how = w.mode === "plugin" || w.by_plugin
    ? "The Waku plugin sends each turn, and Waku Memory picks what to keep."
    : w.mode === "off" ? "Nothing is sent." : "Waku Agent sends each fact it keeps.";
  const shared = uiCard(`<span>An outside memory that every agent of yours reads: Claude Code, Codex, the phone app.
      ${w.connected
        ? `${uiBadge("connected", "ok")} ${w.account ? esc(w.account) + ". " : ""}${how}`
        : `${uiBadge("not connected", "neutral")} Everything stays on this machine.`}</span>`,
    {title: "Waku Memory, shared",
     action: w.connected ? `<a class="link" href="https://www.waku.one/memories" target="_blank" rel="noopener noreferrer">open waku.one</a>`
                         : uiLink("how to connect", "#connections/waku-memory"), cls: "box"});
  return `<div class="tiles" style="grid-template-columns:repeat(auto-fill,minmax(280px,1fr));margin-bottom:var(--space-3)">${local}${shared}</div>`;
}
function memOverview(d){
  const s = d.stats;
  const pillars = [
    ["Semantic","semantic",d.facts.length+" facts","durable, distilled facts about you and your people"],
    ["Episodic","episodic",d.episodes.length+" episodes","one dated summary per consolidation — stays small on purpose"],
    ["Procedural","skills",d.skills.length+" skills","SKILL.md files loaded only when relevant — how to act"],
  ].map(([t,sub,n,desc]) => uiCard(`<span>${desc}</span>`,
      {title: t, action: uiLink(n, `#memory/${sub}`), cls: "box"})).join("");
  return memWhich(d) + uiNotice("note", `<b>One file underneath.</b>
      <div class="r">What Waku remembers is rows in <code>.waku/state.db</code>: facts and episodes, searchable
      by keyword (FTS5). Each tab here is the readable view, and the raw rows are at the foot of
      ${uiLink("Semantic", "#memory/semantic")} and ${uiLink("Episodic", "#memory/episodic")}.
      <br><br>Some assistants (Hermes) keep memory as a single <code>MEMORY.md</code> file. Waku keeps
      the queryable source in <code>state.db</code> <b>and</b> writes a
      human-readable ${reveal("MEMORY.md","MEMORY.md")} mirror after every turn — so you get both: a real file
      you can open, backed by a sturdy database.</div>`) + `
    <h2>The three pillars</h2>
    <div class="tiles" style="grid-template-columns:repeat(auto-fill,minmax(220px,1fr))">${pillars}</div>
    <h2>Retrieval gate — does this turn even need memory?</h2>${gateSplit(s)}
    <div class="meta" style="margin-top:var(--space-2)">A cheap model decides <b>if</b> a turn needs memory at all, before any lookup —
      this is memory <i>retrieval</i>, the hero decision. (The Observability page charts the same skip/retrieve
      numbers as an operational metric; the decision itself is memory's.)</div>
    <div class="meta" style="margin-top:var(--space-3)">Files: ${reveal("state.db","state.db")} · ${reveal("MEMORY.md","MEMORY.md")} · ${reveal("SOUL.md","SOUL.md")} · ${reveal("skills","skills/")}</div>`;
}
function memSemantic(d){
  let h = `<div class="meta" style="margin-bottom:var(--space-3)">Durable facts distilled from what you tell Waku —
    the smallest, most-reused store. Edit or forget any of them; changes are live next turn.</div>`;
  // editFact (memory.js) finds the row #fact-N, swaps its .fc for a textarea
  // and its last cell for save/cancel, so the row carries the id.
  h += uiTable(["subject", "fact", "source", ""], d.facts.map(f => ({id: `fact-${f.id}`, cells: [
    `<code>${esc(f.subject)}</code>`,
    `<span class="fc">${esc(f.content)}</span>`,
    `<span class="meta">${esc(f.source)}</span>`,
    `<span style="white-space:nowrap">${uiButton("edit", {level: "tertiary", size: "sm", onclick: `editFact(${f.id})`})} · ${uiButton("delete", {level: "tertiary", size: "sm", danger: true, onclick: `delMem('delete_fact',${f.id})`})}</span>`,
  ]})), {empty: "no facts yet"});
  return h + rawRows(d, "facts");
}
function memEpisodic(d){
  const src = d.episodes_source || "sqlite";
  let h = `<div class="meta" style="margin-bottom:var(--space-2)">backend: ${uiBadge(esc(src), "value")}</div>`;
  if (d.episodes_error) h += uiCard(`<span class="empty">Could not read episodes from Notion: ${esc(d.episodes_error)}</span>`);
  h += uiNotice("note", `<b>Why is this small?</b> <span class="r">Episodic memory holds one <i>distilled</i> summary per
    consolidation, not every message. The raw, blow-by-blow conversation lives in the
    ${uiLink("<code>chat_log</code> table", "#gateway")}
    (the big one), under Gateway — episodes are its highlights.</span>`);
  h += uiTable(["date","episode",""], d.episodes.map(e => [
    `<span class="meta">${esc(e.happened_at)}</span>`, esc(e.summary),
    uiButton("delete", {level: "tertiary", size: "sm", danger: true, onclick: `delMem('delete_episode','${e.id}')`}),
  ]), {empty: "no episodes yet"});
  return h + rawRows(d, "episodes", d.episodes_source === "notion"
    ? "Episodes live in Notion now. These rows are the old local copy in <code>state.db</code>." : "");
}
function memSkills(d){
  let h = `<div class="meta" style="margin-bottom:var(--space-3)">Procedural memory — markdown instructions loaded
    only when a message matches. Add your own three ways: teach Waku in chat (it calls
    <code>create_skill</code>), edit a skill below, or drop a <code>SKILL.md</code> into ${reveal("skills","the skills folder")}.</div>`;
  h += d.skills.map((sk,i) => {
    const full = `---
name: ${sk.name}
description: ${sk.description}
---

${sk.body}`;
    return uiCard(`
      <div class="u"><code>${esc(sk.name)}</code> <span class="meta" style="font-weight:400">· ${esc(sk.description)}</span>
        <span style="margin-left:calc(var(--spacing) * 1.5)">${uiBadge(sk.editable?"home":"built-in")}</span></div>
      <textarea class="editor" id="sk-${i}" style="min-height:150px;margin-top:var(--space-2)" data-path="${esc(sk.path)}"
        oninput="dirty('sksave-${i}')" onfocus="markEditing()">${esc(full)}</textarea>
      <div style="margin-top:var(--space-2)">${uiButton("Save SKILL.md", {level: "primary", onclick: `saveSkill(${i})`, attrs: `id="sksave-${i}" disabled`})}
        <span class="meta" id="skmsg-${i}" style="margin-left:var(--space-2)">${esc(sk.rel)}</span></div>`);
  }).join("") || uiCard(`<span class="empty">no skills loaded</span>`);
  return h;
}
function memSoul(d){
  return `<div class="meta" style="margin-bottom:var(--space-3)">SOUL.md is Waku's persona — the system prompt it
    loads every turn. Editing it changes who your Waku is. Changes are live next turn.</div>
    ${uiCard(`<textarea id="soul" class="editor" style="min-height:260px"
      oninput="dirty('soul-save')" onfocus="markEditing()">${esc(d.soul||"")}</textarea>
    <div style="margin-top:var(--space-2)">${uiButton("Save SOUL.md", {level: "primary", onclick: "saveSoul()", attrs: 'id="soul-save" disabled'})}
      <span class="meta" id="soul-msg" style="margin-left:var(--space-2)"></span></div>`)}
    <div class="meta" style="margin-top:var(--space-2)">${reveal("SOUL.md","open SOUL.md in your editor")}</div>`;
}
function memConsolidation(d){
  const distilled = d.facts.filter(f => f.source==="consolidation");
  let h = uiCard(`<b>How it works.</b> <span class="r prose">Every ${d.consolidate_every} exchanges,
    a cheap model reads the unconsolidated ${"<code>chat_log</code>"} and distills it into durable
    <b>facts</b> (semantic) plus one <b>episode</b> (episodic). Batching keeps it cheap and gives the
    summarizer enough context to pick what's worth keeping.</span>`);
  h += `<div style="margin-top:var(--space-3)">${uiStatBand([
    {label:"messages queued", value:d.chat_pending},
    {label:"trigger threshold", value:d.consolidate_every*2},
    {label:"facts from consolidation", value:distilled.length},
    {label:"episodes total", value:d.episodes.length},
  ])}</div>`;
  h += `<h2>Facts it distilled</h2>`;
  h += table(["subject","fact","when"], distilled.map(f =>
    `<tr><td><code>${esc(f.subject)}</code></td><td>${esc(f.content)}</td><td class="meta">${esc((f.created_at||"").slice(0,10))}</td></tr>`));
  h += `<div class="meta" style="margin-top:var(--space-2)">This is a memory operation, shown here. Each run is also
    ${uiLink("traced", "#observability/turns")} (Observability) and can be scored by the judge evals.</div>`;
  return h;
}

// Tools ▸ Results: the artifacts tool calls produced (kept distinct from the
// tools themselves — the old tab conflated capability with output).
function toolsResults(d){
  let h = `<div class="meta" style="margin-bottom:var(--space-2)">What tool calls actually wrote. These are results, not the tools.</div>`;
  h += `<h2>Calendar events <span class="meta" style="font-weight:400">· from create_event</span></h2>`;
  h += table(["event","start","end","with"], d.calendar.map(e =>
    `<tr><td>${esc(e.title)}</td><td class="meta">${esc(e.start)}</td><td class="meta">${esc(e.end)}</td><td>${esc(e.attendees)}</td></tr>`));
  h += `<div class="meta" style="margin-bottom:var(--space-4)">also written to <code>calendar.ics</code> — ${reveal("calendar.ics","reveal calendar.ics in Finder")} (double-click to import into Calendar.app)</div>`;
  h += `<h2>Outbox — drafted messages <span style="font-weight:400;text-transform:none;letter-spacing:0">· ${reveal("outbox","open the outbox folder")}</span></h2>`;
  h += d.outbox.length ? d.outbox.map(o => uiCard(`<span class="u">${esc(o.name)}</span><div class="r">${esc(o.text)}</div>`)).join("")
                       : uiCard(`<span class="empty">no drafted messages</span>`);
  return h + rawRows(d, "calendar_events");
}
// What each of the four hook moments is, in the words a person would use.
const HOOK_WHEN = {SessionStart: "Session start", UserPromptSubmit: "Each message",
  PreToolUse: "Before a tool runs", Stop: "After each reply"};

function toolsHooks(t){
  const k = t.hooks || {rows: [], skipped: []};
  let h = uiNotice("note", `<b>Hooks — calls Waku makes by itself.</b>
    <div class="r">A tool is called when the model decides to. A hook is called at a fixed moment of every turn,
    whatever the model decides. Plugins declare them. This tab lists the hooks that exist;
    ${uiLink("Observability ▸ Turns", "#observability/turns")} shows each time one ran.</div>`);
  h += uiTable(["When", "Plugin", "Calls", "Last run"], (k.rows || []).map(r => {
    const when = `${esc(HOOK_WHEN[r.event] || r.event)}<div class="meta"><code>${esc(r.event)}</code></div>`;
    if (!r.plugin) return [when, `<span class="meta">no plugin uses this yet</span>`, "", ""];
    const last = r.last
      ? (r.last.ok ? uiBadge("ok", "ok") : uiBadge("failed", "bad"))
        + ` <span class="meta">${Math.round(r.last.duration_ms || 0)} ms · ${esc(String(r.last.ts || "").replace("T", " ").slice(0, 16))}</span>`
        + (r.last.error ? `<div class="meta">${esc(r.last.error)}</div>` : "")
      : `<span class="meta">not yet</span>`;
    return [when, esc(r.plugin),
      `<code>${esc(r.tool)}</code><div class="meta">on ${esc(r.server || "?")}${r.kind && r.kind !== "mcp_tool" ? ` · type ${esc(r.kind)} is not run` : ""}</div>`,
      last];
  }));
  if ((k.skipped || []).length)
    h += `<div class="meta" style="margin-top:var(--space-2)">Loaded without their hooks, because they were written for
      Claude Code: ${k.skipped.map(n => `<code>${esc(n)}</code>`).join(" ")}.</div>`;
  return h;
}

function connectionField(key, field, prefix="connection"){
  const id = `${prefix}-${key}-${field.name}`;
  const label = `${esc(field.label)}${field.required?" *":""}`;
  const help = field.help ? `<div class="conn-field-help">${esc(field.help)}</div>` : "";
  if (field.kind === "bool") return `<div class="conn-field">
    <label class="conn-check" for="${id}"><input id="${id}" data-field="${esc(field.name)}" type="checkbox" ${field.value?"checked":""}>
      <span>${label}</span></label>${help}</div>`;
  if (field.kind === "choice") return `<div class="conn-field"><label class="fld" for="${id}"><span>${label}</span>
    <select id="${id}" data-field="${esc(field.name)}">${field.options.map(o=>`<option value="${esc(o)}" ${o===field.value?"selected":""}>${esc(o)}</option>`).join("")}</select>
    </label>${help}</div>`;
  const configured = field.secret && field.configured
    ? ` ${uiBadge("set ····"+esc(field.last4), "ok")}` : "";
  const clear = field.secret && field.configured
    ? `<label class="conn-clear"><input type="checkbox" data-clear="${esc(field.name)}"> Clear saved value</label>` : "";
  return `<div class="conn-field"><label class="fld" for="${id}"><span>${label}${configured}</span>
    <input id="${id}" data-field="${esc(field.name)}" type="${field.secret?"password":"text"}"
      value="${field.secret?"":esc(field.value)}" placeholder="${field.secret?(field.configured?"Blank keeps the saved value":"Not configured"):""}">
    </label>${clear}${help}</div>`;
}
async function saveConnection(key, force){
  const modal = document.querySelector(`dialog [data-connection="${key}"]`), values = {}, clear = [];
  if (!modal) return;
  modal.querySelectorAll("[data-field]").forEach(el => values[el.dataset.field] = el.type === "checkbox" ? (el.checked ? "1" : "") : el.value);
  modal.querySelectorAll("[data-clear]").forEach(el => { if (el.checked) clear.push(el.dataset.clear); });
  const msg = document.getElementById(`connection-msg-${key}`);
  msg.textContent = force ? "saving without a successful test…" : "saving…";
  const r = await postJSON("/api/connections", {key, values, clear, force:!!force});
  if (!r.ok && r.can_force) {
    msg.innerHTML = `${esc(r.error)} ${uiButton("Save anyway", {level: "secondary", cls: "conn-force", onclick: `saveConnection('${esc(key)}',true)`})}`;
  } else if (!r.ok) {
    msg.textContent = r.error || "failed";
  } else {
    closeConnectionModal();
    await refresh();
  }
}
async function testConnection(key){
  const msg = document.getElementById(`connection-msg-${key}`);
  if (msg) msg.textContent = "testing…";
  const r = await postJSON("/api/connections/test", {key});
  if (!r.status) {
    if (msg) msg.textContent = r.error || "failed";
    return;
  }
  const display = connectionStatusDisplay(r.status);
  const status = document.getElementById("connection-modal-status");
  if (status) {
    status.className = `connstatus ${display.className}`;
    status.innerHTML = `<span class="conndot"></span>${esc(display.label)}`;
  }
  const detail = document.getElementById("connection-modal-status-detail");
  if (detail) detail.textContent = r.status.message || "";
  const checked = document.getElementById("connection-modal-checked");
  if (checked) checked.textContent = r.status.checked_at ? `Last checked ${r.status.checked_at}` : "";
  if (msg) msg.textContent = r.status.message || display.label;
  await refresh();
}
async function saveProvider(provider){
  const info = (D.providers || []).find(x => x.key === provider);
  const field = info && info.fields[0] && document.getElementById(`provider-${provider}-${info.fields[0].name}`);
  const payload = {provider};
  if (field && field.value) payload.key = field.value;
  // Models are global fields for the *current* provider. Switching cards must
  // omit them so apply_provider selects the new provider's own default.
  if (provider === stProvider()) {
    const model = document.getElementById("provider-model"), small = document.getElementById("provider-small-model"), base = document.getElementById("provider-base-url"), custom = document.getElementById("provider-custom-key");
    if (model) payload.model = model.value;
    if (small) payload.small_model = small.value;
    if (base) payload.base_url = base.value;
    if (custom && custom.value) payload.custom_key = custom.value;
    if (document.getElementById("provider-clear-custom-key")?.checked) payload.custom_key = "";
  }
  const r = await postJSON("/api/providers", payload);
  if (!r.ok) alert(r.error || "Provider update failed"); else refresh();
}
function stProvider(){ return (D.settings || {}).provider || "anthropic"; }

// The Connections page (spec 030). `connections_page` in /api/data holds
// every card, in its group, with its state and its one button
// (waku/ops/connections_page.py decides all of it). This file draws them.

// A card with no drawing of its own shows the first letter of its name.
function connectionLogo(card){
  return card.logo
    ? `<img class="provlogo connlogo" src="/static/${esc(card.logo)}" alt="">`
    : `<span class="provlogo connmark" aria-hidden="true">${esc(card.name.slice(0, 1).toUpperCase())}</span>`;
}

// The one button of a card. `kind` says what it does; an address is opened
// only when it is http(s).
function connectionAction(card){
  const a = card.action;
  if (!a) return "";
  const t = esc(a.target);
  if (a.kind === "href")
    return /^https?:\/\//.test(a.target)
      ? `<a class="link" href="${t}" target="_blank" rel="noopener noreferrer">${esc(a.label)}</a>` : "";
  const onclick = {dialog: `openConnectionModal('${t}')`, chat: `connectInChat('${t}')`,
                   add: `addConnector('${t}')`, manage: `openConnectorManage('${t}')`}[a.kind];
  return onclick ? uiButton(esc(a.label), {level: "secondary", onclick}) : "";
}

function connectionCard(card){
  const action = connectionAction(card);
  return `<div class="conncell" id="conn-${esc(card.id)}">${uiCard(`
    ${connectionLogo(card)}
    <div class="connstatus ${esc(card.state)}"><span class="conndot"></span>${esc(card.label)}</div>
    ${card.why ? `<div class="connwhy">${esc(card.why)}</div>` : ""}
    <div class="conndesc">${esc(card.what)}</div>
    ${action ? `<div class="provactions connactions">${action}</div>` : ""}`,
    {title: esc(card.name), cls: "provcard conncard"})}</div>`;
}

function connectInChat(key){
  const input = document.getElementById("dmsg");
  if (!input) return;
  document.body.classList.remove("dock-closed");
  input.value = `/connect ${key}`;
  sendChat(input);
}

function connectionsGrid(page){
  const grid = cards => `<div class="provgrid conngrid">${cards.map(connectionCard).join("")}</div>`;
  return page.groups.map(group => `<section class="connsection" id="conn-group-${esc(group.id)}">
    <h2>${esc(group.title)}</h2>
    ${group.id === "apps" ? connectorSignIn(page.sign_in) : ""}
    ${grid(group.cards)}
    ${group.id === "apps" && page.add_by_address ? connectorAddSection() : ""}
    ${group.more ? `<h3 class="connsub">${esc(group.more.title)}</h3>${grid(group.more.cards)}` : ""}
  </section>`).join("");
}

// A link such as #connections/app-notion opens the page at that card and
// marks it, so one click from Tools reaches the card that controls a tool.
function focusConnection(id, scroll){
  const card = document.getElementById("conn-" + id);
  const el = card || document.getElementById("conn-group-" + id);
  if (!el) return;
  if (card) card.classList.add("conn-target");
  if (scroll) el.scrollIntoView({block: card ? "center" : "start"});
}

// The connection dialog. openDialog (ui.js) owns the <dialog>: Escape and a
// click on the scrim close it natively, and onClose tidies up however it closed.
function openConnectionModal(key){
  const item = ((D && D.connections) || []).find(connection => connection.key === key);
  if (!item) return;
  markEditing();
  const display = connectionStatusDisplay(item.status);
  const status = item.status || {};
  const fields = item.fields.map(field => connectionField(item.key, field)).join("");
  const setup = (item.install_command || item.setup_url) ? uiNotice("note", `
        ${item.install_command?`<code>${esc(item.install_command)}</code>`:""}
        ${item.setup_url?`<a href="${esc(item.setup_url)}" target="_blank" rel="noopener noreferrer">Setup guide ↗</a>`:""}`) : "";
  const d = openDialog(`<div data-connection="${esc(item.key)}">
      <header class="connmodal-head">
        <img class="provlogo connlogo" src="/static/logos/connections/${esc(item.key)}.svg" alt="">
        <div class="connmodal-title">
          <h3 id="connection-modal-title">${esc(item.name)}</h3>
          <div class="connstatus ${display.className}" id="connection-modal-status"><span class="conndot"></span>${esc(display.label)}</div>
        </div>
        ${uiButton("Close", {level: "tertiary", size: "sm", cls: "connmodal-close", onclick: "closeConnectionModal()", attrs: 'aria-label="Close"'})}
      </header>
      <p class="conndesc connmodal-desc">${esc(item.what)}</p>
      <div class="connmodal-meta">
        <span id="connection-modal-status-detail">${esc(status.message || "")}</span>
        <span id="connection-modal-checked">${status.checked_at?`Last checked ${esc(status.checked_at)}`:""}</span>
      </div>
      ${setup}
      <div class="connection-fields">${fields}</div>
      <div class="dialog-foot">
        <span class="connmodal-message" id="connection-msg-${esc(item.key)}" aria-live="polite"></span>
        ${uiButton("Save", {level: "primary", onclick: `saveConnection('${esc(item.key)}')`})}
        ${uiButton("Test connection", {level: "secondary", onclick: `testConnection('${esc(item.key)}')`})}
      </div>
    </div>`, {label: item.name, onClose: () => {
      editing = false;
      if (activeView === "connections") render();
    }});
  // openDialog focuses the first control, which is Close; start on a field instead.
  d.querySelector(".connection-fields input, .connection-fields select")?.focus();
}

function closeConnectionModal(){
  closeDialog();
}

// Escape is native to <dialog> now; kept for anything that still calls it.
function connectionModalKeydown(event){
  if (event.key === "Escape") closeConnectionModal();
}

const VIEWS = {
  models(d){
    // Provider card grid (logo / status dot / edit / enable-disable). Editing
    // happens in a modal opened from a card; both live in js/models.js.
    return modelsGrid(d);
  },
  connections(d){
    const page = d.connections_page;
    return page && page.groups.length ? connectionsGrid(page)
      : uiCard(`<span class="empty">Nothing to connect yet.</span>`);
  },
  // Gateway: ONE unified conversation across every channel (dashboard, telegram,
  // voice, cli) — the same loop + memory answer all of them. Each message is
  // tagged with where it came in, Hermes-style. You type in the dock on the right.
  // Gateway = an INBOX of conversations (like Slack/Intercom): one row per
  // conversation, tagged with its channel(s). Click one to open it in the chat
  // dock (the active thread). No longer a flat stream that duplicates the dock.
  gateway(d){
    const sessions = d.sessions || [];
    let h = `<div class="meta" style="margin-bottom:var(--space-3)">Every conversation across every channel —
      web, phone (Telegram), voice, terminal — answered by the same brain. Click one to open it in the
      chat dock &rarr;. This is the inbox; the dock is the open thread.</div>`;
    if (!sessions.length)
      return h + uiCard(`<span class="empty">no conversations yet — say something in the chat dock &rarr;</span>`);
    h += sessions.map(s => uiRow(gwTags(s),
      `${esc(s.title||s.id)} <span class="meta" style="font-weight:400;white-space:nowrap">· ${sessionMeta(s)}</span>`,
      esc(s.last||""),
      {onclick: `openConversation('${esc(s.id)}')`, cls: s.id === SESSION ? "on" : ""})).join("");
    return h + rawRows(d, "chat_log",
      "Every message of every conversation above, one row each, as SQLite stores them in <code>state.db</code>.");
  },
  overview(d){
    const s = d.stats;
    const u = d.usage || {total_cost:0};
    return `${memoryChoiceCard(d)}${waitingCard(d)}${uiStatBand([
        {label:"spent", value:money(u.total_cost), sub:"all-time", tone:"ok"},
        {label:"avg turn", value:secs(s.latency_avg)},
        {label:"turns", value:s.turns}, {label:"tool calls", value:s.tool_calls},
        {label:"facts", value:d.facts.length}, {label:"events", value:d.calendar.length},
      ])}
    <h2>Retrieval gate — the hero decision</h2>${gateSplit(s)}
    <h2 style="margin-top:var(--space-6)">Architecture — click any box <span class="arch-status"></span></h2>
    ${archSVG(d)}
    <h2>Graph workflows — when a turn needs shape</h2>
    ${graphPanel(d)}
    <h2>Latest turn</h2>${d.turns.length?turnCard(d.turns[0]):uiCard('<span class="empty">no turns yet — talk to Waku first</span>')}`;
  },
  loop(d){
    return d.turns.length ? d.turns.map(turnCard).join("") : uiCard(`<span class="empty">no turns yet</span>`);
  },
  // Graph workflows: the loop's sibling. The chart is rendered from the
  // engine's own describe() (served in d.graph.workflows) so it can never
  // show a shape the engine doesn't run. Nothing here is a mode switch —
  // the harness routes every message itself; this tab just tells the story.
  graph(d){
    const g = d.graph || {enabled:false, workflows:[], stats:{quick:0, full:0}};
    let h = `<div class="meta" style="margin-bottom:var(--space-3)">The loop is one agent turn: the model picks tools until
      it stops. Some work has <b>shape</b> — steps that can run at the same time, and explicit "if this, go
      there" routing. A <b>graph workflow</b> makes that shape first-class: nodes (each does one job) connected
      by edges (what happens next). The loop did not change one line — the <code>full_agent</code> node below
      IS the same loop, running as one step. The harness routes every message itself — and workflows you
      can also call BY NAME from the chat box: type <code>/graphs</code> to see them.</div>`;
    if (!g.enabled)
      h += uiCard(`<b>Off</b> — every turn currently runs the classic loop.
        <div class="meta" style="margin-top:calc(var(--spacing) * 1.5)">Switch on <b>graph workflows</b> in
        ${uiLink("Behaviour", "#settings")}, or set
        <code>WAKU_GRAPH_WORKFLOWS=1</code> in <code>.env</code>. Any failure anywhere fails open to the
        plain loop — this can never lose a reply, only save time and tokens.</div>`);
    // The two workflows are two different JOBS with different triggers, which is
    // the thing the page has to make obvious — otherwise two stacked charts read
    // like two options you pick between.
    const NOTE = {
      triage: `<b>Runs itself, on every message.</b> Gated by the graph-workflows flag.
        Solid arrows = always, dashed = the router's choice. <code>full_agent</code> is the
        ordinary loop running as one node — a graph does not replace the loop, it arranges calls to it.`,
      gather: `<b>Runs when you start it</b> — <code>make gather</code> or the button below — and
        ignores the flag entirely. The four scans have no dependencies on each other, so the engine
        runs them in ONE WAVE: together, not in turn. It proposes and never acts; the digest lands
        in the outbox for you to read.`,
    };
    (g.workflows || []).forEach(w => {
      if (!w) return;
      h += `<h2>${esc(w.name)} — live topology <span class="arch-status"></span></h2>`;
      const tot = g.stats.quick + g.stats.full;
      const extra = w.name === "triage" && tot
        ? ` · ${g.stats.quick} quick / ${g.stats.full} full so far` : "";
      h += uiCard(`${graphSVG(w)}
        <div class="meta" style="margin-top:var(--space-2)">${NOTE[w.name] || ""}${extra} ·
        drawn from the engine's own <code>describe()</code>, so this picture cannot drift from the code</div>`);
      if (w.name === "gather") h += graphRunPanel();
    });
    const gturns = (d.turns||[]).filter(t => t.graph && t.graph.route);
    h += `<h2>Graph turns</h2>`;
    h += gturns.length
      ? gturns.slice(0,20).map(t => uiCard(`
          <div class="u">${esc(t.user_message)}</div>
          <div class="meta" style="margin-top:var(--spacing)">${uiBadge(`graph · ${esc(t.graph.route)}`, t.graph.route==="quick" ? "neutral" : "value")}
            <span class="meta" style="margin:0">${esc(t.graph.reason||"")}</span></div>
          <div class="r">${renderMarkdown(t.reply||"")}</div>`)).join("")
      : uiCard(`<span class="empty">no graph turns yet — ${g.enabled
          ? 'say "thanks!" in the chat and watch it take the quick door'
          : "switch the flag on first"}</span>`);
    return h;
  },
  memory(d, sub){
    sub = sub || "overview";
    const tabs = [["overview","Overview"],["semantic","Semantic",d.facts.length],
      ["episodic","Episodic",d.episodes.length],["skills","Skills",d.skills.length],
      ["soul","SOUL"],["consolidation","Consolidation",d.chat_pending]];
    let h = subtabBar("memory", tabs, sub);
    if (sub==="semantic") return h + memSemantic(d);
    if (sub==="episodic") return h + memEpisodic(d);
    if (sub==="skills") return h + memSkills(d);
    if (sub==="soul") return h + memSoul(d);
    if (sub==="consolidation") return h + memConsolidation(d);
    return h + memOverview(d);
  },
  settings(d){
    const st = d.settings || {providers:[]};
    return `<h2>Experimental tools</h2>${uiCard(`
      <div class="meta" style="margin-bottom:var(--space-2)">Opt in to local coding delegation for chat.</div>
      <label class="fld">Sub-agent delegation<select id="set-experimental" onfocus="markEditing()">
        <option value="" ${!st.experimental?"selected":""}>off</option>
        <option value="1" ${st.experimental?"selected":""}>on</option>
      </select></label>
      ${uiButton("Save", {level: "primary", onclick: "saveSettings()"})}<span class="meta" id="set-msg"></span>`)}
    <h2>Graph workflows</h2>${uiCard(`
      <div class="meta" style="margin-bottom:var(--space-2)">Off by default. When on, <b>every</b> message is triaged
        through a graph first: a small model classifies it while today's calendar loads in parallel — trivial
        messages get a fast small-model reply, real tasks run the exact same loop as a node. This flag governs
        the AUTOMATIC door only — workflows you call by name (<code>/gather</code>) run either way. Any
        failure fails open to the plain loop. Watch it live on the
        ${uiLink("Graph", "#graph")} tab.</div>
      <label class="fld">Triage-first turns
        <select id="set-graph-workflows" onfocus="markEditing()">
          <option value="" ${!st.graph_workflows?"selected":""}>off — every turn runs the classic loop (default)</option>
          <option value="1" ${st.graph_workflows?"selected":""}>on — triage graph routes each message</option>
        </select></label>
      <div style="margin-top:var(--space-3)">${uiButton("Save &amp; switch", {level: "primary", onclick: "saveSettings()"})}
        <span class="meta" style="margin-left:var(--space-2)">rebuilds the agent in-process — no restart</span></div>
    `)}`;
  },
  tools(d, sub){
    const t = d.tools || {catalog:[], mcp:{configured:false,servers:[],live:false}, apple_on:false};
    sub = sub || "available";
    const tabs = [["available","Available",t.catalog.length],["results","Results"],
      ["hooks","Hooks",(t.hooks && t.hooks.count)||null]];
    let h = subtabBar("tools", tabs, sub);
    if (sub === "results") return h + toolsResults(d);
    if (sub === "hooks") return h + toolsHooks(t);
    // Available: what the agent can do right now, grouped by where each
    // ability comes from (spec 030). The page changes nothing: a connection
    // is managed on its card in Connections, one click from its group here.
    h += `<div class="meta" style="margin-bottom:var(--space-3)">What Waku can do right now. Each tool says where
      it comes from: Waku itself, or a connection. This page changes nothing: a connection is added and
      removed in ${uiLink("Connections", "#connections")}.</div>`;
    const toolCard = (c, label) => uiCard(`
        <div class="tn"><code>${esc(c.name)}</code> ${uiBadge(esc(label), c.from ? "ok" : "neutral")}</div>
        <div class="td">${esc(c.description)}</div>`, {cls: "toolcard", size: "sm"});
    const builtIn = t.catalog.filter(c => !c.from);
    if (builtIn.length)
      h += `<h2>Built in</h2>` + builtIn.map(c => toolCard(c, "built in")).join("");
    // One group for each connection, in the order its first tool appears.
    const groups = [];
    t.catalog.filter(c => c.from).forEach(c => {
      let g = groups.find(x => x.name === c.from.name);
      if (!g) groups.push(g = {name: c.from.name, id: c.from.id, items: []});
      g.items.push(c);
    });
    groups.forEach(g => {
      h += `<h2>${esc(g.name)}</h2>` + g.items.map(c => toolCard(c, g.name)).join("");
      if (g.id) h += `<div class="meta toolmanage">${uiLink("Manage in Connections", "#connections/" + encodeURIComponent(g.id))}</div>`;
    });
    // Roadmap: whiteboard boxes not wired in yet — set expectations, don't over-promise.
    if ((t.planned||[]).length){
      h += `<h2>Coming soon <span class="meta" style="font-weight:400">· on the architecture chart, not wired in yet (opt in with <code>WAKU_EXPERIMENTAL=1</code>)</span></h2>`;
      h += t.planned.map(p => uiCard(`
        <div class="tn"><code>${esc(p.name)}</code> ${uiBadge(`soon · ${esc(p.box)}`)}</div>
        <div class="td">${esc(p.description)}</div>`, {cls: "toolcard toolcard-soon", size: "sm"})).join("");
    }
    // The last line of the page: the starter connections nobody connected,
    // so a person who looks for a missing ability finds the way.
    const missing = t.not_connected || [];
    if (missing.length)
      h += `<div class="meta toolmissing">Not connected: ${missing.map(m => esc(m.name)).join(", ")}.
        ${uiLink("Connect", "#connections/" + encodeURIComponent(missing[0].id))}</div>`;
    return h;
  },
  sql(d){
    // The one thing the old Database page had that no other page does: a
    // read-only console over state.db. The tables themselves are shown where
    // they belong, each with its raw rows.
    const db = d.db || {tables:[], all_tables:[], fts:[], size:0, path:""};
    const kb = (db.size/1024).toFixed(1);
    let h = uiCard(`
      <div class="u" style="font-family:var(--face-mono);font-size:var(--text-xs);word-break:break-all">${esc(db.path)}</div>
      <div class="meta">${kb} KB on disk · SQLite + FTS5 · open it yourself: <code>sqlite3 .waku/state.db</code></div>
      <div class="meta" style="margin-top:var(--space-2)">${reveal("state.db","reveal state.db in Finder")} &nbsp;·&nbsp; ${reveal("","open the .waku folder")}</div>`);
    h += dbQueryView();
    h += `<h2>Tables</h2>` + uiTable(["table", "rows", "what it holds", "shown on"], (db.tables || []).map(t => {
      const home = DB_HOME[t.name];
      return [`<code>${esc(t.name)}</code>`, `<span class="meta">${t.count}</span>`,
        `<span class="meta">${DB_DESC[t.name] || ""}</span>`, home ? uiLink(home[0], home[1]) : ""];
    }));
    h += `<h2>FTS5 — the keyword index</h2>` + uiCard(`The <code>*_fts</code> virtual tables (and their
      <code>*_fts_data</code>/<code>*_fts_idx</code> shadows) make memory searchable by keyword — no embeddings,
      no vector DB. This is the "keyword top-k" the retrieval gate queries.
      <div class="meta" style="margin-top:var(--space-2)">all ${db.all_tables.length} tables: ${db.all_tables.map(t=>`<code>${esc(t)}</code>`).join(" ")}</div>`);
    return h;
  },
};
