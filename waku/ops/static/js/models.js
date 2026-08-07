// waku dashboard — model picker/catalog/pins, and the remaining Settings toggle.
// Split out of app.js: classic <script>, shared global scope (no build
// step, no modules). Load order + rules: static/README.md.

async function saveSettings(){
  const experimental = document.getElementById("set-experimental")?.value;
  const graph_workflows = document.getElementById("set-graph-workflows")?.value;
  document.getElementById("set-msg").textContent = "switching…";
  const r = await postJSON("/api/settings", {experimental, graph_workflows});
  document.getElementById("set-msg").textContent = r.error ? ("Error: "+r.error) : "Saved.";
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

// One-click model switch: posts to /api/providers so the provider/model pair
// is validated and applied by the integrations layer. Keeps the other slot
// (main vs gate) as-is. Live for the next turn.
async function switchModel(id, asGate){
  const st = (D && D.settings) || {};
  const msg = document.getElementById("free-switch-msg");
  if (msg) msg.textContent = "switching…";
  const payload = {provider: st.provider,
    model: asGate ? st.model : id, small_model: asGate ? id : st.small_model};
  const r = await postJSON("/api/providers", payload);
  if (!r.error){ editing = false; modelCatalog = null; await refresh(); }
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

// --- Models page: a grid of provider cards (logo, name, status dot, actions)
// plus an edit modal. Status is derived, never stored: unconfigured = no key,
// configured = key set but disabled, enabled = key set and available. The
// ACTIVE provider (settings.provider) can't be disabled (server guards too).
function providerCardStatus(p, st){
  const keySet = !!(p.fields && p.fields[0] && p.fields[0].configured);
  if (!keySet) return "unconfigured";
  return (st.disabled_providers || []).includes(p.key) ? "configured" : "enabled";
}

function modelsGrid(d){
  const st = d.settings || {};
  const rank = p => p.key === st.provider ? 0
    : providerCardStatus(p, st) === "enabled" ? 1
    : providerCardStatus(p, st) === "configured" ? 2 : 3;
  const providers = (d.providers || []).slice()
    .sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name));
  return `<div class="provgrid">` + providers.map(p => providerCard(p, st)).join("") +
    `</div><div id="prov-modal-root"></div>`;
}

function providerCard(p, st){
  const status = providerCardStatus(p, st);
  const current = p.key === st.provider;
  const dot = status === "enabled" ? "var(--good)" : status === "configured" ? "#4c9aff" : "var(--bad)";
  return `<div class="provcard" data-provider="${esc(p.key)}">
    ${current ? `<span class="srcpill prov-current" style="background:var(--good-soft);color:var(--good)">current</span>` : ""}
    <img class="provlogo" src="/static/logos/${esc(p.key)}.svg" alt="" onerror="this.style.display='none'">
    <div class="provname">${esc(p.name)}</div>
    <div class="provstatus"><span class="provdot" style="background:${dot}"></span>${status}</div>
    <div class="provactions">
      <button class="save ghost" onclick="openProviderModal('${esc(p.key)}')">edit</button>
      ${status === "configured" ? `<button class="save ghost" onclick="toggleProvider('${esc(p.key)}',false)">enable</button>` : ""}
      ${status === "enabled" && !current ? `<button class="save ghost" onclick="toggleProvider('${esc(p.key)}',true)">disable</button>` : ""}
    </div></div>`;
}

// enable/disable a provider (the grid button). Server keeps the key; the
// provider just leaves/enters the available list.
async function toggleProvider(provider, disabled){
  const r = await postJSON("/api/providers", {provider, disabled});
  if (!r.ok) alert(r.error || "update failed");
  else { editing = false; await refresh(); }
}

