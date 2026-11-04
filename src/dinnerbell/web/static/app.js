// SPDX-License-Identifier: AGPL-3.0-or-later
// Dinnerbell's only script besides HTMX. Loaded from /static, so the CSP needs no inline code.
//
// HTMX swaps in successful responses only. When a request fails outright (no network, a server
// error, an expired session, or no answer in time), show a banner so a tap that didn't save never
// looks like it did. A preference row that failed goes back to the level it last saved. The
// banner stays until each card or row that failed saves, or that card refreshes. A tap replaced
// by a second tap on another control (hx-sync) is aborted, which HTMX reports as
// htmx:sendAbort, not a failure, so it raises nothing.
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
      // last saved, which the page rendered as checked. The row posts to /preferences/stance, or
      // to /admin/members/{id}/preferences/stance when an admin edits someone else's (#181).
      if (elt instanceof HTMLFormElement && elt.matches("form[hx-post$='/preferences/stance']")) {
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
    if (!xhr || xhr.status < 200 || xhr.status >= 300) {
      return;
    }
    var target = event.detail.target;
    // A card's refresh shows it as the server has it, so a failed tap on it no longer misleads.
    if (!isSave(event) && !(target instanceof Element && target.hasAttribute("data-poll"))) {
      return;
    }
    delete unsaved[targetOf(event)];
    if (Object.keys(unsaved).length === 0) {
      setBanner("");
    }
  });

  // If HTMX's own swap fails, it never clears htmx-request, and the repeat guard below and the
  // refresh would skip that form or card until a reload.
  document.addEventListener("htmx:onLoadError", function (event) {
    var elt = event.detail && event.detail.elt;
    if (elt instanceof Element) {
      elt.classList.remove("htmx-request");
    }
    var target = event.detail && event.detail.target;
    if (target instanceof Element) {
      target.removeAttribute("aria-busy");
    }
  });

  // While a save is on its way, the card or row it will replace is busy, so a screen reader waits
  // for the new one. app.css dims the control after 150 ms. A card's refresh doesn't count.
  // HTMX ends every request it sends with htmx:afterRequest: a response, an error, a timeout, or
  // an abort when a second tap replaces it.
  document.addEventListener("htmx:beforeSend", function (event) {
    var target = event.detail && event.detail.target;
    if (isSave(event) && target instanceof Element) {
      target.setAttribute("aria-busy", "true");
    }
  });
  document.addEventListener("htmx:afterRequest", function (event) {
    var target = event.detail && event.detail.target;
    if (target instanceof Element) {
      target.removeAttribute("aria-busy");
    }
  });

  // iOS Safari shows :active, the pressed style in app.css, only on a page that listens for touches.
  document.addEventListener("touchstart", function () {}, { passive: true });

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

  // A full-page form, such as feedback or a setting, posts and loads a new page. Until it does,
  // mark the form as submitting (app.css dims its buttons after 150 ms) and ignore a second
  // submit, so a double tap posts once. Forms HTMX sends, forms the browser's validation stops
  // (they never fire submit), and forms the size check above stops are left alone. A GET, such as
  // the Catalog search, is safe to repeat. The page may never leave: the load can be stopped, so
  // the mark lifts after 10 seconds. A click or Enter with Ctrl, Cmd, or Shift held can open the
  // result in another tab or window while this page stays, so it isn't marked at all.
  var SUBMITTING = "submitting";
  var RELEASE_MS = 10000;
  var releases = new WeakMap();
  var elsewhere = false;

  function isFullPagePost(form, submitter) {
    var method = (submitter && submitter.getAttribute("formmethod")) || form.getAttribute("method") || "get";
    var target = (submitter && submitter.getAttribute("formtarget")) || form.getAttribute("target") || "_self";
    return method.toLowerCase() === "post" && target === "_self";
  }

  function unmark(form) {
    window.clearTimeout(releases.get(form));
    releases.delete(form);
    form.classList.remove(SUBMITTING);
    form.removeAttribute("aria-busy");
  }

  // A click or Enter submits in the same task, so note its modifier keys until that task ends.
  function noteModifiers(event) {
    elsewhere = event.ctrlKey || event.metaKey || event.shiftKey;
    window.setTimeout(function () {
      elsewhere = false;
    }, 0);
  }
  document.addEventListener(
    "click",
    function (event) {
      if (event.target instanceof Element && event.target.closest("button, input[type=submit], input[type=image]")) {
        noteModifiers(event);
      }
    },
    true
  );
  document.addEventListener(
    "keydown",
    function (event) {
      if (event.key === "Enter") {
        noteModifiers(event);
      }
    },
    true
  );

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) {
      return;
    }
    var inputs = form.querySelectorAll("input[type=file][data-max-bytes]");
    for (var i = 0; i < inputs.length; i += 1) {
      if (!fileFits(inputs[i])) {
        event.preventDefault();
      }
    }
    if (event.defaultPrevented || !isFullPagePost(form, event.submitter)) {
      return; // HTMX cancels the submit of each form it sends.
    }
    if (form.classList.contains(SUBMITTING)) {
      event.preventDefault();
      return;
    }
    if (elsewhere) {
      return;
    }
    form.classList.add(SUBMITTING);
    form.setAttribute("aria-busy", "true");
    releases.set(
      form,
      window.setTimeout(function () {
        unmark(form);
      }, RELEASE_MS)
    );
  });

  // Back and Forward can show this page again from the browser's cache, as it was when the form
  // left: clear the mark, so the form works again.
  window.addEventListener("pageshow", function () {
    var forms = document.querySelectorAll("form." + SUBMITTING);
    for (var i = 0; i < forms.length; i += 1) {
      unmark(forms[i]);
    }
  });

  // Calls still in progress (scheduled, collecting, voting, or decided) carry data-poll, the URL
  // of their card. Refresh them now and then, so a page left open shows the call opening,
  // voting, the decision, and Mark done at meal time. Skip a card while someone is using it: an
  // open form, typing in a field, a field changed and not saved yet in a form that's showing, or
  // a request in flight. Those last two don't rely on focus, which Safari doesn't give a tapped
  // checkbox or button. A focused button, checkbox, or radio doesn't count: Chrome focuses a
  // tapped one, and HTMX keeps that focus across the swap. A save swaps in a new card, whose
  // fields start unchanged; a closed form's changes are dropped. Skip hidden tabs.
  //
  // The card is the poll's source, so it shares the card's hx-sync: a tap replaces a poll in
  // flight, and its answer can't overwrite the tap's. A poll never starts while a save is in
  // flight, since that would abort the save.
  var POLL_MS = 45000;
  var TYPING = "textarea, select, input:not([type=hidden]):not([type=radio]):not([type=checkbox])";

  // The option a drop-down select shows until someone picks another: the last one marked
  // selected, or else the first one that isn't disabled. This assumes a drop-down: a list box
  // (size > 1) with none marked shows none, and cards have no list boxes.
  function defaultIndex(select) {
    var first = -1;
    for (var i = select.options.length - 1; i >= 0; i -= 1) {
      if (select.options[i].defaultSelected) {
        return i;
      }
      if (!select.options[i].disabled) {
        first = i;
      }
    }
    return first;
  }

  function changed(field) {
    if (field.type === "checkbox" || field.type === "radio") {
      return field.checked !== field.defaultChecked;
    }
    if (field instanceof HTMLSelectElement) {
      if (!field.multiple) {
        return field.selectedIndex !== defaultIndex(field);
      }
      for (var i = 0; i < field.options.length; i += 1) {
        if (field.options[i].selected !== field.options[i].defaultSelected) {
          return true;
        }
      }
      return false;
    }
    // A number or time field with something unfinished in it reads as "".
    return field.value !== field.defaultValue || Boolean(field.validity && field.validity.badInput);
  }

  function unsavedIn(card) {
    var fields = card.querySelectorAll("textarea, select, input:not([type=hidden])");
    for (var i = 0; i < fields.length; i += 1) {
      if (!fields[i].closest("details:not([open])") && changed(fields[i])) {
        return true;
      }
    }
    return false;
  }

  window.setInterval(function () {
    if (document.hidden || !window.htmx) {
      return;
    }
    var cards = document.querySelectorAll("[data-poll]");
    for (var i = 0; i < cards.length; i += 1) {
      var card = cards[i];
      var busy = card.classList.contains("htmx-request") || card.querySelector(".htmx-request");
      var typing = card.contains(document.activeElement) && document.activeElement.matches(TYPING);
      if (busy || typing || card.querySelector("details[open]") || unsavedIn(card)) {
        continue;
      }
      window.htmx.ajax("GET", card.getAttribute("data-poll"), { source: card, target: card, swap: "outerHTML" });
    }
  }, POLL_MS);
})();
