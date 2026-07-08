import { useEffect, useState } from "react";
import { NavLink, Link } from "react-router-dom";

export default function Sidebar() {
  const [tree, setTree] = useState(null);

  useEffect(() => {
    fetch("/api/docs/tree")
      .then((r) => r.json())
      .then(setTree);
  }, []);

  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        <h1>CodeDoc</h1>
        <p>Auto-generated documentation</p>
      </div>

      <nav className="sidebar-nav">
        <div className="nav-section">
          <div className="nav-section-title">Overview</div>
          <NavLink to="/" end className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}>
            <span className="icon">&#9776;</span>
            <span className="label">Index</span>
          </NavLink>
        </div>

        {tree && tree.topics.length > 0 && (
          <div className="nav-section">
            <div className="nav-section-title">Modules</div>
            {tree.topics.map((t) => (
              <NavLink
                key={t}
                to={`/topic/${encodeURIComponent(t)}`}
                className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}
              >
                <span className="icon">&#9670;</span>
                <span className="label">{t}</span>
              </NavLink>
            ))}
          </div>
        )}

        {tree && tree.summaries.files.length > 0 && (
          <div className="nav-section">
            <div className="nav-section-title">File Summaries</div>
            {tree.summaries.files.map((f) => (
              <NavLink
                key={f}
                to={`/summary/files/${encodeURIComponent(f)}`}
                className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}
              >
                <span className="icon">&#128196;</span>
                <span className="label">{f.replace(/_/g, "/").replace(/\.md$/, "")}</span>
              </NavLink>
            ))}
          </div>
        )}

        {tree && tree.summaries.dirs.length > 0 && (
          <div className="nav-section">
            <div className="nav-section-title">Directory Summaries</div>
            {tree.summaries.dirs.map((d) => (
              <NavLink
                key={d}
                to={`/summary/dirs/${encodeURIComponent(d)}`}
                className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}
              >
                <span className="icon">&#128193;</span>
                <span className="label">{d.replace(/_/g, "/")}</span>
              </NavLink>
            ))}
          </div>
        )}
      </nav>

      <div className="sidebar-footer">
        <Link to="/chat" className="chat-nav-btn">
          &#128172; Ask about this codebase
        </Link>
      </div>
    </aside>
  );
}