// --- edit modal: API key (+ main/small model when this provider is current,
// with a searchable live catalog) and a "set as current" action.
function openProviderModal(provider){
  markEditing();   // keep the 5s refresh loop from wiping this modal
  const st = (D && D.settings) || {};
  const p = (D.providers || []).find(x => x.key === provider);
  if (!p) return;
  const current = provider === st.provider;
  const f = (p.fields || [])[0] || {};
  const baseField = (p.fields || []).find(field => field.name.endsWith("_BASE_URL"));
  const selectedBaseUrl = current && st.base_url ? st.base_url : (baseField?.value || "");
  const root = document.getElementById("prov-modal-root");
  root.innerHTML = `<div class="provmodal-back" onclick="closeProviderModal()">
    <div class="provmodal${current ? " provmodal-models" : ""}" onclick="event.stopPropagation()">
      <div class="u" style="display:flex;justify-content:space-between;align-items:center">
        <b>${esc(p.name)}</b><a class="reveal" onclick="closeProviderModal()">✕</a></div>
      <label class="fld"><span>API key <span class="meta">(${esc(f.name || "")})</span>
        ${f.configured ? `<span class="srcpill" style="background:var(--good-soft);color:var(--good)">set ····${esc(f.last4 || "")}</span>`
                       : `<span class="srcpill apple">not set</span>`}</span>
        <input type="password" id="pm-key" placeholder="${f.configured ? "key on file — blank keeps it" : "paste key"}"></label>
      ${baseField ? `<label class="fld"><span>Base URL <span class="meta">(select the API key's region)</span></span>
        <select id="pm-base-url" onfocus="markEditing()">
          ${(baseField.options || []).map((url, index) => {
            const label = (baseField.option_labels || [])[index];
            return `<option value="${escAttr(url)}" ${url===selectedBaseUrl?"selected":""}>${label?esc(label)+" — ":""}${esc(url)}</option>`;
          }).join("")}
        </select></label>` : ""}
      ${current ? `
      ${renderModelPicker("pm-model", "Main model (runs the loop; needs tool calling)", st.model || "")}
      ${renderModelPicker("pm-small-model", "Gate / summary model", st.small_model || "")}` : ""}
      <div style="display:flex;gap:8px;margin-top:10px">
        <button class="save" id="pm-save" onclick="saveProviderModal('${esc(provider)}')">Save</button>
        ${!current ? `<button class="save ghost" id="pm-make-current" onclick="makeCurrentProvider('${esc(provider)}')">Set as current provider</button>` : ""}
      </div>
      <span class="meta" id="pm-msg"></span>
    </div></div>`;
  if (current) loadModalModels(provider);
}

function closeProviderModal(){
  editing = false;
  const root = document.getElementById("prov-modal-root");
  if (root) root.innerHTML = "";
}

// Populate both modal pickers from one request: this provider's live catalog,
// or its defaults when there is no catalog. Manual typing always still works.
async function loadModalModels(provider){
  setupModelPickers([], provider);
  setModelPickerMeta("Loading models…");
  let data;
  try {
    const response = await fetch("/api/models?provider=" + encodeURIComponent(provider));
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    data = await response.json();
  } catch(e){
    data = {models: [], listed: false, error: e.message || String(e)};
  }
  setupModelPickers(data.models || [], provider);
  if (!data.listed){
    setModelPickerMeta(data.error && !(data.models || []).length
      ? "Could not load catalog — you can still type any model id."
      : data.error
        ? "Could not load catalog — showing defaults only."
      : "Live catalog unavailable — showing defaults.");
  }
}

// Shared model list for the currently open modal.
let _modalModels = [];
let _activeModelPicker = null;
let _outsidePickerListener = false;

function escAttr(s){
  return esc(s).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function renderModelPicker(id, label, value){
  return `<label class="fld">${esc(label)}
    <div class="model-picker" id="${escAttr(id)}-picker">
      <div class="model-picker-input">
        <input type="text" id="${escAttr(id)}" value="${escAttr(value || "")}" autocomplete="off" onfocus="markEditing()" onclick="event.stopPropagation()">
        <button type="button" class="model-picker-toggle" onclick="toggleModelPicker('${escAttr(id)}'); event.stopPropagation();" aria-label="toggle models" aria-controls="${escAttr(id)}-list" aria-expanded="false">▾</button>
      </div>
      <div class="model-picker-list" id="${escAttr(id)}-list" role="listbox">
        <input type="text" class="model-picker-search" id="${escAttr(id)}-search" placeholder="filter models..." autocomplete="off" aria-label="filter models" oninput="filterModelPicker('${escAttr(id)}')" onfocus="markEditing()" onclick="event.stopPropagation()">
        <div class="model-picker-items" id="${escAttr(id)}-items"></div>
        <div class="model-picker-meta" id="${escAttr(id)}-meta" aria-live="polite"></div>
      </div>
    </div>
  </label>`;
}

function setupModelPickers(models, provider){
  _modalModels = Array.isArray(models) ? models : [];
  ["pm-model", "pm-small-model"].forEach(id => {
    const input = document.getElementById(id);
    if (!input) return;
    const itemsBox = document.getElementById(id + "-items");
    const search = document.getElementById(id + "-search");
    if (itemsBox && !itemsBox.dataset.modelPickerBound){
      itemsBox.dataset.modelPickerBound = "true";
      itemsBox.addEventListener("click", e => {
        const item = e.target.closest(".model-picker-item");
        if (!item) return;
        e.stopPropagation();
        selectModelPicker(id, item.dataset.model || "");
      });
      search?.addEventListener("keydown", e => {
        if (e.key !== "Enter") return;
        const query = search.value.toLowerCase();
        const first = _modalModels.find(m => (m.id || "").toLowerCase().includes(query));
        if (!first) return;
        e.preventDefault();
        selectModelPicker(id, first.id || "");
      });
    }
    renderModelPickerItems(id, (search?.value || "").toLowerCase());
  });
  if (!_outsidePickerListener){
    _outsidePickerListener = true;
    document.addEventListener("click", e => {
      if (!e.target.closest?.(".model-picker")) closeAllModelPickers();
    }, true);
    document.addEventListener("keydown", e => { if (e.key === "Escape") closeAllModelPickers(); });
  }
}

function setModelPickerMeta(message){
  ["pm-model", "pm-small-model"].forEach(id => {
    const meta = document.getElementById(id + "-meta");
    if (meta) meta.textContent = message;
  });
}

function toggleModelPicker(id){
  const list = document.getElementById(id + "-list");
  if (!list) return;
  const isOpen = list.classList.contains("open");
  closeAllModelPickers();
  if (!isOpen){
    list.classList.add("open");
    list.parentElement.querySelector(".model-picker-toggle")?.setAttribute("aria-expanded", "true");
    _activeModelPicker = id;
    const search = document.getElementById(id + "-search");
    if (search) search.focus();
  }
}

function closeAllModelPickers(){
  document.querySelectorAll(".model-picker-list.open").forEach(el => {
    el.classList.remove("open");
    el.parentElement.querySelector(".model-picker-toggle")?.setAttribute("aria-expanded", "false");
  });
  _activeModelPicker = null;
}

function closeModelPicker(id){
  const list = document.getElementById(id + "-list");
  if (list){
    list.classList.remove("open");
    list.parentElement.querySelector(".model-picker-toggle")?.setAttribute("aria-expanded", "false");
  }
  if (_activeModelPicker === id) _activeModelPicker = null;
}

function filterModelPicker(id){
  const query = (document.getElementById(id + "-search")?.value || "").toLowerCase();
  renderModelPickerItems(id, query);
}

function renderModelPickerItems(id, query){
  const itemsBox = document.getElementById(id + "-items");
  const metaBox = document.getElementById(id + "-meta");
  if (!itemsBox) return;
  const filtered = _modalModels.filter(m => (m.id || "").toLowerCase().includes(query));
  itemsBox.innerHTML = filtered.map((m, index) => `<div class="model-picker-item${index === 0 ? " active" : ""}" role="option" data-model="${escAttr(m.id)}">${esc(m.id)}</div>`).join("");
  if (metaBox){
    if (_modalModels.length === 0) metaBox.textContent = "No models loaded — you can still type any model id.";
    else if (filtered.length === 0) metaBox.textContent = `No models match "${query}".`;
    else metaBox.textContent = "";
  }
}

function selectModelPicker(id, value){
  const input = document.getElementById(id);
  if (input){
    input.value = value;
    input.focus();
  }
  closeModelPicker(id);
}

function modalKeyPayload(provider){
  const key = document.getElementById("pm-key")?.value;
  const payload = {provider};
  if (key) payload.key = key;
  const baseUrl = document.getElementById("pm-base-url")?.value;
  if (baseUrl) payload.base_url = baseUrl;
  return payload;
}

function setProviderModalBusy(activeId, busy){
  ["pm-save", "pm-make-current"].forEach(id => {
    const button = document.getElementById(id);
    if (!button) return;
    if (!button.dataset.label) button.dataset.label = button.textContent;
    button.disabled = busy;
    button.textContent = busy && id === activeId
      ? (id === "pm-make-current" ? "Switching…" : "Saving…")
      : button.dataset.label;
  });
  if (busy){
    const msg = document.getElementById("pm-msg");
    if (msg) msg.textContent = activeId === "pm-make-current"
      ? "Switching provider…" : "Saving and validating changes…";
  }
}

async function submitProviderModal(provider, payload, activeId){
  setProviderModalBusy(activeId, true);
  let r;
  try { r = await postJSON("/api/providers", payload); }
  catch(e){ r = {ok:false, error:e.message || String(e)}; }
  if (!r.ok){
    setProviderModalBusy(activeId, false);
    const msg = document.getElementById("pm-msg");
    if (msg) msg.textContent = r.error || "update failed";
    return;
  }
  editing = false;
  closeProviderModal();
  await refresh();
}

async function saveProviderModal(provider){
  const st = (D && D.settings) || {};
  const payload = modalKeyPayload(provider);
  payload.activate = false;
  if (provider === st.provider){
    payload.model = document.getElementById("pm-model")?.value ?? "";
    payload.small_model = document.getElementById("pm-small-model")?.value ?? "";
  }
  await submitProviderModal(provider, payload, "pm-save");
}

// "Set as current": apply_provider switches provider and picks its default
// models when none are passed (keeps the key field if one was just typed).
async function makeCurrentProvider(provider){
  await submitProviderModal(provider, modalKeyPayload(provider), "pm-make-current");
}
