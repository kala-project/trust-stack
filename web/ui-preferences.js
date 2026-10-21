"use strict";

(function uiPreferences(global) {
  const THEME_KEY = "truststack-theme";
  const SIDEBAR_KEY = "truststack-sidebar-collapsed";
  const themes = new Set(["light", "dark", "system"]);
  const systemTheme = global.matchMedia("(prefers-color-scheme: dark)");

  function read(key, fallback) {
    try { return global.localStorage.getItem(key) || fallback; } catch (_) { return fallback; }
  }

  function write(key, value) {
    try { global.localStorage.setItem(key, value); } catch (_) { /* Preferences remain usable for this page. */ }
  }

  function resolvedTheme(preference) {
    return preference === "system" ? (systemTheme.matches ? "dark" : "light") : preference;
  }

  function applyTheme(preference, notify) {
    const safePreference = themes.has(preference) ? preference : "system";
    const resolved = resolvedTheme(safePreference);
    const root = document.documentElement;
    root.dataset.theme = resolved;
    root.dataset.themePreference = safePreference;
    root.style.colorScheme = resolved;
    const meta = document.querySelector('meta[name="color-scheme"]');
    if (meta) meta.content = "light dark";
    document.querySelectorAll("[data-theme-select]").forEach(select => { select.value = safePreference; });
    if (notify) global.dispatchEvent(new CustomEvent("ui-themechange", { detail: { preference: safePreference, theme: resolved } }));
  }

  // This file is loaded synchronously in <head>; apply the persisted/system theme before paint.
  applyTheme(read(THEME_KEY, "system"), false);

  function initialize() {
    const body = document.body;
    const sidebar = document.querySelector("#sidebar-menu");
    const toggle = document.querySelector("#menu-button");
    const backdrop = document.querySelector("#sidebar-backdrop");
    const narrow = global.matchMedia("(max-width: 760px)");
    let desktopCollapsed = read(SIDEBAR_KEY, "false") === "true";
    let returnFocus = null;

    function drawerOpen() { return body.classList.contains("nav-open"); }

    function syncSidebar() {
      if (!sidebar || !toggle) return;
      const isNarrow = narrow.matches;
      const open = isNarrow && drawerOpen();
      body.classList.toggle("sidebar-collapsed", !isNarrow && desktopCollapsed);
      body.classList.toggle("drawer-scroll-lock", open);
      toggle.setAttribute("aria-expanded", String(isNarrow ? open : !desktopCollapsed));
      toggle.setAttribute("aria-label", isNarrow ? (open ? "Close navigation" : "Open navigation") : (desktopCollapsed ? "Expand sidebar" : "Collapse sidebar"));
      const label = toggle.querySelector(".sr-only");
      if (label) label.textContent = toggle.getAttribute("aria-label");
      sidebar.inert = isNarrow ? !open : desktopCollapsed;
      if (backdrop) {
        backdrop.hidden = !open;
        backdrop.setAttribute("aria-hidden", String(!open));
      }
    }

    function closeDrawer(restoreFocus) {
      body.classList.remove("nav-open");
      syncSidebar();
      if (restoreFocus && returnFocus && returnFocus.isConnected) returnFocus.focus({ preventScroll: true });
      returnFocus = null;
    }

    if (sidebar && toggle) {
      toggle.addEventListener("click", event => {
        // app.js retains its historical mobile listener; own this control in the
        // capture phase so that listener cannot toggle the drawer a second time.
        event.stopImmediatePropagation();
        if (narrow.matches) {
          if (drawerOpen()) closeDrawer(true);
          else {
            returnFocus = toggle;
            body.classList.add("nav-open");
            syncSidebar();
            const first = sidebar.querySelector(".nav-item:not([hidden])");
            if (first) first.focus({ preventScroll: true });
          }
        } else {
          desktopCollapsed = !desktopCollapsed;
          write(SIDEBAR_KEY, String(desktopCollapsed));
          syncSidebar();
        }
      }, true);
      sidebar.addEventListener("click", event => {
        if (!event.target.closest(".nav-item")) return;
        if (narrow.matches) closeDrawer(false);
        else syncSidebar();
      });
      if (backdrop) backdrop.addEventListener("click", () => closeDrawer(true));
      document.addEventListener("keydown", event => {
        if (event.key === "Escape" && narrow.matches && drawerOpen()) {
          event.preventDefault();
          closeDrawer(true);
        }
      });
      narrow.addEventListener("change", () => {
        body.classList.remove("nav-open");
        returnFocus = null;
        syncSidebar();
      });
      syncSidebar();
    }

    document.querySelectorAll("[data-theme-select]").forEach(select => {
      select.value = document.documentElement.dataset.themePreference || "system";
      select.addEventListener("change", () => {
        const preference = themes.has(select.value) ? select.value : "system";
        write(THEME_KEY, preference);
        applyTheme(preference, true);
      });
    });

    systemTheme.addEventListener("change", () => {
      if ((document.documentElement.dataset.themePreference || "system") === "system") applyTheme("system", true);
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initialize, { once: true });
  else initialize();
})(globalThis);
