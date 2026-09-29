// likearr web UI: a form marked data-submit-once disables its buttons once it submits, so a
// double press sends one request.
(function () {
  "use strict";

  var MARK = "data-submit-once-disabled";

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-submit-once")) {
      return;
    }
    // After the submit event: a button disabled during it would drop out of the form data.
    setTimeout(function () {
      form.querySelectorAll("button, input[type=submit]").forEach(function (button) {
        if (!button.disabled) {
          button.disabled = true;
          button.setAttribute(MARK, "");
        }
      });
    }, 0);
  });

  // A page restored by Back keeps the buttons this disabled; enable them again.
  window.addEventListener("pageshow", function (event) {
    if (!event.persisted) {
      return;
    }
    document.querySelectorAll("[" + MARK + "]").forEach(function (button) {
      button.disabled = false;
      button.removeAttribute(MARK);
    });
  });
})();
