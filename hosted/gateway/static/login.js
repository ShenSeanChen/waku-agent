// The sign-in page. It holds an access token for the length of one fetch and
// writes it nowhere.
//
// flowType "implicit" and persistSession false are the spec's, and they are
// what make GET /login's Clear-Site-Data safe to send: there is no PKCE
// verifier that has to survive it, and no Supabase token is left in this
// origin's storage for the next person at this browser.
//
// The fragment is parsed here rather than by the library. detectSessionInUrl
// is off, so nothing in the library writes a session anywhere, and this page
// needs exactly one string out of the URL.
(function () {
  "use strict";
  var url = document.body.dataset.supabaseUrl;
  var key = document.body.dataset.supabaseKey;
  var client = supabase.createClient(url, key, {
    auth: {
      flowType: "implicit",
      persistSession: false,
      autoRefreshToken: false,
      detectSessionInUrl: false
    }
  });
  var status = document.getElementById("status");
  var form = document.getElementById("form");
  var button = document.getElementById("send");

  function say(text) { status.textContent = text; }

  function zone() {
    try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; }
    catch (e) { return "UTC"; }
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
    say(body.error || "That did not work. Ask for a new link.");
  }

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    button.disabled = true;
    say("Sending...");
    var email = document.getElementById("email").value.trim();
    var sent = await client.auth.signInWithOtp({
      email: email,
      options: {emailRedirectTo: location.origin + "/login"}
    });
    button.disabled = false;
    say(sent.error ? sent.error.message : "Check your email for the link.");
  });

  var hash = new URLSearchParams(location.hash.replace(/^#/, ""));
  var token = hash.get("access_token");
  if (token) { exchange(token); }
  else if (hash.get("error_description")) { say(hash.get("error_description")); }
})();
