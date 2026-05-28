import { NavLink } from "react-router-dom";
import { useExplain } from "../explain/ExplainContext";
import { Explainable } from "../explain/Explainable";
import { HealthBadge } from "./HealthBadge";

export function TopBar() {
  const { enabled, toggle, setPanelOpen } = useExplain();
  return (
    <header className="topbar">
      <div className="topbar__brand">
        <span className="topbar__logo">◑</span> SyncSage
      </div>
      <nav className="topbar__nav">
        <NavLink to="/" end className={({ isActive }) => (isActive ? "navlink active" : "navlink")}>
          Graph
        </NavLink>
        <NavLink to="/sources" className={({ isActive }) => (isActive ? "navlink active" : "navlink")}>
          Sources
        </NavLink>
        <NavLink to="/config" className={({ isActive }) => (isActive ? "navlink active" : "navlink")}>
          Config
        </NavLink>
      </nav>
      <div className="topbar__right">
        <HealthBadge />
        <button className="btn btn--ghost" onClick={() => setPanelOpen(true)}>
          How it works
        </button>
        <Explainable id="explain.toggle" as="span">
          <button className={`btn ${enabled ? "btn--primary" : ""}`} onClick={toggle}>
            Explain {enabled ? "on" : "off"}
          </button>
        </Explainable>
      </div>
    </header>
  );
}
