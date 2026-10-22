// SPDX-License-Identifier: AGPL-3.0-or-later
// Dinnerbell's only script besides HTMX. Loaded from /static, so the CSP needs no inline code.
//
// HTMX swaps in successful responses only. When a request fails outright (an expired session,
// a server error, or no network), show a banner so a tap that didn't save never looks like it did.
(function () {
  "use strict";

  function showSaveError() {
    var banner = document.getElementById("save-error");
    if (!banner) {
      return;
    }
    banner.textContent = "Not saved. Reload the page.";
    banner.hidden = false;
  }

  document.addEventListener("htmx:responseError", showSaveError);
  document.addEventListener("htmx:sendError", showSaveError);
})();
