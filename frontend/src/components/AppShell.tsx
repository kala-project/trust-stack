import { type ReactNode, type RefObject, useEffect, useRef, useState } from "react";
import { Icon } from "./Icon";
import styles from "./AppShell.module.css";

const breakpoint = "(max-width: 960px)";
const collapsedKey = "truststack-sidebar-collapsed";

type AppShellProps = {
  sidebar: ReactNode;
  sidebarLabel: string;
  sectionTitle: ReactNode;
  children: ReactNode;
  toolbar?: ReactNode;
  mainId?: string;
  mainRef?: RefObject<HTMLElement | null>;
  mainClassName?: string;
  shellClassName?: string;
};

export function AppShell({
  sidebar, sidebarLabel, sectionTitle, children, toolbar,
  mainId = "main-content", mainRef, mainClassName = "", shellClassName = "",
}: AppShellProps) {
  const [small, setSmall] = useState(() => matchMedia(breakpoint).matches);
  const [navOpen, setNavOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(() => {
    try { return localStorage.getItem(collapsedKey) === "true"; } catch { return false; }
  });
  const [theme, setTheme] = useState(document.documentElement.dataset.theme || "light");
  const nav = useRef<HTMLElement>(null);
  const toggle = useRef<HTMLButtonElement>(null);
  const hideNav = small ? !navOpen : collapsed;

  useEffect(() => {
    const media = matchMedia(breakpoint);
    const resize = () => { setSmall(media.matches); setNavOpen(false); };
    const navigate = () => setNavOpen(false);
    media.addEventListener("change", resize);
    window.addEventListener("hashchange", navigate);
    return () => { media.removeEventListener("change", resize); window.removeEventListener("hashchange", navigate); };
  }, []);

  useEffect(() => {
    if (!small || !navOpen) return;
    const before = document.activeElement as HTMLElement | null;
    const nodes = () => [...(nav.current?.querySelectorAll<HTMLElement>("a[href], button:not([disabled]), input, select, textarea") ?? [])];
    (nodes().find(node => !node.hasAttribute("data-shell-close")) ?? nav.current)?.focus();
    const handle = (event: KeyboardEvent) => {
      if (event.key === "Escape") { setNavOpen(false); event.preventDefault(); return; }
      if (event.key !== "Tab") return;
      const focusable = nodes();
      const first = focusable[0], last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) { last?.focus(); event.preventDefault(); }
      else if (!event.shiftKey && document.activeElement === last) { first?.focus(); event.preventDefault(); }
    };
    document.addEventListener("keydown", handle);
    return () => {
      document.removeEventListener("keydown", handle);
      (before?.isConnected ? before : toggle.current)?.focus({ preventScroll: true });
    };
  }, [small, navOpen]);

  function toggleNavigation() {
    if (small) setNavOpen(value => !value);
    else {
      setCollapsed(value => {
        const next = !value;
        try { localStorage.setItem(collapsedKey, String(next)); } catch {}
        requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
        return next;
      });
    }
  }

  function switchTheme() {
    const next = theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    setTheme(next);
    try {
      localStorage.setItem("truststack-theme", next);
      localStorage.setItem("report-reader-theme", next);
    } catch {}
  }

  return <div className={`${styles.shell} ${!small && collapsed ? styles.collapsed : ""} ${shellClassName}`} data-app-shell="shared">
    <a className={styles.skipLink} href={`#${mainId}`} onClick={event => {
      event.preventDefault();
      document.getElementById(mainId)?.focus();
    }}>Skip to main content</a>
    {small && navOpen && <div className={styles.backdrop} onClick={() => setNavOpen(false)} aria-hidden="true" />}
    <aside ref={nav} className={`${styles.sidebar} ${small && navOpen ? styles.open : ""}`} inert={hideNav}
      role={small && navOpen ? "dialog" : undefined} aria-modal={small && navOpen ? true : undefined} aria-label={sidebarLabel}>
      <div className={styles.brand}><Icon name="report" /><strong>TrustStack</strong>{small && <button data-shell-close className={styles.iconButton} aria-label="Close navigation" onClick={() => setNavOpen(false)}><Icon name="close" /></button>}</div>
      {sidebar}
    </aside>
    <section className={styles.content} inert={small && navOpen}>
      <header className={styles.topbar}>
        <button ref={toggle} className={styles.iconButton} aria-label={hideNav ? "Open navigation" : "Collapse navigation"} aria-expanded={!hideNav} onClick={toggleNavigation}><Icon name="menu" /></button>
        <span>{sectionTitle}</span>
        <div className={styles.toolbar}>
          {toolbar}
          <button className={styles.iconButton} aria-label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"} onClick={switchTheme}><Icon name={theme === "dark" ? "sun" : "moon"} /></button>
        </div>
      </header>
      <main ref={mainRef} id={mainId} tabIndex={-1} className={`${styles.main} ${mainClassName}`}>{children}</main>
    </section>
  </div>;
}
