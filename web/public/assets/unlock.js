// Served on the 403 page of a load of `/` without a token (for example a reload after the
// token was stripped from the address bar). It re-enters with the token this tab kept in
// sessionStorage, once; a rejected token is dropped so this never loops.
(function () {
  var KEY = "pr-console-token";
  var params = new URLSearchParams(location.search);
  var stored = null;
  try { stored = sessionStorage.getItem(KEY); } catch (e) { stored = null; }
  if (stored && !params.has("token")) {
    location.replace("/?token=" + encodeURIComponent(stored));
    return;
  }
  if (params.has("token")) {
    try { sessionStorage.removeItem(KEY); } catch (e) { /* storage blocked */ }
  }
  var msg = document.getElementById("msg");
  if (msg) {
    msg.textContent = params.has("token")
      ? "The token was refused (the server may have restarted). Open the URL that server.py printed."
      : "Missing token. Open the URL that server.py printed.";
  }
})();
