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
      // location.assign and not a redirect the fetch would follow invisibly.
      location.assign(body.enter);
      return;
    }
    say(body.error || "That did not work. Ask for a new link.", true);
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
  });

  var hash = new URLSearchParams(location.hash.replace(/^#/, ""));
  var token = hash.get("access_token");
  if (token) { exchange(token); }
  else if (hash.get("error_description")) { say(hash.get("error_description"), true); }
})();
