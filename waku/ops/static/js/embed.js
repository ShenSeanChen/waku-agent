// waku embedded chat — the bootstrap for embed.html (spec 008), LOADS LAST.
// Stands in for main.js on a page that is only the chat column: no views, no
// /api/data, no timers polling the whole dashboard. Classic <script>, shared
// global scope like the rest (static/README.md).

// --- the few globals render.js and dock.js expect from main.js -------------
// They are main.js's names with the chat column's meaning: there is no paused
// banner to clear and no view to re-render, and "refresh" re-reads only what
// the header draws.
let paused = false;
function render(){}

// What the header needs (History, the model picker, the thread to restore),
// read from /api/session?action=state rather than /api/data, which carries
// the memory, the traces and the spend: an embed session is refused that
// route (hosted/gateway/embed.py), and the chat does not need it.
//
// Always a person's doing, never a timer's: it runs when the page opens, after
// a turn they sent, and after a model they picked. Nothing here polls, so a
// framed chat left open never holds a hosted container awake.
async function refresh(){
  try {
    const res = await fetch("/api/session?action=state");
    noteStatus(res.status);
    if (!res.ok) return;
    const st = await res.json();
    D = {sessions: st.sessions || [], settings: st.settings || {},
         current_session: st.current_session};
    syncModelChip();
    applyTele();
  } catch(e){ /* the container is waking or gone: keep what is shown */ }
}

// --- the page around us (spec 008 F) ---------------------------------------
// The origins this page was served with: hosted, the gateway's frame-ancestors
// allowlist; locally, waku.one's three hosts (dashboard.py, embed_page).
const EMBED_ORIGINS = (document.body.dataset.embedOrigins || "").split(/\s+/).filter(Boolean);

// The origin of the page that framed us, or null. From document.referrer,
// which is the framing page for the iframe's first document and survives the
// /auth/embed redirect, and ONLY if it is on the allowlist: postMessage is
// never sent to "*", and never to an origin the gateway would not let frame
// this page. Not framed, no referrer, or a referrer off the list: null, and
// nothing is posted.
function embedParentOrigin(){
  if (window.parent === window) return null;
  let origin = null;
  try { origin = new URL(document.referrer).origin; } catch(e){ return null; }
  return EMBED_ORIGINS.includes(origin) ? origin : null;
}
function tellParent(message){
  const origin = embedParentOrigin();
  if (!origin) return;
  window.parent.postMessage({source: "waku-agent", ...message}, origin);
}

// turn-done after every turn, however it ended: credits may have moved even
// when the reply is an error. report-saved once per saved report. The header
// is re-read after each turn too, since nothing here polls: History learns of
// the conversation the turn just wrote to.
const reportsTold = new Set();
turnWatchers.push(ev => {
  if (ev.kind === "report" && ev.memory_id && !reportsTold.has(ev.memory_id)){
    reportsTold.add(ev.memory_id);
    tellParent({type: "report-saved", memory_id: ev.memory_id, title: ev.title || ""});
  } else if (ev.kind === "turn_end"){
    tellParent({type: "turn-done", credits_changed: true});
    refresh();
  }
});

// session-expired when a chat call comes back 401: the embed session ended
// (12 hours, or a sign-out anywhere). Every call the chat makes reports its
// status through noteStatus (util.js). Told once; waku.one asks /v1/embed for
// a new code and reloads the frame.
let expiredTold = false;
statusWatchers.push(status => {
  if (status !== 401 || expiredTold) return;
  expiredTold = true;
  tellParent({type: "session-expired"});
});

// --- the console's theme (waku-memory spec 040 T) ---------------------------
// Framed, the chat wears the theme of the page around it, not its own. The
// first paint comes from the server: waku.one adds ?theme= to the address and
// dashboard.py puts data-theme on <html> before any CSS applies. After that
// the console posts {"source": "waku-console", "type": "theme", "theme": ...}
// whenever its theme changes, an OS change included when it follows the
// system.
//
// NEVER STORED. The frame shares localStorage with the person's own dashboard
// on the same host, where "waku-theme" is the choice they made with the
// toggle. The console's theme is the console's: writing it there would change
// the dashboard's look the next time they open it directly. So it is applied
// and held here, and the stored choice is read only when no console theme
// came at all (an old waku.one, or the page opened on its own).
const CONSOLE_THEMES = ["light", "dark"];
const servedTheme = document.documentElement.dataset.theme;
let consoleTheme = CONSOLE_THEMES.includes(servedTheme) ? servedTheme : null;
function applyConsoleTheme(t){
  consoleTheme = t;
  applyTheme(t);   // theme.js; deliberately not cycleTheme, which stores it
}

// --- what the page around us may ask (waku-memory spec 040 P2, T) -----------
// Two messages, and only these two:
//   {"source": "waku-console", "type": "new-chat"}: waku.one's own "New chat"
//     starts one here, through the same newChat() as "+ New chat". A chat
//     that is already empty is left as it is.
//   {"source": "waku-console", "type": "theme", "theme": "light"|"dark"}: the
//     console's theme, applied and not stored (above).
// Accepted ONLY from window.parent, and only when its origin is on the
// allowlist this page was served with: never from "*", never from this
// frame's own origin, never from another window. Every other message is
// ignored, a theme other than exactly "light" or "dark" included.
function acceptParentMessage(event){
  if (!event || event.source !== window.parent || window.parent === window) return false;
  const own = window.location && window.location.origin;
  if (!event.origin || event.origin === own || !EMBED_ORIGINS.includes(event.origin)) return false;
  const data = event.data;
  if (!data || typeof data !== "object" || data.source !== "waku-console") return false;
  if (data.type === "theme"){
    if (typeof data.theme !== "string" || !CONSOLE_THEMES.includes(data.theme)) return false;
    applyConsoleTheme(data.theme);
    return true;
  }
  if (data.type !== "new-chat") return false;
  if (!CHAT.length) return false;   // already a new chat: nothing to start
  newChat();
  return true;
}
window.addEventListener("message", acceptParentMessage);

// --- bootstrap --------------------------------------------------------------
applyTheme(consoleTheme || currentTheme());
watchSlots();
wireComposer();
syncChatLogs();
(async () => {
  await refresh();
  // Restore the current thread so a reload never looks like it lost the chat.
  if (D && D.current_session) await loadThreadInto(D.current_session, {setSession: true});
})();
