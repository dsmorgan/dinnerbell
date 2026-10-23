// SPDX-License-Identifier: AGPL-3.0-or-later
// Dinnerbell's only script besides HTMX. Loaded from /static, so the CSP needs no inline code.
//
// HTMX swaps in successful responses only. When a request fails outright (an expired session,
// a server error, or no network), show a banner so a tap that didn't save never looks like it did.
(function () {
  "use strict";

  function showSaveError(event) {
    var config = event && event.detail && event.detail.requestConfig;
    if (config && config.verb === "get") {
      return; // a background refresh failed; nothing the person did went unsaved
    }
    var banner = document.getElementById("save-error");
    if (!banner) {
      return;
    }
    banner.textContent = "Not saved. Reload the page.";
    banner.hidden = false;
  }

  document.addEventListener("htmx:responseError", showSaveError);
  document.addEventListener("htmx:sendError", showSaveError);

  // A file over the server's limit usually ends in a reset connection rather than a readable
  // 413, so check the size before sending. Inputs opt in with data-max-bytes and name the
  // element for the message with data-size-error.
  function fileFits(input) {
    var limit = Number(input.getAttribute("data-max-bytes"));
    var note = document.getElementById(input.getAttribute("data-size-error") || "");
    var file = input.files && input.files[0];
    var tooBig = Boolean(file && limit && file.size > limit);
    if (note) {
      note.textContent = tooBig ? "Catalog files can be up to 1 MB." : "";
      note.hidden = !tooBig;
    }
    if (tooBig) {
      input.value = "";
    }
    return !tooBig;
  }

  document.addEventListener("change", function (event) {
    var input = event.target;
    if (input instanceof HTMLInputElement && input.type === "file" && input.hasAttribute("data-max-bytes")) {
      fileFits(input);
    }
  });

  document.addEventListener("submit", function (event) {
    var inputs = event.target.querySelectorAll("input[type=file][data-max-bytes]");
    for (var i = 0; i < inputs.length; i += 1) {
      if (!fileFits(inputs[i])) {
        event.preventDefault();
      }
    }
  });

  // Calls still in progress (scheduled, collecting, voting, or decided) carry data-poll, the URL
  // of their card. Refresh them now and then, so a page left open shows the call opening,
  // voting, the decision, and Mark done at meal time. Skip a card while someone is using it: an
  // open form, focus inside, or a request in flight (Safari doesn't focus a tapped button).
  // Skip hidden tabs.
  var POLL_MS = 45000;
  window.setInterval(function () {
    if (document.hidden || !window.htmx) {
      return;
    }
    var cards = document.querySelectorAll("[data-poll]");
    for (var i = 0; i < cards.length; i += 1) {
      var card = cards[i];
      var busy = card.classList.contains("htmx-request") || card.querySelector(".htmx-request");
      if (busy || card.querySelector("details[open]") || card.contains(document.activeElement)) {
        continue;
      }
      window.htmx.ajax("GET", card.getAttribute("data-poll"), { target: card, swap: "outerHTML" });
    }
  }, POLL_MS);
})();
