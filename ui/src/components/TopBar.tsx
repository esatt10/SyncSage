import { useState } from "react";
import { NavLink } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { HealthBadge } from "./HealthBadge";
import { McpDialog } from "./McpDialog";
import { useTheme } from "../hooks/useTheme";

export function TopBar() {
  const [theme, toggleTheme] = useTheme();
  const [showMcp, setShowMcp] = useState(false);
  const overview = useQuery({ queryKey: ["overview"], queryFn: api.overview });

  return (
    <header className="topbar">
      <div className="topbar__brand">
        <span className="topbar__logo" aria-hidden>
          S
        </span>
        SyncSage
        {overview.data ? <span className="topbar__kb">{overview.data.name}</span> : null}
      </div>

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
