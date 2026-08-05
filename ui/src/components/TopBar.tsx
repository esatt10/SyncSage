import { useState } from "react";
import { NavLink } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { HealthBadge } from "./HealthBadge";
import { McpDialog } from "./McpDialog";
import { useTheme } from "../hooks/useTheme";
import { PheasantMark } from "./PheasantMark";

export function TopBar() {
  const [theme, toggleTheme] = useTheme();
  const [showMcp, setShowMcp] = useState(false);
  const overview = useQuery({
    queryKey: ["overview"],
    queryFn: api.overview,
    // Rendered once, above every page's routes, so this is what keeps a
    // source registered from the Sources page (or the Notebook rail's
    // quick-add) visibly "syncing" even after you've closed that dialog
    // and moved on — including to Settings, which has no sync UI of its
    // own. Polls only while something actually is syncing.
    refetchInterval: (query) => (query.state.data?.sources?.some((s) => s.syncing) ? 1500 : false),
  });
  const syncingSources = overview.data?.sources?.filter((s) => s.syncing) ?? [];

  return (
    <header className="topbar">
      <div className="topbar__brand">
        <span className="topbar__logo" aria-hidden>
          <PheasantMark />
        </span>
        pheasant
        {overview.data ? <span className="topbar__kb">{overview.data.name}</span> : null}
      </div>

      {syncingSources.length > 0 ? (
        <span
          className="sync-badge"
          title={`Syncing: ${syncingSources.map((s) => s.name).join(", ")} — search and chat may be slower until this finishes.`}
        >
          <span className="spinner" />
          Syncing {syncingSources.length} source{syncingSources.length === 1 ? "" : "s"}…
        </span>
      ) : null}

      <nav className="topbar__nav">
        <NavLink to="/" end className={({ isActive }) => (isActive ? "navlink active" : "navlink")}>
          Notebook
        </NavLink>
        <NavLink
          to="/sources"
          className={({ isActive }) => (isActive ? "navlink active" : "navlink")}
        >
          Sources
        </NavLink>
        <NavLink
          to="/config"
          className={({ isActive }) => (isActive ? "navlink active" : "navlink")}
        >
          Settings
        </NavLink>
      </nav>

      <div className="topbar__right">
        <HealthBadge />
        <button className="btn btn--small" onClick={() => setShowMcp(true)}>
          Connect agent
        </button>
        <button
          className="btn btn--icon"
          onClick={toggleTheme}
          title={theme === "dark" ? "Switch to light" : "Switch to dark"}
          aria-label="Toggle theme"
        >
          {theme === "dark" ? "☀" : "☾"}
        </button>
      </div>

      {showMcp ? <McpDialog onClose={() => setShowMcp(false)} /> : null}
    </header>
  );
}
