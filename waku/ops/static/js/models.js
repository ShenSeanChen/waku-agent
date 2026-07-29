// waku dashboard — settings write (applyModel), model picker/catalog/pins.
// Split out of app.js: classic <script>, shared global scope (no build
// step, no modules). Load order + rules: static/README.md.

// The ONE writer to /api/settings (settings form, catalog "use", chat pill all
// funnel through here) so the after-switch reset can't drift. On success it
// releases the edit lock — else the render guard keeps showing the OLD state
// (live bug: "Current:" card stayed on kimi after switching) — clears the stale
// catalog, and re-fetches. Returns the response so callers show their own status.
async function applyModel({provider, model, small_model, episodic_store, experimental, graph_workflows, keys = {}}){
  const r = await postJSON("/api/settings", {provider, model, small_model, episodic_store, experimental, graph_workflows, keys});
  if (!r.error){ editing = false; modelCatalog = null; await refresh(); }
  return r;
}
async function saveSettings(){
  const provider = document.getElementById("set-provider").value;
  const model = document.getElementById("set-model").value.trim();
  const small_model = (document.getElementById("set-small-model")?.value || "").trim();
  const episodic_store = document.getElementById("set-episodic-store")?.value;
  const experimental = document.getElementById("set-experimental")?.value;
  const graph_workflows = document.getElementById("set-graph-workflows")?.value;
  const keys = {};
  document.querySelectorAll("[data-key]").forEach(i => { if(i.value.trim()) keys[i.dataset.key] = i.value.trim(); });
  document.getElementById("set-msg").textContent = "switching…";
  const r = await applyModel({provider, model, small_model, episodic_store, experimental, graph_workflows, keys});
  document.getElementById("set-msg").textContent = r.error ? ("Error: "+r.error) : "Switched to "+r.provider+" — live now.";
}
function markEditing(){ editing = true; }

// Model picker: fill the settings datalist from /api/models (the active
// endpoint's live catalog; on OpenRouter each entry says free / tool support).
// Waku's loop needs tool calling, so tool-less models are labelled as such.
let modelCatalog = null;
async function loadModelList(){
  const dl = document.getElementById("model-list");
  if (!dl) return;
  if (modelCatalog === null){
    try { modelCatalog = await (await fetch("/api/models")).json(); }
    catch(e){ modelCatalog = {models:[], listed:false}; }
  }
  const ms = modelCatalog.models || [];
  dl.innerHTML = ms.map(m => {
    const price = m.free ? "free" : (m.price_out != null ? `$${m.price_in}/$${m.price_out} per M` : "");
    const tags = [price, m.tools === false ? "chat-only" : "", m.reasoning ? "reasoning" : "",
                  m.context ? Math.round(m.context/1000) + "k ctx" : ""].filter(Boolean).join(" · ");
    return `<option value="${esc(m.id)}">${esc(tags)}</option>`;
  }).join("");
  const msg = document.getElementById("model-list-msg");
  if (!msg) return;
  if (modelCatalog.listed){
    const free = ms.filter(m=>m.free), freeTools = free.filter(m=>m.tools);
    msg.textContent = `${ms.length} models on ${modelCatalog.endpoint}` +
      (free.length ? ` · ${free.length} free, ${freeTools.length} of those tool-capable (Waku needs tool calling)` : "") +
      ` · type in the field above to search`;
  } else {
    msg.textContent = modelCatalog.error ? `model list unavailable: ${modelCatalog.error}` : "";
  }
  renderCatalog();
}

// The catalog browser (shown when the endpoint lists models, i.e. OpenRouter):
// suggested picks per SLOT, a search + free/tools filter, and the full list
// grouped by vendor. Every row can go to either slot: "use" is the loop model
// (needs tool calling), "gate" is the small model (needs terse JSON, so
// reasoning models are steered away from it).
let catFilter = {q: "", free: false, tools: false};

function modelRow(m, st){
  const cur = m.id === st.model, curGate = m.id === st.small_model;
  const isPinned = (st.pinned || []).some(p => p.provider === st.provider && p.model === m.id);
  const price = m.free ? "free" : (m.price_out != null ? `$${m.price_in}/$${m.price_out} per M` : "");
  const tags = [price, m.context ? Math.round(m.context/1000) + "k ctx" : ""]
               .filter(Boolean).join(" · ");
  return `<div class="tool" style="display:flex;align-items:center;gap:8px;padding:6px 8px">
    <a class="pinstar ${isPinned?"on":""}" title="${isPinned?"pinned to Your models — click to remove":"pin to Your models (shows in chat switcher)"}"
       onclick="pinModel('${esc(st.provider)}','${esc(m.id)}','${isPinned?"unpin":"pin"}')">${isPinned?"★":"☆"}</a>
    <code style="flex:1;word-break:break-all">${esc(m.id)}</code>
    <span class="meta" style="margin:0;white-space:nowrap">${esc(tags)}</span>
    ${m.reasoning ? `<span class="srcpill apple" title="thinks out loud before answering: fine for the loop, a poor fit for the gate's tiny token budget">reasoning</span>` : ""}
    ${curGate ? `<span class="srcpill">GATE</span>`
              : `<a class="reveal" data-id="${esc(m.id)}" onclick="switchModel(this.dataset.id,true)" title="use as the gate/summary model">gate</a>`}
    ${cur ? `<span class="srcpill" style="background:var(--good-soft);color:var(--good)">CURRENT</span>`
          : (m.tools === false ? `<span class="meta" style="margin:0" title="the loop needs tool calling">chat-only</span>`
                               : `<button class="save" data-id="${esc(m.id)}" onclick="switchModel(this.dataset.id)">use</button>`)}
  </div>`;
}

