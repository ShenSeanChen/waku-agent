// Hosted Waku only (spec 030): the Slack card on the Connections page, and
// the "Add Waku to Slack" line on Overview.
//
// A hosted container holds no Slack token and does not know whether its
// person connected a workspace. The platform's gateway does, and answers
// GET /hosted/slack itself (hosted/gateway/slack_state.py): whether the
// server has a Slack app, the workspace's name and state, and the two
// addresses the card links to. Nothing here runs unless /api/data says
// `hosted`, so a dashboard on a person's own machine never asks.
//
// The workspace's name is Slack's text, so it goes through esc, and an
// address is linked only when it is http(s) (connectionAction checks).

const HOSTED_SLACK = {data: null, at: 0, loading: false};
// How long an answer is kept before the page asks again.
const HOSTED_SLACK_MS = 60000;

// Reached from refresh(), so a timer's poll reaches it too: `background` is
// threaded, and a poll's request carries the header that says so.
async function loadHostedSlack(background = false){
  if (HOSTED_SLACK.loading) return;
  HOSTED_SLACK.loading = true;
  try {
    const res = await fetch("/hosted/slack", background ? {headers: BG} : undefined);
    if (res.ok){ HOSTED_SLACK.data = await res.json(); HOSTED_SLACK.at = Date.now(); }
  } catch(e){ /* the gateway is restarting, or this is not hosted: keep the last answer */ }
  finally { HOSTED_SLACK.loading = false; }
  if (activeView === "connections" || activeView === "overview") render();
}

// Called after every refresh. It asks only on hosted Waku, and only when the
// last answer is old.
function hostedAfterRefresh(background){
  if (D && D.hosted && Date.now() - HOSTED_SLACK.at > HOSTED_SLACK_MS) loadHostedSlack(background);
}

// The Slack card, in the shape connectionCard draws, or null when there is
// nothing to show: not hosted, no answer yet, or a server with no Slack app.
function hostedSlackCard(d){
  const s = HOSTED_SLACK.data;
  if (!d.hosted || !s || !s.available) return null;
  const card = {id: "slack", name: "Slack", logo: "logos/connections/slack.svg", why: "",
                what: "Mention @Waku in a channel, and it answers in the thread."};
  if (s.connected)
    // Both open Slack's approval page. The first is what a person wants when
    // Waku asks for a new permission: the same workspace, approved again.
    return {...card, state: "connected", label: "connected", why: s.team_name,
            action: {kind: "href", label: "Update permissions", target: s.install_url},
            action2: {kind: "href", label: "Connect a different workspace", target: s.install_url}};
  if (s.needs_reconnect)
    return {...card, state: "needs-setup", label: "needs sign-in",
            why: `Waku lost its connection to ${s.team_name}.`,
            action: {kind: "href", label: "Reconnect Slack", target: s.install_url}};
  return {...card, state: "not-configured", label: "not connected",
          action: {kind: "href", label: "Add Waku to Slack", target: s.setup_url}};
}

// One line at the top of Overview while Slack is not connected. Its link
// opens Connections at the Slack card.
function hostedSlackLine(d){
  const card = hostedSlackCard(d);
  if (!card || card.state === "connected") return "";
  return uiNotice("note", card.why ? esc(card.why) : "Waku is not in your Slack yet.",
    uiLink(esc(card.action.label), "#connections/slack"));
}
