// SPDX-License-Identifier: AGPL-3.0-or-later
// Dinnerbell's service worker (spec 6.15, ADR 0010). Served at /sw.js, so its scope is the whole
// site, and only while Admin's Web app switch is on. Its own CSP lets it reach this origin only.
//
// It shows pushes on Android and on iOS before 18.4, which don't show a Declarative Web Push by
// themselves, and opens a push's page when it's tapped. It has no fetch handler and uses no
// cache: every page comes from the server, as in the browser.
(function () {
  "use strict";

  var FALLBACK_TITLE = "Dinnerbell";
  var FALLBACK_BODY = "Open Dinnerbell to see what's new.";

  self.addEventListener("install", function () {
    self.skipWaiting();
  });

  self.addEventListener("activate", function (event) {
    event.waitUntil(self.clients.claim());
  });

  // A page on this site, or the home page for anything else, so a push can't open another site.
  function samePage(url) {
    try {
      var found = new URL(url, self.location.origin);
      if (found.origin === self.location.origin) {
        return found.href;
      }
    } catch (ignored) {
      // not a URL: fall through to the home page
    }
    return new URL("/", self.location.origin).href;
  }

  // The Declarative Web Push message ({"web_push": 8030, "notification": {...}}), or null.
  function message(data) {
    if (!data) {
      return null;
    }
    try {
      var parsed = data.json();
      return parsed && typeof parsed.notification === "object" ? parsed.notification : null;
    } catch (ignored) {
      return null;
    }
  }

  // Always show something: Safari takes back permission after a push that shows nothing.
  self.addEventListener("push", function (event) {
    var note = message(event.data) || {};
    var title = typeof note.title === "string" && note.title ? note.title : FALLBACK_TITLE;
    var options = {
      body: typeof note.body === "string" ? note.body : FALLBACK_BODY,
      icon: "/static/icon-192.png",
      data: { url: samePage(typeof note.navigate === "string" ? note.navigate : "/") }
    };
    if (typeof note.tag === "string" && note.tag) {
      options.tag = note.tag;
    }
    event.waitUntil(self.registration.showNotification(title, options));
  });

  // A tap opens the push's page: in a window of the app this worker controls, or a new one.
  function openPage(url) {
    return self.clients.matchAll({ type: "window" }).then(function (windows) {
      var open = windows[0];
      if (!open || !("navigate" in open)) {
        return self.clients.openWindow(url);
      }
      return open
        .navigate(url)
        .then(function (navigated) {
          return (navigated || open).focus();
        })
        .catch(function () {
          return self.clients.openWindow(url);
        });
    });
  }

  self.addEventListener("notificationclick", function (event) {
    event.notification.close();
    var data = event.notification.data || {};
    event.waitUntil(openPage(samePage(typeof data.url === "string" ? data.url : "/")));
  });
})();
