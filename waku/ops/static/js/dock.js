// waku dashboard — chat sessions/history (loadThreadInto), model chip, stats toggle.
// Split out of app.js: classic <script>, shared global scope (no build
// step, no modules). Load order + rules: static/README.md.

// --- chat sessions (the "New chat" + history picker, like a chat app)
let SESSION = "default";
async function newChat(){
  const r = await postJSON("/api/session", {action:"new"});
  if (r.session_id){ liveView = null; SESSION = r.session_id; CHAT.length = 0; syncChatLogs(); }
  closeSessMenu();
}
// The ONE way to pull a thread's rows into the dock, so the paths can't drift
// (they used to: some dropped meta, some added a length-guard, some didn't).
//   mode 'switch'  -> action:switch, also moves the agent's active thread
//   mode 'history' -> action:history, read-only ('__all__' = full timeline)
// Replaces CHAT + repaints, unless `guard` is set and the length is unchanged
// (the live-poll case, to avoid a needless redraw). Returns the items or null.
async function loadThreadInto(id, {mode = "history", setSession = false, guard = false} = {}){
  const r = await postJSON("/api/session", {action: mode, id});
  if (!r.ok) return null;
  const fresh = (r.history || []).map(histItem);
  if (guard && fresh.length === CHAT.length) return fresh;   // unchanged -> skip repaint
  if (setSession) SESSION = r.session_id;
  CHAT.length = 0; fresh.forEach(m => CHAT.push(m)); syncChatLogs();
  return fresh;
}
async function switchSession(id){
  await loadThreadInto(id, {mode: "switch", setSession: true});
  closeSessMenu();
}
// Open a conversation from the Gateway inbox: load it into the dock (the active
// thread), keep it live-synced (so new Telegram/voice messages appear), and make
// sure the dock is visible.
let liveView = null;   // a conversation opened from the inbox, kept live-updated
async function openConversation(id){
  document.body.classList.remove("dock-closed");
  localStorage.setItem("dockClosed", "0");
  liveView = id;
  await switchSession(id);   // switch the agent so a reply continues this thread
  render();                  // reflect the active-session highlight in the inbox
}
// Read-only "everything" view: the full cross-thread timeline in the dock, like
// the Loop tab but as chat. Doesn't switch the agent — your next message still
// goes to the active thread; this is purely for reading your whole history.
async function viewAllHistory(){
  closeSessMenu();
  document.body.classList.remove("dock-closed");
  localStorage.setItem("dockClosed", "0");
  liveView = "__all__";
  await loadThreadInto("__all__");
}
// Re-pull the opened conversation each refresh so incoming messages from another
// gateway (your phone) show up live — unless a turn is mid-stream in the dock.
async function syncLiveView(){
  if (!liveView || CHAT.some(m => m.pending)) return;
  await loadThreadInto(liveView, {guard: true});   // guard: repaint only if changed
}
// The history and model menus are both ui.js's one menu (openMenu), which
// closes itself on an outside click or Escape. Only one is open at a time, so
// either close function closes whichever it is.
function closeSessMenu(){ closeMenu(); }
function toggleSessMenu(ev){
  ev.stopPropagation();
  if (document.getElementById("ui-menu") && _menuTrigger === ev.currentTarget){ closeMenu(); return; }
  const sessions = (D && D.sessions) || [];
  // "All messages" shows the full cross-thread timeline (like the Loop tab, but
  // as chat) — so your whole history is one scroll, not split across threads.
  const allItem = uiMenuItem("<b>All messages</b>",
    {sub: "full timeline", on: liveView === "__all__", onclick: "viewAllHistory()",
     title: "every thread together, newest last"});
  const rows = sessions.length
    ? sessions.map(s => uiMenuItem(`${esc(s.title||s.id)} ${gwTags(s)}`,
        {sub: sessionMeta(s), on: s.id === SESSION, onclick: `openConversation('${esc(s.id)}')`})).join("")
    : `<div class="menu-empty">no past conversations yet</div>`;
  openMenu(ev.currentTarget, allItem + uiMenuSep() + rows, {width: "400px"});
}

// --- mini model switcher in the chat dock: a pill showing the current brain,
// clicking it drops the live catalog to swap without leaving the conversation.
// Posts to /api/providers (the same endpoint the Models page uses).
function syncModelChip(){
  const el = document.getElementById("modelchip");
  if (!el || !D || !D.settings) return;
  const st = D.settings;
  el.innerHTML = `<span class="mc-dot"></span><span class="mc-name">${esc(st.model || st.provider || "model")}</span><span class="mc-caret">&#9662;</span>`;
}
function closeModelMenu(){ closeMenu(); }

// --- per-turn stats toggle (gate / seconds / iterations / tools). On by
// default; the choice persists in localStorage. Hides the .tele blocks via a
// body class so it applies to already-rendered turns too.
function applyTele(){
  const off = localStorage.getItem("waku_tele") === "0";
  document.body.classList.toggle("no-tele", off);
  const b = document.getElementById("teletoggle");
  if (b) b.classList.toggle("on", !off);
}
function toggleTele(){
  const off = localStorage.getItem("waku_tele") === "0";
  localStorage.setItem("waku_tele", off ? "1" : "0");   // flip
  applyTele();
}
function toggleModelMenu(ev){
  ev.stopPropagation();
  if (document.getElementById("ui-menu") && _menuTrigger === ev.currentTarget){ closeMenu(); return; }
  const st = (D && D.settings) || {};
  // Disabled providers leave the switcher (the Models grid's disable button);
  // their pins stay on file and reappear when re-enabled.
  const disabled = st.disabled_providers || [];
  const pinned = (st.pinned || []).filter(p => !disabled.includes(p.provider));
  const items = pinned.length ? pinned.map(p =>
    uiMenuItem(`<code>${esc(p.model)}</code>${p.default ? " " + uiBadge("default", "value") : ""}`,
      {sub: esc(p.provider), on: p.provider === st.provider && p.model === st.model,
       onclick: `switchTo('${esc(p.provider)}','${esc(p.model)}')`})
  ).join("") : `<div class="menu-empty">No models pinned yet.</div>`;
  openMenu(ev.currentTarget, uiMenuLabel("Your models") + items + uiMenuSep()
    + uiMenuItem("Manage models…", {onclick: "location.hash='models';closeMenu()"}));
}
// Switch BOTH provider and model in one click (a pinned model can be any
// provider). Same-provider switch keeps the gate model; cross-provider lets the
// new provider's default gate model take over.
async function switchTo(provider, model){
  const st = (D && D.settings) || {};
  const chip = document.getElementById("modelchip");
  const name = chip && chip.querySelector(".mc-name");
  closeModelMenu();
  if (name) name.textContent = "switching…";
  await postJSON("/api/providers", {provider, model,
    small_model: provider === st.provider ? st.small_model : ""});
  await refresh();
}