// Slot suggestions are transparent heuristics over catalog metadata (tools,
// price, context, reasoning), NOT a quality leaderboard. Loop: tool-capable,
// free first, then biggest context. Gate: cheap non-reasoning instruct-style.
const GATE_HINT = /instruct|gemma|haiku|flash|mini|nano|lite|small/;
function loopPicks(ms){
  return ms.filter(m => m.tools)
           .sort((a,b) => (b.free - a.free) || ((b.context||0) - (a.context||0))).slice(0, 4);
}
function gatePicks(ms){
  return ms.filter(m => m.tools !== false && m.reasoning !== true
                        && (m.free || (m.price_out != null && m.price_out <= 1.5)))
           .sort((a,b) => (GATE_HINT.test(b.id) - GATE_HINT.test(a.id))
                        || (b.free - a.free) || ((a.price_out||99) - (b.price_out||99))).slice(0, 4);
}

function renderCatalog(){
  const box = document.getElementById("catalog");
  if (!box || !modelCatalog) return;
  const all = modelCatalog.models || [];
  const head = document.getElementById("catalog-h");
  if (!modelCatalog.listed || !all.length){
    box.style.display = "none"; if (head) head.style.display = "none"; return;
  }
  box.style.display = ""; if (head) head.style.display = "";
  box.innerHTML = `
    <div class="cat-controls">
      <input id="cat-q" type="text" placeholder="filter models…" value="${esc(catFilter.q)}"
        onfocus="markEditing()" oninput="catFilter.q=this.value;renderCatalogList()">
      <label class="meta" style="margin:0"><input type="checkbox" id="cat-free" ${catFilter.free?"checked":""}
        onchange="catFilter.free=this.checked;renderCatalogList()"> free only</label>
      <label class="meta" style="margin:0"><input type="checkbox" id="cat-tools" ${catFilter.tools?"checked":""}
        onchange="catFilter.tools=this.checked;renderCatalogList()"> tool-capable only</label>
    </div>
    <div id="cat-list"></div>
    <div class="meta" id="free-switch-msg" style="margin-top:6px"></div>`;
  renderCatalogList();
}

function renderCatalogList(){
  const list = document.getElementById("cat-list");
  if (!list || !modelCatalog) return;
  const st = (D && D.settings) || {};
  const all = modelCatalog.models || [];
  const q = catFilter.q.trim().toLowerCase();
  const shown = all.filter(m => (!q || m.id.toLowerCase().includes(q))
                             && (!catFilter.free || m.free)
                             && (!catFilter.tools || m.tools));
  let h = "";
  if (!q && !catFilter.free && !catFilter.tools){
    h += `<div class="meta" style="margin:4px 0">Suggested picks: transparent heuristics from catalog metadata (tools, price, context), not a quality leaderboard</div>`;
    h += `<div class="meta" style="margin:6px 0 2px"><b>For the loop</b> (needs tool calling; free first, biggest context)</div>`;
    h += loopPicks(all).map(m => modelRow(m, st)).join("");
    h += `<div class="meta" style="margin:10px 0 2px"><b>For the gate</b> (cheap, terse, non-reasoning)</div>`;
    h += gatePicks(all).map(m => modelRow(m, st)).join("");
    h += `<div class="meta" style="margin:12px 0 2px"><b>Everything</b> (${all.length} models, by vendor)</div>`;
  } else {
    h += `<div class="meta" style="margin:4px 0">${shown.length} of ${all.length} models</div>`;
  }
  const vendors = {};
  shown.forEach(m => (vendors[m.id.split("/")[0]] ??= []).push(m));
  const expand = q || catFilter.free || catFilter.tools;
  h += Object.keys(vendors).sort().map(v => `
    <details ${expand ? "open" : ""}><summary><code>${esc(v)}</code>
      <span class="meta" style="margin-left:6px">${vendors[v].length}${vendors[v].some(m=>m.free) ? " · has free" : ""}</span></summary>
      ${vendors[v].map(m => modelRow(m, st)).join("")}
    </details>`).join("");
  list.innerHTML = h;
}

