// The sign-in page. It holds an access token for the length of one fetch and
// writes it nowhere.
//
// flowType implicit and persistSession false are the spec's, and they are
// what make GET /login's Clear-Site-Data safe to send: there is no PKCE
// verifier that has to survive it, and no Supabase token is left in this
// origin's storage for the next person at this browser. Both are properties
// of what this file DOES rather than options passed to a library -- see
// below, where the one call Supabase is needed for is written out.
//
// WHY THERE IS NO SUPABASE CLIENT HERE. Until 2026-09-27 this page loaded the
// 217945-byte UMD build of @supabase/supabase-js to make exactly one call,
// signInWithOtp, and to make it on a page that sends
// Clear-Site-Data: "cache", "storage" -- so the browser threw the bundle away
// and fetched all 218 KB again on every single sign-in. The fragment was
// already parsed by hand below, because detectSessionInUrl is off; the
// session was already POSTed to this origin by hand. The library was one
// fetch wearing 218 KB. It is now that fetch, and the request on the wire is
// byte-for-byte what 2.117.1 sent: same path, same query, same body keys.
// evals/deterministic/hosted/test_login_page.py pins that shape, because the
// thing a hand-rolled request breaks is the one we cannot see from here.
(function () {
  "use strict";
  var url = document.body.dataset.supabaseUrl.replace(/\/+$/, "");
  var key = document.body.dataset.supabaseKey;
  var status = document.getElementById("status");
  var form = document.getElementById("form");
  var button = document.getElementById("send");

  function say(text, bad) {
    status.textContent = text;
    // A failure has to be visible as one. Colour is not the only signal:
    // role="status" already speaks it, and the words say which it is.
    status.classList.toggle("bad", Boolean(bad));
  }

  function zone() {
    try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; }
    catch (e) { return "UTC"; }
  }

  // GoTrue's own error shape has changed names across versions and the
  // library papers over it. All four have been seen from this endpoint, so
  // all four are read rather than the one today's server happens to send.
  function reason(body) {
    return (body && (body.msg || body.message ||
                     body.error_description || body.error)) || "";
  }

  async function sendLink(email) {
    var target = url + "/auth/v1/otp?redirect_to=" +
                 encodeURIComponent(location.origin + "/login");
    var res = await fetch(target, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "apikey": key,
        "Authorization": "Bearer " + key
      },
      // create_user true is signInWithOtp's default and is deliberate: this
      // deployment turns signups OFF in Supabase, so an address nobody
      // invited is refused THERE, by the project's own setting, and the
      // refusal is the same one the library produced. Sending false instead
      // would move that decision into this file, where an operator who turns
      // signups on would not find it.
      body: JSON.stringify({
        email: email,
        data: {},
        create_user: true,
        gotrue_meta_security: {},
        code_challenge: null,
        code_challenge_method: null
      })
    });
    if (res.ok) { return ""; }
    var body = await res.json().catch(function () { return {}; });
    return reason(body) || "That did not work. Try again in a moment.";
  }

  async function exchange(token) {
    // Drop the token out of the address bar before anything else: it is in
    // the browser's history, the back button and every screen share until it
    // is gone.
    history.replaceState(null, "", location.pathname);
    say("Signing you in...");
    var res = await fetch("/auth/session", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({access_token: token, timezone: zone()})
    });
    var body = await res.json().catch(function () { return {}; });
    if (res.ok && body.enter) {
      if (await handedOver(body.enter)) {
        say("You're signed in in your other tab. You can close this one.");
        // Usually refused: a tab the mail client opened was not opened by a
        // script, and only those may close themselves. The sentence above
        // is the real answer; this is a courtesy where the browser allows it.
        try { window.close(); } catch (e) { /* refused is fine */ }
        return;
      }
      // location.assign and not a redirect the fetch would follow invisibly.
      location.assign(body.enter);
      return;
    }
    say(body.error || "That did not work. Ask for a new link.", true);
  }

  // --- the tab that asked for the link --------------------------------------
  //
  // WHY TABS TALK TO EACH OTHER. The link in the email opens a NEW tab, and
  // that tab signs in. The tab the person typed their address into was left
  // reading "Check your email for the link." forever, which reads as broken.
  // So the tab that sent the link listens, and the tab the email opened
  // offers it the hand-off before using it itself.
  //
  // THE ENTER URL IS SINGLE-USE (sessions.Handoffs.redeem pops the code), so
  // exactly one tab may navigate to it. Three messages make that so:
  //
  //   email tab     {type: "enter", enter, from}   "who wants this?"
  //   asking tab    {type: "taken", to, from}      "I do" -- and it waits
  //   email tab     {type: "go", to}               to the FIRST taker only
  //
  // A second asking tab (the person pressed send in two tabs) also answers
  // "taken", is not chosen, and stays where it is. Nobody answers within
  // HANDOFF_WAIT_MS -- no other tab, another browser, no BroadcastChannel --
  // and the email tab signs itself in exactly as it did before.
  //
  // SAME ORIGIN ONLY, by construction: a BroadcastChannel reaches the tabs of
  // this origin in this browser profile and nothing else. Even so, a message
  // is data and not an instruction, so the URL is checked by enterIsOurs
  // before any tab will navigate to it.
  var CHANNEL = "waku-sign-in";
  var HANDOFF_WAIT_MS = 400;
  // hosted/core/tenant.py's alphabet and length, and sessions.new_secret's
  // 43 URL-safe characters. Kept exact so a URL this gateway did not build
  // cannot pass.
  var TENANT_ID = /^[a-z2-7]{12}$/;
  var ENTER_QUERY = /^\?code=[A-Za-z0-9_-]{43}$/;

  function channel() {
    if (typeof BroadcastChannel !== "function") { return null; }
    try { return new BroadcastChannel(CHANNEL); }
    catch (e) { return null; }
  }

  function tabId() {
    return Math.random().toString(36).slice(2) + Date.now().toString(36);
  }

  // `https://<tenant id>.<this host>/auth/enter?code=<code>` and nothing
  // else: the one shape app.py's _sign_in writes. No port, no user info, no
  // fragment, no second query parameter.
  function enterIsOurs(raw) {
    var target;
    try { target = new URL(String(raw)); }
    catch (e) { return false; }
    var suffix = "." + location.hostname;
    var host = target.hostname;
    return target.protocol === "https:" &&
      target.username === "" && target.password === "" &&
      target.port === "" && target.hash === "" &&
      host.length > suffix.length &&
      host.slice(-suffix.length) === suffix &&
      TENANT_ID.test(host.slice(0, -suffix.length)) &&
      target.pathname === "/auth/enter" &&
      ENTER_QUERY.test(target.search);
  }

  // The email tab's half. Resolves true when another tab took the hand-off,
  // false when this tab should use it itself.
  function handedOver(enter) {
    var bus = channel();
    if (!bus) { return Promise.resolve(false); }
    var me = tabId();
    return new Promise(function (resolve) {
      var done = false;
      function finish(taken) {
        if (done) { return; }
        done = true;
        clearTimeout(timer);
        bus.close();
        resolve(taken);
      }
      bus.onmessage = function (event) {
        var message = event.data || {};
        if (message.type !== "taken" || message.to !== me || !message.from) { return; }
        // First taker wins. finish() makes every later answer a no-op, so
        // a second asking tab is never told to go.
        bus.postMessage({type: "go", to: message.from});
        finish(true);
      };
      var timer = setTimeout(function () { finish(false); }, HANDOFF_WAIT_MS);
      bus.postMessage({type: "enter", enter: enter, from: me});
    });
  }

  // The asking tab's half: started once a link has been sent.
  var listening = null;
  function listen() {
    if (listening) { return; }
    listening = channel();
    if (!listening) { return; }
    var me = tabId();
    var offered = null;
    listening.onmessage = function (event) {
      var message = event.data || {};
      if (message.type === "enter" && !offered && message.from &&
          enterIsOurs(message.enter)) {
        offered = message.enter;
        listening.postMessage({type: "taken", to: message.from, from: me});
        // Not chosen (another tab answered first, or the email tab gave up
        // waiting): forget the offer, so the next link can still land here.
        setTimeout(function () { offered = null; }, 2 * HANDOFF_WAIT_MS);
      } else if (message.type === "go" && message.to === me && offered) {
        // The URL this tab checked when it was offered -- never one carried
        // by the "go" message itself.
        listening.close();
        say("Signing you in...");
        location.assign(offered);
      }
    };
  }

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    button.disabled = true;
    say("Sending...");
    var email = document.getElementById("email").value.trim();
    var failed;
    try {
      failed = await sendLink(email);
    } catch (e) {
      // fetch rejects on a dropped connection, and on a CSP refusal. Neither
      // is a signed-out user's fault and neither should leave the page
      // reading "Sending..." forever.
      failed = "Could not reach the sign-in service. Check your connection.";
    }
    button.disabled = false;
    say(failed || "Check your email for the link.", Boolean(failed));
    if (!failed) { listen(); }
  });

  var hash = new URLSearchParams(location.hash.replace(/^#/, ""));
  var token = hash.get("access_token");
  if (token) { exchange(token); }
  else if (hash.get("error_description")) { say(hash.get("error_description"), true); }
})();
