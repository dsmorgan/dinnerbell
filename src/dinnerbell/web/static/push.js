// SPDX-License-Identifier: AGPL-3.0-or-later
// Web push in the browser (spec 6.15). Loaded from /static on a member's pages while the web app
// is on, so the CSP needs no inline code.
//
// On Get the app, it runs the Notifications card. The card stays hidden unless this browser can
// take pushes: on iPhone and iPad only inside the installed app, elsewhere wherever PushManager
// exists. Turning on asks for permission (iOS needs the tap), registers /sw.js for the whole site
// if it isn't already, drops a subscription made with a different key (a changed secret key, say),
// subscribes, and sends the subscription to Dinnerbell with the page's CSRF token. Turn off
// unsubscribes and tells Dinnerbell.
//
// Dinnerbell ties a subscription to the sign-in that sent it, and forgets it when that session
// ends. So on every other page inside the installed app, an existing subscription is sent again
// (one POST, and none when this session sent it in the last 10 minutes): after a new sign-in, the
// device's notifications come back on their own.
(function () {
  "use strict";

  var meta = document.querySelector('meta[name="dinnerbell-push-key"]');
  if (!meta) {
    return;
  }
  var card = document.getElementById("notifications");
  var ios = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  var installed = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window) || (ios && !installed)) {
    return;
  }

  var STATES = {
    on: "Notifications are on for this device.",
    off: "Notifications are off for this device.",
    blocked: "Notifications are blocked for Dinnerbell. Allow them in your device's settings, then open this page again.",
    error: "That didn't work. Check your connection and try again."
  };
  var state = card && document.getElementById("push-state");
  var onButton = card && document.getElementById("push-on");
  var offButton = card && document.getElementById("push-off");

  function show(name, text) {
    state.textContent = text || STATES[name];
    onButton.hidden = name === "on" || name === "blocked";
    offButton.hidden = name !== "on";
  }

  function keyBytes(text) {
    var base64 = text.replace(/-/g, "+").replace(/_/g, "/");
    var raw = window.atob(base64 + "===".slice((base64.length + 3) % 4));
    var bytes = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i += 1) {
      bytes[i] = raw.charCodeAt(i);
    }
    return bytes;
  }

  var KEY = keyBytes(meta.getAttribute("content") || "");

  function sameKey(buffer) {
    if (!buffer) {
      return false;
    }
    var other = new Uint8Array(buffer);
    if (other.length !== KEY.length) {
      return false;
    }
    for (var i = 0; i < KEY.length; i += 1) {
      if (other[i] !== KEY[i]) {
        return false;
      }
    }
    return true;
  }

  function csrfToken() {
    try {
      return JSON.parse(document.body.getAttribute("hx-headers") || "{}")["X-CSRF-Token"] || "";
    } catch (e) {
      return "";
    }
  }

  // Posts a form to Dinnerbell. Resolves on a 2xx; rejects with the server's message on a 400.
  function post(path, fields) {
    var body = Object.keys(fields)
      .map(function (name) {
        return encodeURIComponent(name) + "=" + encodeURIComponent(fields[name]);
      })
      .join("&");
    return window
      .fetch(path, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRF-Token": csrfToken() },
        body: body
      })
      .then(function (response) {
        if (response.ok) {
          return null;
        }
        return response.text().then(function (text) {
          throw new Error(response.status === 400 ? text : "");
        });
      });
  }

  var SAVED = "dinnerbell-push-saved";
  var RESAVE_MS = 10 * 60 * 1000;

  // What this session last sent, so browsing the app doesn't send it on every page: a SHA-256
  // of the CSRF token and the endpoint, never either itself. The token changes with the
  // session, so a new sign-in always sends it again.
  function stamp(subscription) {
    var text = new TextEncoder().encode(csrfToken() + " " + subscription.endpoint);
    return window.crypto.subtle.digest("SHA-256", text).then(function (digest) {
      return Array.prototype.map
        .call(new Uint8Array(digest), function (b) {
          return ("0" + b.toString(16)).slice(-2);
        })
        .join("");
    });
  }

  function forget() {
    try {
      window.localStorage.removeItem(SAVED);
    } catch (e) {
      // nothing stored
    }
  }

  function savedRecently(subscription) {
    return stamp(subscription).then(function (hash) {
      try {
        var saved = JSON.parse(window.localStorage.getItem(SAVED) || "{}");
        return saved.hash === hash && Date.now() - saved.at < RESAVE_MS;
      } catch (e) {
        return false;
      }
    });
  }

  function save(subscription) {
    var keys = subscription.toJSON().keys || {};
    var fields = { endpoint: subscription.endpoint, p256dh: keys.p256dh || "", auth: keys.auth || "" };
    return post("/app/notifications", fields)
      .then(function () {
        return stamp(subscription);
      })
      .then(function (hash) {
        try {
          window.localStorage.setItem(SAVED, JSON.stringify({ hash: hash, at: Date.now() }));
        } catch (e) {
          // private browsing: it sends again next page
        }
      })
      .catch(function (error) {
        forget(); // so the next page sends it again
        throw error;
      });
  }

  // Every page but Get the app, inside the installed app: send an existing subscription again.
  if (!card) {
    if (!installed || Notification.permission !== "granted") {
      return;
    }
    navigator.serviceWorker
      .getRegistration("/")
      .then(function (reg) {
        return reg ? reg.pushManager.getSubscription() : null;
      })
      .then(function (subscription) {
        if (!subscription || !sameKey(subscription.options.applicationServerKey)) {
          return null;
        }
        return savedRecently(subscription).then(function (recent) {
          return recent ? null : save(subscription);
        });
      })
      .catch(function () {
        // the next page tries again
      });
    return;
  }

  function registration() {
    return navigator.serviceWorker.getRegistration("/").then(function (found) {
      return found || navigator.serviceWorker.register("/sw.js", { scope: "/" });
    }).then(function () {
      return navigator.serviceWorker.ready;
    });
  }

  // The subscription made with this server's key, after dropping one made with another.
  function current(reg) {
    return reg.pushManager.getSubscription().then(function (subscription) {
      if (!subscription || sameKey(subscription.options.applicationServerKey)) {
        return subscription;
      }
      var old = subscription.endpoint;
      return subscription.unsubscribe().then(function () {
        return post("/app/notifications/off", { endpoint: old }).catch(function () {}).then(function () {
          return null;
        });
      });
    });
  }

  function failed(error) {
    show("error", error && error.message ? error.message : STATES.error);
  }

  function turnOn() {
    onButton.disabled = true;
    // Ask first, inside the tap: iOS refuses a permission request that isn't one.
    Notification.requestPermission()
      .then(function (permission) {
        if (permission !== "granted") {
          show(permission === "denied" ? "blocked" : "off");
          return null;
        }
        return registration()
          .then(function (reg) {
            return current(reg).then(function (subscription) {
              return subscription || reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: KEY });
            });
          })
          .then(function (subscription) {
            return save(subscription).then(function () {
              show("on");
            });
          });
      })
      .catch(failed)
      .then(function () {
        onButton.disabled = false;
      });
  }

  function turnOff() {
    offButton.disabled = true;
    navigator.serviceWorker
      .getRegistration("/")
      .then(function (reg) {
        return reg ? reg.pushManager.getSubscription() : null;
      })
      .then(function (subscription) {
        if (!subscription) {
          return null;
        }
        var endpoint = subscription.endpoint;
        return post("/app/notifications/off", { endpoint: endpoint }).then(function () {
          forget();
          return subscription.unsubscribe();
        });
      })
      .then(function () {
        show("off");
      })
      .catch(failed)
      .then(function () {
        offButton.disabled = false;
      });
  }

  onButton.addEventListener("click", turnOn);
  offButton.addEventListener("click", turnOff);
  card.hidden = false;

  if (Notification.permission === "denied") {
    show("blocked");
    return;
  }
  navigator.serviceWorker
    .getRegistration("/")
    .then(function (reg) {
      return reg && Notification.permission === "granted" ? current(reg) : null;
    })
    .then(function (subscription) {
      if (!subscription) {
        show("off");
        return null;
      }
      return save(subscription).then(function () {
        show("on");
      });
    })
    .catch(failed);
})();
