// SPDX-License-Identifier: AGPL-3.0-or-later
// Fun mode (#153): Dinnerbone's Den. Loaded only for a viewer who has fun mode on; with it off,
// no page links this file. Loaded from /static, so the CSP needs no inline code.
//
// The scene is on <body> (data-fun, data-period, data-season, data-season-depth), rendered by
// the server in the household's time zone, so this script never reads the clock to draw it.
// Anything that moves checks prefers-reduced-motion first, and holds a still pose when it's set.
"use strict";
