// SPDX-License-Identifier: AGPL-3.0-or-later
// Dinnerbell's only script besides HTMX. Loaded from /static, so the CSP needs no inline code.
//
// HTMX swaps in successful responses only. When a request fails outright (no network, a server
// error, an expired session, or no answer in time), show a banner so a tap that didn't save never
// looks like it did. The next save that works clears it. A tap replaced by a second tap (hx-sync)
// is aborted, which HTMX reports as htmx:sendAbort, not a failure, so it raises nothing.
(function () {
  "use strict";

  var OFFLINE = "Not saved. Check your connection and try again.";
  var SERVER = "Not saved. Something went wrong. Try again.";
  var RELOAD = "Not saved. Reload the page.";
  var SLOW = "This might not have saved: the server took too long. Reload the page to check.";

  function isSave(event) {
    var config = event && event.detail && event.detail.requestConfig;
    // A failed GET is a background refresh or a search; nothing the person did went unsaved.
    return !(config && config.verb === "get");
  }

  function setBanner(text) {
    var banner = document.getElementById("save-error");
    if (!banner) {
      return;
    }
    banner.textContent = text;
    banner.hidden = !text;
  }

  function failed(text) {
    return function (event) {
      if (isSave(event)) {
        setBanner(text);
      }
    };
  }

  document.addEventListener("htmx:sendError", failed(OFFLINE));
  document.addEventListener("htmx:timeout", failed(SLOW));
  document.addEventListener("htmx:responseError", function (event) {
    var status = event.detail && event.detail.xhr ? event.detail.xhr.status : 0;
    // 401 and 403 mean an expired session or a stale CSRF token, and other 4xx a page that's out
    // of date, so reloading is the fix. A 5xx might work the second time.
    failed(status >= 500 ? SERVER : RELOAD)(event);
  });
  document.addEventListener("htmx:afterRequest", function (event) {
    var xhr = event.detail && event.detail.xhr;
    if (isSave(event) && xhr && xhr.status >= 200 && xhr.status < 300) {
      setBanner("");
    }
  });

  // An action that a card refuses (a 409) shows its error at the top of the card, which can be
  // above the screen on a tall card. Bring it into view once the new card settles.
  document.addEventListener("htmx:afterSettle", function (event) {
    var swapped = event.target;
    if (!isSave(event) || !(swapped instanceof Element)) {
      return;
    }
    var shown = "[role=alert]:not([hidden])";
    var alert = swapped.matches(shown) ? swapped : swapped.querySelector(shown);
    if (!alert) {
      return;
    }
    var still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    alert.scrollIntoView({ block: "nearest", behavior: still ? "instant" : "smooth" });
  });

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
