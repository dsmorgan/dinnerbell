// SPDX-License-Identifier: AGPL-3.0-or-later
// Dinnerbell's only script besides HTMX. Loaded from /static, so the CSP needs no inline code.
//
// HTMX swaps in successful responses only. When a request fails outright (no network, a server
// error, an expired session, or no answer in time), show a banner so a tap that didn't save never
// looks like it did. A preference row that failed goes back to the level it last saved. The
// banner stays until each card or row that failed saves, gets a 409 (which swaps in the card as
// the server has it, with the reason it refused), or refreshes. A tap replaced
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
    // HTMX swaps in a 409 too (responseHandling in base.html): the card as the server has it, with
    // its own error at the top. That settles an earlier failed tap on it as a 2xx does.
    if (!xhr || ((xhr.status < 200 || xhr.status >= 300) && xhr.status !== 409)) {
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

  // A form marked data-reset-on-success, such as the Assistant page's message box, empties its
  // text once HTMX has its answer (a 2xx), and keeps it when the request failed, so nothing typed
  // is lost. The box stays editable while the reply is on its way, which can take a while, so it
  // empties only if it still holds what was sent: a follow-up typed meanwhile stays, and keeps
  // its focus.
  var SENT = "data-sent";
  var TEXT_FIELD = "textarea, input:not([type=hidden]):not([type=checkbox]):not([type=radio])";

  function resettable(event) {
    var elt = event.detail && event.detail.elt;
    return elt instanceof HTMLFormElement && elt.hasAttribute("data-reset-on-success") ? elt : null;
  }

  document.addEventListener("htmx:beforeRequest", function (event) {
    var form = resettable(event);
    var field = form && form.querySelector(TEXT_FIELD);
    if (field) {
      field.setAttribute(SENT, field.value);
    }
  });
  document.addEventListener("htmx:afterRequest", function (event) {
    var form = resettable(event);
    var field = form && form.querySelector(TEXT_FIELD);
    if (!field) {
      return;
    }
    var sent = field.getAttribute(SENT);
    field.removeAttribute(SENT);
    var xhr = event.detail.xhr;
    if (!xhr || xhr.status < 200 || xhr.status >= 300 || field.value !== sent) {
      return;
    }
    field.value = "";
    field.focus();
  });

  // The Assistant page's reply as the model writes it (#370). A form marked data-stream-timeout
  // posts with fetch instead of HTMX, asking for text/event-stream, where the browser can read a
  // response as it arrives; elsewhere HTMX sends it as before, and without script the browser
  // does. The server sends events (web/assistant_stream.py): start (the message and a hidden
  // placeholder for the reply), text (the reply so far), discard (that text wasn't the reply),
  // done (the reply and its cards, which replace the placeholder), and error. Lines starting
  // with ":" only keep the connection open. The placeholder is aria-hidden, so the log announces
  // the finished reply once. The thinking indicator shows until text does, and again after a
  // discard. As with HTMX, a failure removes the exchange, keeps the text in the box, and shows
  // the banner; the box empties only once the reply is done, if it still holds what was sent.
  var STREAM_TIMEOUT = "data-stream-timeout";
  var canStream = Boolean(
    window.htmx &&
      window.fetch &&
      window.AbortController &&
      window.TextDecoder &&
      window.URLSearchParams &&
      window.Response &&
      "body" in window.Response.prototype
  );

  function parseEvent(block) {
    var name = "message";
    var data = "";
    var lines = block.split("\n");
    for (var i = 0; i < lines.length; i += 1) {
      if (lines[i].indexOf("event:") === 0) {
        name = lines[i].slice(6).trim();
      } else if (lines[i].indexOf("data:") === 0) {
        data += lines[i].slice(5);
      }
    }
    if (!data) {
      return null;
    }
    try {
      return { name: name, data: JSON.parse(data) };
    } catch (e) {
      return null;
    }
  }

  // Swap in the server's own rendered templates through HTMX, as its responses are: the same
  // parser, no script tags, and hx- attributes processed, so a card's Confirm and Cancel work.
  function put(target, html, style) {
    window.htmx.swap(target, html, { swapStyle: style, swapDelay: 0, settleDelay: 0, ignoreTitle: true });
  }

  function streamSend(form) {
    var log = document.querySelector(form.getAttribute("hx-target") || "#assistant-log");
    var field = form.querySelector(TEXT_FIELD);
    var sent = field ? field.value : "";
    var button = form.querySelector("button[type=submit]");
    var controller = new AbortController();
    var timedOut = false;
    var timer = window.setTimeout(function () {
      timedOut = true;
      controller.abort();
    }, Number(form.getAttribute(STREAM_TIMEOUT)) || 120000);
    var replace = false;
    var reply = null;
    var settled = false;

    form.classList.add("streaming");
    if (button) {
      button.disabled = true;
    }
    if (log) {
      log.setAttribute("aria-busy", "true");
    }

    function end() {
      settled = true;
      window.clearTimeout(timer);
      form.classList.remove("streaming", "writing");
      if (button) {
        button.disabled = false;
      }
      if (log) {
        log.removeAttribute("aria-busy");
      }
    }

    function fail(text) {
      if (settled) {
        return;
      }
      end();
      // The exchange it added: the member's message and the placeholder after it.
      if (reply) {
        var mine = reply.previousElementSibling;
        if (mine && mine.classList.contains("turn-mine")) {
          mine.remove();
        }
        reply.remove();
      }
      unsaved[(log && log.id) || "page"] = true;
      setBanner(text);
    }

    function succeed() {
      if (settled) {
        return;
      }
      end();
      delete unsaved[(log && log.id) || "page"];
      if (Object.keys(unsaved).length === 0) {
        setBanner("");
      }
      if (field && field.value === sent) {
        field.value = "";
        field.focus();
      }
    }

    function started(data) {
      replace = Boolean(data.replace);
      if (replace) {
        // Conversation hours 0: the exchange replaces the turns shown; waiting cards stay until
        // done brings them back.
        var old = Array.prototype.slice.call(log.children);
        for (var i = 0; i < old.length; i += 1) {
          if (!old[i].matches("article.draft:not(.draft-done)")) {
            old[i].remove();
          }
        }
        put(log, data.html, "afterbegin");
      } else {
        put(log, data.html, "beforeend");
      }
      var placeholders = log.querySelectorAll("[data-stream-reply]");
      reply = placeholders[placeholders.length - 1] || null;
    }

    function shown(html) {
      var text = reply && reply.querySelector("[data-stream-text]");
      if (!text) {
        return;
      }
      text.textContent = "";
      put(text, html, "beforeend");
      reply.hidden = !html;
      form.classList.toggle("writing", Boolean(html));
    }

    function done(data) {
      if (replace) {
        var cards = log.querySelectorAll("article.draft");
        for (var i = 0; i < cards.length; i += 1) {
          cards[i].remove();
        }
      }
      if (reply && reply.isConnected) {
        put(reply, data.html, "outerHTML");
      } else {
        put(log, data.html, "beforeend");
      }
      reply = null;
      succeed();
    }

    function handle(item) {
      if (!item || settled) {
        return;
      }
      if (item.name === "start") {
        started(item.data);
      } else if (item.name === "text") {
        shown(item.data.html);
      } else if (item.name === "discard") {
        shown("");
      } else if (item.name === "done") {
        done(item.data);
      } else if (item.name === "error") {
        fail(item.data.status >= 500 ? SERVER : RELOAD);
      }
    }

    function read(reader, decoder, buffer) {
      return reader.read().then(function (chunk) {
        buffer += decoder.decode(chunk.value || new Uint8Array(), { stream: !chunk.done });
        var cut = buffer.indexOf("\n\n");
        while (cut >= 0) {
          handle(parseEvent(buffer.slice(0, cut)));
          buffer = buffer.slice(cut + 2);
          cut = buffer.indexOf("\n\n");
        }
        if (chunk.done) {
          // The connection ended before the reply did: it may still be stored.
          fail(SLOW);
          return null;
        }
        return read(reader, decoder, buffer);
      });
    }

    window
      .fetch(form.getAttribute("hx-post") || form.action, {
        method: "POST",
        body: new URLSearchParams(new FormData(form)),
        headers: { Accept: "text/event-stream" },
        credentials: "same-origin",
        redirect: "manual",
        signal: controller.signal,
      })
      .then(function (response) {
        var type = response.headers.get("Content-Type") || "";
        if (response.status === 204) {
          succeed();
          return null;
        }
        // A redirect (a session that ended) and any refusal: reload, as with HTMX. A 5xx might
        // work the second time.
        if (!response.ok || type.indexOf("text/event-stream") !== 0 || !response.body) {
          fail(response.status >= 500 ? SERVER : RELOAD);
          return null;
        }
        return read(response.body.getReader(), new TextDecoder(), "");
      })
      .catch(function () {
        fail(timedOut || reply ? SLOW : OFFLINE);
      });
  }

  document.addEventListener("htmx:confirm", function (event) {
    var form = event.detail && event.detail.elt;
    if (!canStream || !(form instanceof HTMLFormElement) || !form.hasAttribute(STREAM_TIMEOUT)) {
      return;
    }
    event.preventDefault(); // HTMX doesn't send it
    if (!form.classList.contains("streaming")) {
      streamSend(form);
    }
  });

  // A text box marked data-enter-sends, such as the Assistant page's message box, sends its form
  // on Enter, as its submit button does (requestSubmit, so HTMX and the form's checks apply).
  // Shift+Enter starts a new line, and Enter with Ctrl, Cmd, or Alt does what it always did.
  // Enter does nothing more while the box is empty or the form is sending (hx-disabled-elt
  // disables the button). Enter that picks an input method's candidate
  // (isComposing, or key code 229 in Safari) stays the input method's. On a touchscreen there's
  // no Shift key to start a line with, so Enter starts one there and the button sends.
  var ENTER_SENDS = "data-enter-sends";
  var touchscreen = window.matchMedia && window.matchMedia("(hover: none) and (pointer: coarse)");

  document.addEventListener("keydown", function (event) {
    var field = event.target;
    if (event.key !== "Enter" || event.shiftKey || event.altKey || event.ctrlKey || event.metaKey) {
      return;
    }
    if (event.isComposing || event.keyCode === 229) {
      return;
    }
    if (!(field instanceof HTMLTextAreaElement) || !field.hasAttribute(ENTER_SENDS) || !field.form) {
      return;
    }
    if (touchscreen && touchscreen.matches) {
      return;
    }
    event.preventDefault();
    var send = field.form.querySelector("button[type=submit]");
    if (!field.value.trim() || (send && send.disabled)) {
      return;
    }
    field.form.requestSubmit(send || undefined);
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
  //
  // A refresh that fails doesn't repeat every 45 seconds forever. A 403 or 404 (a guest no longer
  // invited, a call that's gone) won't change, so that card stops refreshing; a save that swaps
  // in a new card starts it again. No network, no answer in time, or another error slows every
  // card's refresh, twice as long each round up to 10 minutes. Any request that works, or the
  // browser coming back online, goes back to 45 seconds. A 401 needs nothing: its HX-Redirect
  // sends the page to sign in.
  var POLL_MS = 45000;
  var POLL_MAX_MS = 600000;
  var pollDelay = POLL_MS;
  var pollStarted = 0; // when the current round started
  var pollTimer = 0;
  var slowed = false; // whether a failure in the current round already slowed the next
  var answered = false; // whether a refresh in the current round worked

  function isPoll(event) {
    var target = event.detail && event.detail.target;
    return !isSave(event) && target instanceof Element && target.hasAttribute("data-poll");
  }

  // Time the next round from the start of this one, so a failure that comes in late doesn't
  // add its wait on top.
  function nextRound() {
    window.clearTimeout(pollTimer);
    pollTimer = window.setTimeout(pollRound, Math.max(0, pollStarted + pollDelay - Date.now()));
  }

  // Once a round: three cards that can't reach the server wait twice as long, not eight times.
  // A refresh in the same round that worked wins, so one card's error doesn't slow the rest.
  function slowDown() {
    if (slowed || answered) {
      return;
    }
    slowed = true;
    pollDelay = Math.min(pollDelay * 2, POLL_MAX_MS);
    nextRound();
  }

  function pollFailed(event) {
    if (isPoll(event)) {
      slowDown();
    }
  }

  // Back to every 45 seconds, timed from the round's start, so a round overdue at that pace runs
  // now: the cards that waited catch up at once.
  function backToNormal() {
    if (pollDelay !== POLL_MS) {
      pollDelay = POLL_MS;
      nextRound();
    }
  }

  document.addEventListener("htmx:sendError", pollFailed);
  document.addEventListener("htmx:timeout", pollFailed);
  // A poll a tap replaced ends in an abort, also with status 0, so only the error events above
  // count a refresh that got no answer.
  document.addEventListener("htmx:afterRequest", function (event) {
    var status = event.detail && event.detail.xhr ? event.detail.xhr.status : 0;
    // Any request that works, a tap's save as well as a refresh, means the server answers again,
    // so the other cards don't keep waiting up to 10 minutes.
    if (status >= 200 && status < 300) {
      answered = true;
      backToNormal();
      return;
    }
    if (!isPoll(event)) {
      return;
    }
    if (status === 403 || status === 404) {
      event.detail.target.removeAttribute("data-poll");
    } else if (status >= 400 && status !== 401) {
      slowDown();
    }
  });
  // The browser says the network is back: don't wait out a long backoff to find out.
  window.addEventListener("online", backToNormal);

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

  function pollRound() {
    pollStarted = Date.now();
    slowed = false;
    answered = false;
    nextRound();
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
  }

  pollStarted = Date.now();
  nextRound();

  // Copy (a new sign-in code, spec 6.15): a button with data-copy="id" puts that element's text
  // on the clipboard. It stays hidden where the clipboard isn't available.
  function showCopyButtons() {
    if (!navigator.clipboard || !window.isSecureContext) {
      return;
    }
    var buttons = document.querySelectorAll("button[data-copy]");
    for (var i = 0; i < buttons.length; i += 1) {
      buttons[i].hidden = false;
    }
  }
  document.addEventListener("click", function (event) {
    var button = event.target instanceof Element ? event.target.closest("button[data-copy]") : null;
    var source = button && document.getElementById(button.getAttribute("data-copy"));
    if (!source || !navigator.clipboard) {
      return;
    }
    function say(text) {
      button.textContent = text;
      window.setTimeout(function () {
        button.textContent = "Copy";
      }, 3000);
    }
    try {
      navigator.clipboard.writeText(source.textContent.trim()).then(
        function () {
          say("Copied");
        },
        function () {
          say("Couldn't copy");
        }
      );
    } catch (e) {
      say("Couldn't copy");
    }
  });
  showCopyButtons();
  document.addEventListener("htmx:afterSettle", showCopyButtons);
})();
