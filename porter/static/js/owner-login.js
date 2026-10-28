/* Fragment credentials never enter a request URL, history entry or referrer. */
(() => {
  "use strict";
  try {
    const theme = localStorage.getItem("porter-theme");
    if (theme === "light" || (!theme && matchMedia("(prefers-color-scheme: light)").matches)) {
      document.documentElement.dataset.theme = "light";
    }
  } catch { /* Storage restrictions must not prevent sign-in. */ }
  const fragment = new URLSearchParams(location.hash.slice(1));
  const code = fragment.get("code");
  if (location.hash) history.replaceState(null, "", location.pathname + location.search);
  if (!code) return;
  document.getElementById("owner-code").value = code;
  document.getElementById("owner-opening").hidden = false;
  document.getElementById("owner-link-form").requestSubmit();
})();