// One-click model switch: same /api/settings path as the Save button, keeping
// the other slot (main vs gate) as-is. Live for the next turn.
async function switchModel(id, asGate){
  const st = (D && D.settings) || {};
  const msg = document.getElementById("free-switch-msg");
  if (msg) msg.textContent = "switching…";
  const r = await applyModel({provider: st.provider,
    model: asGate ? st.model : id, small_model: asGate ? id : st.small_model});
  if (msg) msg.textContent = r.error ? ("Error: " + r.error)
                                     : (asGate ? "Gate model is now " : "Model is now ") + id + ". Applies from your next message.";
}

// "Your models" — the curated shortlist the chat pill shows, spanning every
// provider. The first pinned model per provider is that provider's default
// (used when you switch to it). pin/unpin/default all POST /api/pin.
function yourModelsCard(st){
  const pinned = st.pinned || [];
  const providers = (st.providers || []).map(p => p.name);
  const rows = pinned.map(p => `
    <div class="pinrow ${(p.provider===st.provider && p.model===st.model)?"on":""}">
      <span class="mm-prov">${esc(p.provider)}</span>
      <code style="flex:1;word-break:break-all">${esc(p.model)}</code>
      ${p.default ? `<span class="srcpill" title="this provider's default model">default</span>`
                  : `<a class="reveal" onclick="pinModel('${esc(p.provider)}','${esc(p.model)}','default')" title="make this ${esc(p.provider)}'s default">make default</a>`}
      <a class="reveal" onclick="pinModel('${esc(p.provider)}','${esc(p.model)}','unpin')" title="remove from your list">remove</a>
    </div>`).join("") || `<div class="meta">No models pinned yet — add one below.</div>`;
  // The add row is self-contained: pick any provider + type/choose a model id,
  // then Add. Works even for providers with no live catalog. The datalist
  // suggests the CURRENT provider's models (the only one we've fetched).
  const provOpts = providers.map(n => `<option value="${esc(n)}" ${n===st.provider?"selected":""}>${esc(n)}</option>`).join("");
  // Populate the model <select> for the initially-selected provider once the
  // card is in the DOM (a fresh fetch of that provider's catalog).
  setTimeout(() => loadAddModels(st.provider), 0);
  return `<h2>Your models <span class="meta" style="font-weight:400">— what the chat switcher shows</span></h2>
    <div class="card">
      ${rows}
      <div class="addmodel">
        <select id="add-prov" onfocus="markEditing()" onchange="loadAddModels(this.value)">${provOpts}</select>
        <select id="add-model"><option value="">loading models…</option></select>
        <button class="save" onclick="addPinnedModel()">Add</button>
      </div>
      <div class="meta" style="margin-top:6px" id="add-msg">Pick a provider, choose a model, then Add.</div>
    </div>`;
}

// Fill the add-row model <select> with a provider's catalog (any provider, not
// just the active one — the backend takes a ?provider= override).
async function loadAddModels(provider){
  const sel = document.getElementById("add-model");
  const msg = document.getElementById("add-msg");
  if (!sel) return;
  sel.innerHTML = `<option value="">loading ${esc(provider)} models…</option>`;
  let data;
  try { data = await (await fetch("/api/models?provider=" + encodeURIComponent(provider))).json(); }
  catch(e){ sel.innerHTML = `<option value="">couldn't load — pick another provider</option>`; return; }
  const ms = data.models || [];
  sel.innerHTML = `<option value="">choose a model…</option>` + ms.map(m => {
    const meta = [m.free ? "free" : (m.price_out != null ? `$${m.price_in}/$${m.price_out}` : ""),
                  m.context ? Math.round(m.context/1000) + "k" : ""].filter(Boolean).join(" · ");
    return `<option value="${esc(m.id)}">${esc(m.id)}${meta ? "  ("+esc(meta)+")" : ""}</option>`;
  }).join("");
  if (msg) msg.innerHTML = data.listed
    ? `${ms.length} models on <b>${esc(provider)}</b>. Choose one and Add — or star models in the catalog below.`
    : data.error
      ? `Couldn't list <b>${esc(provider)}</b>: <span style="color:var(--bad)">${esc(data.error)}</span> — showing its defaults only.`
      : `No live catalog for <b>${esc(provider)}</b> (only its defaults shown). Set its API key to list more.`;
}

async function addPinnedModel(){
  const provider = document.getElementById("add-prov")?.value;
  const model = document.getElementById("add-model")?.value;
  if (!provider || !model) return;
  await pinModel(provider, model, "pin");   // refreshes; the row appears in the list
}

async function pinModel(provider, model, action){
  const r = await postJSON("/api/pin", {provider, model, action});
  if (!r.error){ editing = false; await refresh(); }
}

