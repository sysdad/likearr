// likearr web UI (#129): a visible banner for any hx-post that fails - the service is down, a
// proxy returns 5xx, or the request never reaches the server at all. Without this, the control
// (a radio, a select) shows the value the person just picked while nothing was actually saved,
// and the only way to notice is that the page around it never changed.
//
// One handler covers every hx-post: htmx's own response handling (see the htmx-config meta tag
// in base.html) never swaps a 4xx/5xx response, and dispatches htmx:responseError for one and
// htmx:sendError when the request could not be sent at all (the service is down, a network
// drop). A 409 (stale tab) and a 401 (session expired, HX-Redirect to /login) are configured or
// handled separately and never reach this file - see base.html and web/auth.py.
(function () {
  "use strict";

  var BANNER_ID = "hx-error-banner";
  var MAX_BODY_CHARS = 200; // a proxy's 4xx can be a whole HTML page

  function messageFor(event) {
    var xhr = event.detail && event.detail.xhr;
    var status = xhr ? xhr.status : 0;
    var config = event.detail && event.detail.requestConfig;
    var verb = config && config.verb ? String(config.verb).toUpperCase() : "";
    // A GET (a job page's poll, say, during a redeploy) saved nothing, so it must not say it did.
    var text = verb === "GET"
      ? "likearr could not be reached; this page may be out of date - reload it."
      : "That change was not saved - reload the page.";
    if (!status) {
      return verb === "GET" ? text : text + " The server could not be reached.";
    }
    text += " (" + status + ")";
    if (status >= 400 && status < 500) {
      var body = (xhr.responseText || "").trim().slice(0, MAX_BODY_CHARS);
      if (body) {
        text += ": " + body;
      }
    }
    return text;
  }

  function showBanner(event) {
    var banner = document.getElementById(BANNER_ID);
    if (!banner) {
      banner = document.createElement("div");
      banner.id = BANNER_ID;
      banner.className = "hx-error-banner";
      banner.setAttribute("role", "alert");
      document.body.insertBefore(banner, document.body.firstChild);
    }
    // A second failure replaces the banner's text rather than stacking a new one.
    banner.textContent = messageFor(event);
  }

  document.body.addEventListener("htmx:responseError", showBanner);
  document.body.addEventListener("htmx:sendError", showBanner);
})();
