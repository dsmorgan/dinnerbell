// SPDX-License-Identifier: AGPL-3.0-or-later
// Dinnerbell's only script besides HTMX. Loaded from /static, so the CSP needs no inline code.
//
// HTMX swaps in successful responses only. When a request fails outright (no network, a server
// error, an expired session, or no answer in time), show a banner so a tap that didn't save never
// looks like it did. A preference row that failed goes back to the level it last saved. The
// banner stays until each card or row that failed saves. A tap replaced by a second tap on
// another control (hx-sync) is aborted, which HTMX reports as htmx:sendAbort, not a failure, so
// it raises nothing.
(function () {
  "use strict";

  var OFFLINE = "Not saved. Check your connection and try again.";
  var SERVER = "Not saved. Something went wrong. Try again.";
  var RELOAD = "Not saved. Reload the page.";
  var SLOW = "This might not have saved: the server took too long. Reload the page to check.";

  // The ids of the cards and rows whose last save failed.
  var unsaved = {};

  function isSave(event) {
    var config = event && event.detail && event.detail.requestConfig;
    // A failed GET is a background refresh or a search; nothing the person did went unsaved.
    return !(config && config.verb === "get");
  }

  function targetOf(event) {
    var target = event.detail && event.detail.target;
    return (target && target.id) || "page";
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
      if (!isSave(event)) {
        return;
      }
      unsaved[targetOf(event)] = true;
      var elt = event.detail && event.detail.elt;
      // A preference row shows the level tapped as soon as it's tapped. Put back the one it
      // last saved, which the page rendered as checked.
      if (elt instanceof HTMLFormElement && elt.matches("form[hx-post='/preferences/stance']")) {
        elt.reset();
      }
      setBanner(text);
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
    if (!isSave(event) || !xhr || xhr.status < 200 || xhr.status >= 300) {
      return;
    }
    delete unsaved[targetOf(event)];
    if (Object.keys(unsaved).length === 0) {
      setBanner("");
    }
  });

  // A double tap on one of a card's controls would send the same thing twice: the card's
  // hx-sync aborts the first in the browser, but the server has usually received it already.
  // So while a control's request is in flight (HTMX marks its form with htmx-request), drop a
  // repeat. A tap on a different control still replaces it.
  document.addEventListener("htmx:confirm", function (event) {
    var elt = event.detail && event.detail.elt;
    if (elt instanceof Element && elt.closest(".event") && elt.classList.contains("htmx-request")) {
      event.preventDefault();
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
  // open form, typing in a field, or a request in flight (Safari doesn't focus a tapped button).
  // A focused button doesn't count: Chrome focuses a tapped one, and HTMX keeps that focus
  // across the swap. Skip hidden tabs.
  //
  // The card is the poll's source, so it shares the card's hx-sync: a tap replaces a poll in
  // flight, and its answer can't overwrite the tap's. A poll never starts while a save is in
  // flight, since that would abort the save.
  var POLL_MS = 45000;
  var TYPING = "textarea, select, input:not([type=hidden]):not([type=radio]):not([type=checkbox])";
  window.setInterval(function () {
    if (document.hidden || !window.htmx) {
      return;
    }
    var cards = document.querySelectorAll("[data-poll]");
    for (var i = 0; i < cards.length; i += 1) {
      var card = cards[i];
      var busy = card.classList.contains("htmx-request") || card.querySelector(".htmx-request");
      var typing = card.contains(document.activeElement) && document.activeElement.matches(TYPING);
      if (busy || typing || card.querySelector("details[open]")) {
        continue;
      }
      window.htmx.ajax("GET", card.getAttribute("data-poll"), { source: card, target: card, swap: "outerHTML" });
    }
  }, POLL_MS);
})();
