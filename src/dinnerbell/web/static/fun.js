// SPDX-License-Identifier: AGPL-3.0-or-later
// Fun mode (#153): Dinnerbone's Den. Loaded only for a viewer who has fun mode on; with it off,
// no page links this file. Loaded from /static, so the CSP needs no inline code.
//
// The server draws the den: the scene on <body>, his pose on .den (data-pose), and what the den
// knows in #den-state (the call he follows, its quiet caption, and a badge to flip for). A card
// swapped into Today brings a fresh #den-state out of band, so this script only follows it:
//
// - It walks him from spot to spot when the pose changes. fun.css places every pose, so a walk
//   animates `left` from where he stands to where the new pose puts him.
// - While answers are open, he runs laps around the card of the call he follows, on a track
//   over the page, sized to it only during a lap. A lap pauses while the page scrolls and gives
//   up when his state changes or the window is resized; the next one measures again.
// - Celebrations: the server sends HX-Trigger "dinnerbell:celebrate" only to the member who
//   locked or marked done the call on Today. Everyone else gets the quiet caption, once per call
//   and state per tab (sessionStorage). A badge flips once, for the member who earned it: the
//   server keeps the marker, and this script tells it after the flip, once the page is in view.
// - Tap him: a hop, puffed cheeks, and a heart. Asleep, he opens an eye and rolls over.
// - Snow never sits on a turning wheel: climbing in, he shakes the wheel's cap off before it
//   turns, and it builds back up once he's off. Still, a wheel he's in has no cap.
//
// With prefers-reduced-motion, nothing moves: poses change in place, there are no laps, and a
// celebration is the still pose with its caption. Captions are text only (textContent).
(function () {
  "use strict";

  var den = document.querySelector(".den");
  if (!den) {
    return; // the den is on Today only
  }
  var ham = den.querySelector(".den-ham");
  var heart = den.querySelector(".den-heart");
  var caption = document.querySelector(".den-caption");
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  var S = {
    walk: null,
    lap: null,
    lapTimer: 0,
    scrollTimer: 0,
    sayTimer: 0,
    gait: null,
    gaitUntil: 0,
    event: null,
    track: null,
    runner: null,
    applied: null, // the last #den-state applied, so the same state arriving again changes nothing
    pending: null, // a walk waiting on a celebration, such as the waddle after the belly pat
  };

  function moving() {
    return !reduce.matches;
  }

  function flash(el, name, ms) {
    el.classList.remove(name);
    void el.offsetWidth; // restart the CSS animation
    el.classList.add(name);
    window.setTimeout(function () {
      el.classList.remove(name);
    }, ms);
  }

  // Captions --------------------------------------------------------------------------------

  function say(text) {
    if (!caption || typeof text !== "string" || !text) {
      return;
    }
    caption.textContent = text;
    // Scrolled away from the den, as after tapping Lock this far down a card, the caption floats
    // at the bottom of the screen instead, so it's seen.
    var box = den.getBoundingClientRect();
    caption.classList.toggle("den-float", box.bottom < 0 || box.top > window.innerHeight);
    caption.hidden = false;
    window.clearTimeout(S.sayTimer);
    S.sayTimer = window.setTimeout(function () {
      caption.hidden = true;
      caption.textContent = "";
    }, 4200);
  }

  // True the first time this tab sees ``key`` (sessionStorage). Without storage, every time.
  function firstTime(key) {
    try {
      if (window.sessionStorage.getItem(key)) {
        return false;
      }
      window.sessionStorage.setItem(key, "1");
    } catch (e) {
      // private browsing or blocked storage: show it
    }
    return true;
  }

  function captionKey(eventId, run, state) {
    return "dinnerbell:den:" + eventId + ":" + run + ":" + state;
  }

  // Walking ---------------------------------------------------------------------------------

  function stopWalk() {
    if (S.walk) {
      S.walk.cancel();
      S.walk = null;
    }
    den.removeAttribute("data-walking");
    den.removeAttribute("data-gait");
  }

  function setPose(next, gait) {
    var current = den.getAttribute("data-pose");
    if (!next) {
      return;
    }
    if (next === current) {
      // Already there, or on the way: htmx settles a swap and its out-of-band state separately,
      // so the same state often arrives twice.
      if (!S.walk) {
        settled(next);
      }
      return;
    }
    stopLaps();
    var from = parseFloat(window.getComputedStyle(ham).left) || 0;
    stopWalk();
    var intoWheel = next === "wheel" && current !== "wheel" && moving();
    if (current === "wheel" && next !== "wheel" && moving()) {
      flash(den, "den-coast", 2700);
      flash(den, "den-rebuild", 8000); // the snow on the wheel builds back up
    }
    den.setAttribute("data-pose", next);
    var to = parseFloat(window.getComputedStyle(ham).left) || 0;
    if (!moving() || Math.abs(to - from) < 2) {
      if (intoWheel) {
        flash(den, "den-shake", 700);
      }
      settled(next);
      return;
    }
    den.setAttribute("data-walking", to < from ? "left" : "right");
    if (gait) {
      den.setAttribute("data-gait", gait);
    }
    var pace = gait === "sprint" ? 4 : gait === "waddle" ? 40 : 14;
    S.walk = ham.animate([{ left: from + "px" }, { left: to + "px" }], {
      duration: Math.max(250, Math.abs(to - from) * pace),
      easing: "ease-in-out",
    });
    S.walk.onfinish = function () {
      S.walk = null;
      den.removeAttribute("data-walking");
      den.removeAttribute("data-gait");
      if (intoWheel) {
        flash(den, "den-shake", 700); // he shakes the snow off before it turns
      }
      settled(next);
    };
  }

  function settled(pose) {
    if (pose === "laps") {
      scheduleLap(1500 + Math.random() * 1500);
    }
  }

  // Laps ------------------------------------------------------------------------------------

  var RUNNER_W = 24;
  var RUNNER_H = 21;
  var LAP_PACE = 4.2; // ms per px

  function lapCard() {
    if (!S.event || !/^\d+$/.test(S.event)) {
      return null;
    }
    var card = document.getElementById("event-" + S.event);
    return card && card.offsetParent ? card : null;
  }

  function scheduleLap(delay) {
    window.clearTimeout(S.lapTimer);
    if (!moving() || den.getAttribute("data-pose") !== "laps" || S.lap) {
      return;
    }
    S.lapTimer = window.setTimeout(runLap, delay);
  }

  function stopLaps() {
    window.clearTimeout(S.lapTimer);
    if (S.lap) {
      S.lap.cancel();
      S.lap = null;
    }
    if (S.runner) {
      S.runner.classList.remove("den-on");
    }
    collapseTrack();
    ham.classList.remove("den-away");
  }

  // The track spans the page only during a lap. Collapsed, it adds nothing to the page's height,
  // so the page shrinks back when its content does.
  function collapseTrack() {
    if (S.track) {
      S.track.style.setProperty("height", "0px");
    }
  }

  function runnerParts() {
    if (!S.track) {
      S.track = document.createElement("div");
      S.track.className = "den-track";
      S.track.setAttribute("aria-hidden", "true");
      S.runner = document.createElement("div");
      S.runner.className = "den-runner";
      S.runner.appendChild(ham.querySelector(".ham").cloneNode(true));
      S.track.appendChild(S.runner);
      document.body.appendChild(S.track);
    }
    collapseTrack(); // measure the page without the track itself
    S.track.style.setProperty("height", document.documentElement.scrollHeight + "px");
  }

  // Centre points and headings, feet against each edge of the card from outside, clockwise.
  function lapPoints(card) {
    var sx = window.scrollX;
    var sy = window.scrollY;
    var c = card.getBoundingClientRect();
    var h = ham.getBoundingClientRect();
    var L = c.left + sx;
    var T = c.top + sy;
    var R = c.right + sx;
    var B = c.bottom + sy;
    var half = RUNNER_H / 2;
    var x0 = h.left + sx + h.width / 2;
    var y0 = h.bottom + sy - half;
    var xs = Math.min(Math.max(x0, L + RUNNER_W), R - RUNNER_W);
    return [
      [x0, y0, 0],
      [xs, T - half, 0],
      [R - RUNNER_W / 2, T - half, 0],
      [R + half, T + RUNNER_W / 2, 90],
      [R + half, B - RUNNER_W / 2, 90],
      [R - RUNNER_W / 2, B + half, 180],
      [L + RUNNER_W / 2, B + half, 180],
      [L - half, B - RUNNER_W / 2, 270],
      [L - half, T + RUNNER_W / 2, 270],
      [L + RUNNER_W / 2, T - half, 360],
      [xs, T - half, 360],
      [x0, y0, 360],
    ];
  }

  function runLap() {
    var card = lapCard();
    if (!card || document.hidden || den.getAttribute("data-pose") !== "laps" || !moving()) {
      scheduleLap(5000);
      return;
    }
    runnerParts();
    var points = lapPoints(card);
    var lengths = [0];
    var total = 0;
    for (var i = 1; i < points.length; i += 1) {
      var dx = points[i][0] - points[i - 1][0];
      var dy = points[i][1] - points[i - 1][1];
      total += Math.sqrt(dx * dx + dy * dy) + 8; // a turn at each corner takes a moment
      lengths.push(total);
    }
    var frames = points.map(function (p, n) {
      var x = p[0] - RUNNER_W / 2;
      var y = p[1] - RUNNER_H / 2;
      return { transform: "translate(" + x + "px, " + y + "px) rotate(" + p[2] + "deg)", offset: lengths[n] / total };
    });
    ham.classList.add("den-away");
    S.runner.classList.add("den-on");
    S.lap = S.runner.animate(frames, { duration: total * LAP_PACE, easing: "linear" });
    S.lap.onfinish = function () {
      S.lap = null;
      S.runner.classList.remove("den-on");
      collapseTrack();
      ham.classList.remove("den-away");
      scheduleLap(2200 + Math.random() * 2400);
    };
  }

  window.addEventListener(
    "scroll",
    function () {
      if (!S.lap) {
        return;
      }
      S.lap.pause();
      window.clearTimeout(S.scrollTimer);
      S.scrollTimer = window.setTimeout(function () {
        if (S.lap) {
          S.lap.play();
        }
      }, 250);
    },
    { passive: true },
  );

  var resizeTimer = 0;
  window.addEventListener("resize", function () {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(function () {
      if (S.lap) {
        stopLaps();
        scheduleLap(1000);
      }
    }, 200);
  });

  // Celebrations ----------------------------------------------------------------------------

  function flip() {
    if (!moving()) {
      return;
    }
    var pose = den.getAttribute("data-pose");
    if (pose === "sleep" || pose === "doze") {
      flash(den, "den-stir", 1700); // he wakes for it
    }
    var who = S.lap ? S.runner : ham;
    who.querySelector(".ham").animate(
      [
        { translate: "0 0", rotate: "0deg", transformOrigin: "50% 50%" },
        { translate: "0 -13px", rotate: "-180deg", transformOrigin: "50% 50%", offset: 0.5 },
        { translate: "0 0", rotate: "-360deg", transformOrigin: "50% 50%" },
      ],
      { duration: 760, easing: "ease-in-out", delay: 300 },
    );
  }

  function csrfToken() {
    try {
      return JSON.parse(document.body.getAttribute("hx-headers") || "{}")["X-CSRF-Token"] || null;
    } catch (e) {
      return null;
    }
  }

  // Run ``fn`` now if the page is in view, or once it comes into view.
  function whenVisible(fn) {
    if (!document.hidden) {
      fn();
      return;
    }
    document.addEventListener("visibilitychange", function shown() {
      if (!document.hidden) {
        document.removeEventListener("visibilitychange", shown);
        fn();
      }
    });
  }

  function sawBadge(id) {
    var token = csrfToken();
    if (!token || !/^\d+$/.test(id) || !window.fetch) {
      return;
    }
    window
      .fetch("/me/fun/seen", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRF-Token": token },
        body: "award=" + encodeURIComponent(id),
      })
      .catch(function () {
        // it flips again next time
      });
  }

  // The member who locked or marked done the call on Today: the bell and a sprint to the bowl,
  // or a pat of the belly and a waddle to bed. The den's state arrives with the same response
  // and moves him; this sets how he goes. Their quiet caption is this one.
  document.addEventListener("dinnerbell:celebrate", function (e) {
    var d = e.detail || {};
    if (d.event && d.run && d.state) {
      firstTime(captionKey(String(d.event), String(d.run), String(d.state)));
    }
    say(d.caption);
    if (!moving()) {
      return;
    }
    if (d.kind === "decided") {
      flash(den, "den-ring", 2600);
      S.gait = "sprint";
    } else if (d.kind === "done") {
      flash(den, "den-pat", 1300);
      S.gait = "waddle";
    }
    S.gaitUntil = Date.now() + 5000;
  });

  // The den's state ---------------------------------------------------------------------------

  function apply(onLoad) {
    var s = document.getElementById("den-state");
    if (!s) {
      return;
    }
    var names = ["pose", "period", "season", "season-depth", "event", "run", "state", "caption", "badge"];
    var signature = names
      .map(function (name) {
        return s.getAttribute("data-" + name) || "";
      })
      .join("|");
    if (signature === S.applied) {
      return; // htmx settles a card and its out-of-band state separately: the same state twice
    }
    S.applied = signature;
    if (S.pending) {
      window.clearTimeout(S.pending.timer); // a newer state wins over a walk still waiting
      S.pending = null;
    }
    ["period", "season", "season-depth"].forEach(function (name) {
      var value = s.getAttribute("data-" + name);
      if (value) {
        document.body.setAttribute("data-" + name, value);
      }
    });
    var previous = S.event;
    S.event = s.getAttribute("data-event");
    if (previous !== S.event && S.lap) {
      stopLaps();
    }
    var gait = Date.now() < S.gaitUntil ? S.gait : null;
    S.gait = null;
    S.gaitUntil = 0;
    var next = s.getAttribute("data-pose");
    if (gait === "waddle") {
      S.pending = {
        pose: next,
        timer: window.setTimeout(function () {
          S.pending = null;
          setPose(next, gait);
        }, 1300), // after the pat
      };
    } else {
      setPose(next, gait);
    }
    var state = s.getAttribute("data-state");
    var quiet = s.getAttribute("data-caption");
    var said = false;
    if (quiet && (state === "decided" || state === "done") && S.event) {
      if (firstTime(captionKey(S.event, s.getAttribute("data-run"), state))) {
        say(quiet);
        said = true;
      }
    }
    var award = s.getAttribute("data-badge");
    if (onLoad && award) {
      var badgeCaption = s.getAttribute("data-badge-caption");
      whenVisible(function () {
        window.setTimeout(
          function () {
            say(badgeCaption);
            flip();
            sawBadge(award);
          },
          said ? 4500 : 600,
        );
      });
    }
  }

  // A swap that changes nothing about him leaves a running lap alone; one that does moves him
  // (setPose stops the lap), and the next lap measures the card again.
  document.addEventListener("htmx:afterSettle", function () {
    apply(false);
  });

  // Taps ------------------------------------------------------------------------------------

  ham.addEventListener("click", function () {
    var pose = den.getAttribute("data-pose");
    if (pose === "sleep" || pose === "doze") {
      flash(den, "den-stir", 1500);
      return;
    }
    flash(heart, "den-pop", 1300);
    if (!moving()) {
      return;
    }
    flash(den, "den-puff", 1600);
    ham.animate([{ translate: "0 0" }, { translate: "0 -8px", offset: 0.4 }, { translate: "0 0" }], {
      duration: 380,
      easing: "ease-out",
    });
  });

  den.classList.add("den-live");
  apply(true);
})();
