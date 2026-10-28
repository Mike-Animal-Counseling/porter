/* Porter panel JS — theme, toasts, copy buttons, drawer, sidebar. No frameworks. */
(function () {
  "use strict";
  var root = document.documentElement;

  /* ---- theme: localStorage, default dark, respect prefers-color-scheme on first visit ---- */
  function applyTheme(name) {
    if (name === "light") root.setAttribute("data-theme", "light");
    else root.removeAttribute("data-theme");
    var btn = document.getElementById("theme-toggle");
    if (btn) btn.setAttribute("aria-pressed", name === "light" ? "true" : "false");
    var cv = document.getElementById("constel");
    if (cv && cv._nodes && cv.getClientRects().length) drawConstellation(cv);
  }
  var stored = null;
  try { stored = localStorage.getItem("porter-theme"); } catch (e) {}
  if (!stored && window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches) {
    stored = "light";
  }
  applyTheme(stored === "light" ? "light" : "dark");
  document.addEventListener("click", function (ev) {
    var t = ev.target.closest("#theme-toggle");
    if (!t) return;
    var next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
    try { localStorage.setItem("porter-theme", next); } catch (e) {}
    applyTheme(next);
  });

  /* ---- toasts: server sends HX-Trigger: {"toast": {"type": "...", "msg": "..."}} ---- */
  function toast(type, msg) {
    var box = document.getElementById("toasts");
    if (!box) return;
    var el = document.createElement("div");
    el.className = "toast toast--" + (type === "success" || type === "error" ? type : "info");
    el.textContent = msg;
    box.appendChild(el);
    setTimeout(function () { el.remove(); }, 4000);
  }
  document.body.addEventListener("toast", function (ev) {
    var d = ev.detail || {};
    toast(d.type, d.msg || "");
  });
  function requestFailed() {
    toast("error", document.body.getAttribute("data-request-failed-msg"));
  }
  ["htmx:responseError", "htmx:sendError", "htmx:timeout"].forEach(function (name) {
    document.body.addEventListener(name, requestFailed);
  });
  // inbox triage: keep the sidebar badge + page title in sync after HTMX actions
  document.body.addEventListener("inboxCount", function (ev) {
    var n = (ev.detail || {}).n;
    if (typeof n !== "number") return;
    var badge = document.getElementById("nav-inbox-badge");
    if (badge) {
      if (n > 0) { badge.textContent = n; badge.style.display = ""; }
      else { badge.style.display = "none"; }
    }
    var h1 = document.querySelector(".page-head h1");
    if (h1) h1.textContent = h1.textContent.replace(/\(\d+\)/, "(" + n + ")");
  });
  // legacy ?msg= banner -> render as toast, then clean the URL
  (function () {
    var m = new URLSearchParams(window.location.search).get("msg");
    if (m) {
      toast("success", m);
      var u = new URL(window.location.href);
      u.searchParams.delete("msg");
      window.history.replaceState(null, "", u.toString());
    }
  })();

  /* ---- copy buttons: data-copy="<text>" ---- */
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest("[data-copy]");
    if (!btn) return;
    var text = btn.getAttribute("data-copy") || "";
    function done() { toast("success", btn.getAttribute("data-copied-msg") || "Copied"); }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { fallback(); });
    } else { fallback(); }
    function fallback() {
      var previous = document.activeElement;
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("aria-hidden", "true");
      ta.style.position = "fixed";
      ta.style.top = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      try {
        if (!document.execCommand("copy")) throw new Error("copy failed");
        done();
      } catch (e) { toast("error", document.body.getAttribute("data-copy-failed-msg")); }
      ta.remove();
      if (previous && previous.isConnected) previous.focus();
    }
  });

  /* ---- drawer ---- */
  var overlay = document.getElementById("drawer-overlay");
  var drawer = document.getElementById("drawer");
  var shell = document.querySelector(".shell");
  var returnFocus = null;
  var previousOverflow = "";
  function openDrawer() {
    if (!drawer) return;
    if (!drawer.classList.contains("drawer--open")) {
      returnFocus = document.activeElement;
      previousOverflow = document.body.style.overflow;
    }
    drawer.inert = false;
    drawer.setAttribute("aria-hidden", "false");
    drawer.classList.add("drawer--open");
    if (overlay) overlay.classList.add("drawer-overlay--open");
    if (shell) shell.inert = true;
    document.body.style.overflow = "hidden";
    drawer.focus();
  }
  function closeDrawer() {
    if (!drawer || !drawer.classList.contains("drawer--open")) return;
    if (overlay) overlay.classList.remove("drawer-overlay--open");
    drawer.classList.remove("drawer--open");
    drawer.inert = true;
    drawer.setAttribute("aria-hidden", "true");
    if (shell) shell.inert = false;
    document.body.style.overflow = previousOverflow;
    var body = document.getElementById("drawer-body");
    if (body) body.innerHTML = "";
    if (returnFocus && returnFocus.isConnected) returnFocus.focus();
    returnFocus = null;
  }
  window.porterCloseDrawer = closeDrawer;
  if (overlay) overlay.addEventListener("click", closeDrawer);
  document.addEventListener("click", function (ev) {
    if (ev.target.closest("[data-close-drawer]")) closeDrawer();
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") {
      closeDrawer();
      if (sidebar && sidebar.classList.contains("sidebar--open")) {
        setSidebar(false);
        if (menuToggle) menuToggle.focus();
      }
    } else if (ev.key === "Tab" && drawer && drawer.classList.contains("drawer--open")) {
      var controls = Array.prototype.filter.call(drawer.querySelectorAll(
        'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex="0"]'
      ), function (element) { return element.getClientRects().length > 0; });
      var first = controls[0], last = controls[controls.length - 1];
      if (!first) { ev.preventDefault(); drawer.focus(); }
      else if (ev.shiftKey && (document.activeElement === first || document.activeElement === drawer)) {
        ev.preventDefault(); last.focus();
      } else if (!ev.shiftKey && document.activeElement === last) {
        ev.preventDefault(); first.focus();
      }
    }
  });
  // after HTMX swaps content into the drawer, open it
  document.body.addEventListener("htmx:afterSwap", function (ev) {
    if (ev.target && ev.target.id === "drawer-body") {
      openDrawer();
    }
  });

  /* ---- sidebar (mobile drawer + collapse) ---- */
  var sidebar = document.getElementById("sidebar");
  var menuToggle = document.getElementById("sidebar-toggle");
  var narrow = window.matchMedia("(max-width: 720px)");
  function setSidebar(open) {
    if (!sidebar) return;
    sidebar.classList.toggle("sidebar--open", open);
    sidebar.inert = narrow.matches && !open;
    if (sidebar.inert) sidebar.setAttribute("aria-hidden", "true");
    else sidebar.removeAttribute("aria-hidden");
    if (menuToggle) menuToggle.setAttribute("aria-expanded", String(open));
  }
  setSidebar(false);
  narrow.addEventListener("change", function () { setSidebar(false); });
  document.addEventListener("click", function (ev) {
    if (ev.target.closest("#sidebar-toggle")) {
      setSidebar(sidebar && !sidebar.classList.contains("sidebar--open"));
    } else if (narrow.matches && !ev.target.closest("#sidebar")) { setSidebar(false); }
  });

  /* ---- language segmented control: POST then reload ---- */
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest("[data-set-lang]");
    if (!btn) return;
    var lang = btn.getAttribute("data-set-lang");
    fetch("/panel/settings/language", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: "lang=" + encodeURIComponent(lang),
      credentials: "same-origin"
    }).then(function (response) {
      if (!response.ok) throw new Error("language update failed");
      window.location.reload();
    }).catch(requestFailed);
  });

  /* ---- confirm dialogs via hx-confirm are native; nothing needed ---- */

  /* ---- memories: domain filter (personal / project / all) ---- */
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest("[data-domain-filter]");
    if (!btn) return;
    var val = btn.getAttribute("data-domain-filter");
    var input = document.getElementById("domain-input");
    if (input) input.value = val;
    document.querySelectorAll("[data-domain-filter]").forEach(function (b) {
      b.setAttribute("aria-pressed", b === btn ? "true" : "false");
    });
    var form = document.getElementById("mem-filter-form");
    if (form && window.htmx) {
      htmx.ajax("GET", "/panel/memories/rows", {
        target: "#mem-rows", swap: "innerHTML",
        values: htmx.values(form),
      });
    }
  });

  /* ---- memories: table/graph view toggle ---- */
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest("[data-mem-view]");
    if (!btn) return;
    var view = btn.getAttribute("data-mem-view");
    var tw = document.getElementById("mem-table-wrap");
    var gw = document.getElementById("mem-graph-wrap");
    document.querySelectorAll("[data-mem-view]").forEach(function (b) {
      b.setAttribute("aria-pressed", b === btn ? "true" : "false");
    });
    if (tw) tw.hidden = view !== "table";
    if (gw) {
      gw.hidden = view !== "graph";
      if (view === "graph") initConstellation();
    }
  });

  /* ---- constellation: mini force-directed graph of memories ----
     Data comes from canvas[data-nodes] (server-escaped JSON: id/label/tags).
     Labels are drawn with fillText only — never innerHTML. Clicks reuse the
     existing HTMX drawer via htmx.ajax. Vanilla, no libraries. */
  var constelDone = false;
  function initConstellation() {
    var cv = document.getElementById("constel");
    if (!cv || constelDone) { if (cv && constelDone) drawConstellation(cv); return; }
    var nodes;
    try { nodes = JSON.parse(cv.getAttribute("data-nodes") || "[]"); }
    catch (e) { nodes = []; }
    if (!nodes.length) return;
    // links: memories sharing at least one tag
    var tagMap = {};
    nodes.forEach(function (n, i) {
      (n.tags || []).forEach(function (tg) {
        (tagMap[tg] = tagMap[tg] || []).push(i);
      });
    });
    var links = [];
    Object.keys(tagMap).forEach(function (tg) {
      var idx = tagMap[tg];
      for (var a = 0; a < idx.length; a++)
        for (var b = a + 1; b < idx.length; b++)
          links.push([idx[a], idx[b]]);
    });
    // init positions on a circle, then relax (all in CSS pixels)
    var W, H, dpr = 1;
    function size() {
      var r = cv.getBoundingClientRect();
      dpr = Math.min(2, window.devicePixelRatio || 1);
      W = Math.max(280, r.width);
      H = parseInt(getComputedStyle(cv).height, 10) || 320;
      cv.width = Math.round(W * dpr);
      cv.height = Math.round(H * dpr);
    }
    size();
    nodes.forEach(function (n, i) {
      var a = (i / nodes.length) * Math.PI * 2;
      var jr = 0.9 + Math.random() * 0.2; // break symmetry
      n.x = W / 2 + Math.cos(a) * Math.min(W, H) * 0.36 * jr;
      n.y = H / 2 + Math.sin(a) * Math.min(W, H) * 0.36 * jr;
      n.vx = 0; n.vy = 0;
      n.r = 5 + Math.min(4, (n.tags || []).length);
    });
    for (var iter = 0; iter < 150; iter++) {
      // repulsion (stronger so labels never stack)
      for (var i = 0; i < nodes.length; i++) {
        for (var j = i + 1; j < nodes.length; j++) {
          var dx = nodes[i].x - nodes[j].x, dy = nodes[i].y - nodes[j].y;
          var d2 = dx * dx + dy * dy + 40;
          var f = 3200 / d2;
          var d = Math.sqrt(d2);
          dx /= d; dy /= d;
          nodes[i].vx += dx * f; nodes[i].vy += dy * f;
          nodes[j].vx -= dx * f; nodes[j].vy -= dy * f;
        }
      }
      // springs on shared-tag links
      links.forEach(function (l) {
        var A = nodes[l[0]], B = nodes[l[1]];
        var dx = B.x - A.x, dy = B.y - A.y;
        var d = Math.sqrt(dx * dx + dy * dy) || 1;
        var f = (d - 150) * 0.012;
        dx /= d; dy /= d;
        A.vx += dx * f; A.vy += dy * f;
        B.vx -= dx * f; B.vy -= dy * f;
      });
      // gentle centering + damping + integrate
      nodes.forEach(function (n) {
        n.vx += (W / 2 - n.x) * 0.004;
        n.vy += (H / 2 - n.y) * 0.004;
        n.vx *= 0.86; n.vy *= 0.86;
        n.x += n.vx; n.y += n.vy;
        n.x = Math.max(40, Math.min(W - 40, n.x));
        n.y = Math.max(30, Math.min(H - 30, n.y));
      });
    }
    cv._nodes = nodes;
    constelDone = true;
    drawConstellation(cv);
    if (cv._porterEventsBound) return;
    cv._porterEventsBound = true;
    cv.addEventListener("click", function (e) {
      var nodes = cv._nodes || [];
      var r = cv.getBoundingClientRect();
      var mx = e.clientX - r.left, my = e.clientY - r.top; // CSS px
      var best = null, bestD = 1e9;
      nodes.forEach(function (n) {
        var d = Math.hypot(n.x - mx, n.y - my);
        if (d < bestD) { bestD = d; best = n; }
      });
      if (best && bestD < Math.max(16, best.r + 8) && window.htmx) {
        window.htmx.ajax("GET", "/panel/memories/" + encodeURIComponent(best.id),
          { target: "#drawer-body", swap: "innerHTML" });
      }
    });
    window.addEventListener("resize", function () {
      clearTimeout(cv._resizeTimer);
      cv._resizeTimer = setTimeout(function () {
        constelDone = false;
        if (cv.isConnected && cv.getClientRects().length) initConstellation();
      }, 150);
    });
  }
  function drawConstellation(cv) {
    var nodes = cv._nodes || [];
    var ctx = cv.getContext("2d");
    var r = cv.getBoundingClientRect();
    var dpr = Math.min(2, window.devicePixelRatio || 1);
    var W = Math.max(280, r.width);
    var H = parseInt(getComputedStyle(cv).height, 10) || 320;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    var css = getComputedStyle(document.documentElement);
    var gold = (css.getPropertyValue("--accent") || "#D4A24E").trim();
    var dim = (css.getPropertyValue("--text-tertiary") || "#6E675C").trim();
    ctx.clearRect(0, 0, W, H);
    // links
    var tagMap = {};
    nodes.forEach(function (n, i) {
      (n.tags || []).forEach(function (tg) { (tagMap[tg] = tagMap[tg] || []).push(i); });
    });
    ctx.strokeStyle = gold; ctx.globalAlpha = 0.28; ctx.lineWidth = 1;
    Object.keys(tagMap).forEach(function (tg) {
      var idx = tagMap[tg];
      for (var a = 0; a < idx.length; a++)
        for (var b = a + 1; b < idx.length; b++) {
          ctx.beginPath();
          ctx.moveTo(nodes[idx[a]].x, nodes[idx[a]].y);
          ctx.lineTo(nodes[idx[b]].x, nodes[idx[b]].y);
          ctx.stroke();
        }
    });
    ctx.globalAlpha = 1;
    // nodes
    nodes.forEach(function (n) {
      var g = ctx.createRadialGradient(n.x, n.y, 0, n.x, n.y, n.r * 3);
      g.addColorStop(0, gold); g.addColorStop(1, "rgba(212,162,78,0)");
      ctx.fillStyle = g;
      ctx.beginPath(); ctx.arc(n.x, n.y, n.r * 3, 0, 7); ctx.fill();
      ctx.fillStyle = gold;
      ctx.beginPath(); ctx.arc(n.x, n.y, n.r, 0, 7); ctx.fill();
    });
    // labels: de-collide — draw highest-confidence first, skip any label
    // whose box intersects an already-placed one. Labels never overlap.
    ctx.font = "11px " + (css.getPropertyValue("font-family") || "sans-serif");
    ctx.textAlign = "center";
    var MAX_LABEL_W = 132, LABEL_H = 14, PAD = 4;
    function isWordChar(ch) { return /[A-Za-z0-9_]/.test(ch); }
    function fitLabel(text) {
      text = String(text || "");
      if (ctx.measureText(text).width <= MAX_LABEL_W) return text;
      var ell = "…", lo = 0, hi = text.length;
      while (lo < hi) {
        var mid = Math.ceil((lo + hi) / 2);
        if (ctx.measureText(text.slice(0, mid) + ell).width <= MAX_LABEL_W) lo = mid;
        else hi = mid - 1;
      }
      // don't cut a Latin word mid-way: backtrack to the previous space
      if (lo < text.length && isWordChar(text.charAt(lo - 1)) && isWordChar(text.charAt(lo))) {
        var sp = text.lastIndexOf(" ", lo - 1);
        if (sp > lo * 0.5) lo = sp;
      }
      return text.slice(0, lo).replace(/\s+$/, "") + ell;
    }
    function boxesHit(a, b) {
      return a.x0 < b.x1 && a.x1 > b.x0 && a.y0 < b.y1 && a.y1 > b.y0;
    }
    var placed = [];
    nodes.slice().sort(function (a, b) {
      return (b.confidence || 0) - (a.confidence || 0);
    }).forEach(function (n) {
      var label = fitLabel(n.label);
      if (!label) return;
      var w = Math.ceil(ctx.measureText(label).width);
      // clamp horizontally so the label never clips at the canvas edge
      var lx = Math.max(w / 2 + PAD, Math.min(W - w / 2 - PAD, n.x));
      var ly = n.y + n.r + 13; // baseline
      var box = { x0: lx - w / 2 - PAD, y0: ly - LABEL_H, x1: lx + w / 2 + PAD, y1: ly + 3 };
      for (var i = 0; i < placed.length; i++) {
        if (boxesHit(box, placed[i])) return; // lower priority: skip the label
      }
      placed.push(box);
      ctx.fillStyle = dim;
      ctx.fillText(label, lx, ly);
    });
  }
})();
