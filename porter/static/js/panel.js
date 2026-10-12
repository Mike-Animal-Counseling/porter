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
      var ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); done(); } catch (e) {}
      ta.remove();
    }
  });

  /* ---- drawer ---- */
  var overlay = document.getElementById("drawer-overlay");
  var drawer = document.getElementById("drawer");
  function closeDrawer() {
    if (overlay) overlay.classList.remove("drawer-overlay--open");
    if (drawer) {
      drawer.classList.remove("drawer--open");
      var body = document.getElementById("drawer-body");
      if (body) body.innerHTML = "";
    }
  }
  window.porterCloseDrawer = closeDrawer;
  if (overlay) overlay.addEventListener("click", closeDrawer);
  document.addEventListener("click", function (ev) {
    if (ev.target.closest("[data-close-drawer]")) closeDrawer();
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") {
      closeDrawer();
      var sb = document.getElementById("sidebar");
      if (sb) sb.classList.remove("sidebar--open");
    }
  });
  // after HTMX swaps content into the drawer, open it
  document.body.addEventListener("htmx:afterSwap", function (ev) {
    if (ev.target && ev.target.id === "drawer-body") {
      if (overlay) overlay.classList.add("drawer-overlay--open");
      if (drawer) drawer.classList.add("drawer--open");
    }
  });

  /* ---- sidebar (mobile drawer + collapse) ---- */
  document.addEventListener("click", function (ev) {
    var t = ev.target.closest("#sidebar-toggle");
    if (!t) return;
    var sb = document.getElementById("sidebar");
    if (sb) sb.classList.toggle("sidebar--open");
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
    }).then(function () { window.location.reload(); });
  });

  /* ---- confirm dialogs via hx-confirm are native; nothing needed ---- */
})();
