import { useState } from "react";
import type { WorkspaceGroup } from "../types";

interface Props {
  workspaces: WorkspaceGroup[];
  currentThreadId: string | null;
  onOpenThread: (id: string) => void;
  onAddWorkspace: (path: string) => Promise<void>;
  onNewSession: (path: string) => Promise<void>;
  onOpenExtensions: () => void;
  open: boolean;
}

/**
 * Sessions grouped by working directory, not a flat list.
 *
 * Two separate `+` buttons, deliberately: the one next to the "Workspaces"
 * label adds a workspace, the one on each group header starts a session in
 * *that* workspace. Collapsing them into one button makes the common action
 * ("another session here") ambiguous with the rare one.
 */
export function Sidebar({
  workspaces,
  currentThreadId,
  onOpenThread,
  onAddWorkspace,
  onNewSession,
  onOpenExtensions,
  open,
}: Props) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [adding, setAdding] = useState(false);
  const [path, setPath] = useState("");
  const [error, setError] = useState("");

  const submit = async () => {
    const trimmed = path.trim();
    if (!trimmed) return;
    try {
      await onAddWorkspace(trimmed);
      setPath("");
      setAdding(false);
      setError("");
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  return (
    <aside className={`sidebar${open ? " open" : ""}`}>
      <div className="brand">
        <span className="brand-mark" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none">
            <path
              d="M4 12L10 6M4 12L10 18M20 12L14 6M20 12L14 18"
              stroke="#FFFFFF"
              strokeWidth="2.4"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </span>
        <div>
          <div className="brand-word">minicodex</div>
          <div className="brand-tag">console · live</div>
        </div>
      </div>

      <div className="sidebar-section" style={{ flex: 1, minHeight: 0 }}>
        <div className="sidebar-label-row">
          <span className="sidebar-label">Workspaces</span>
          <button
            className="ws-add"
            aria-label="Add workspace"
            title="Add workspace"
            onClick={() => setAdding((v) => !v)}
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M12 5v14M5 12h14" />
            </svg>
          </button>
        </div>

        <div className={`ws-add-form${adding ? " open" : ""}`}>
          <input
            value={path}
            spellCheck={false}
            placeholder="/absolute/path/to/project"
            onChange={(e) => setPath(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                void submit();
              }
            }}
          />
          <button className="btn btn-primary btn-sm" onClick={() => void submit()}>
            Add
          </button>
        </div>
        {error && <div className="sidebar-error">{error}</div>}

        <div>
          {workspaces.length === 0 && (
            <div className="sidebar-empty">No workspaces yet -- click + above to add one.</div>
          )}
          {workspaces.map((ws) => {
            const isOpen = expanded[ws.path] !== false;
            return (
              <div className="ws-group" key={ws.path}>
                <div className="ws-header">
                  <button
                    className="ws-toggle"
                    aria-expanded={isOpen}
                    onClick={() => setExpanded((c) => ({ ...c, [ws.path]: !isOpen }))}
                  >
                    <svg
                      className={`ws-chevron${isOpen ? " open" : ""}`}
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2.2"
                    >
                      <path d="M9 6l6 6-6 6" />
                    </svg>
                    <svg
                      className="ws-folder"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.6"
                    >
                      <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" />
                    </svg>
                    <span className="ws-path">{ws.path}</span>
                  </button>
                  <button
                    className="ws-add"
                    title="New session here"
                    onClick={(e) => {
                      e.stopPropagation();
                      setExpanded((c) => ({ ...c, [ws.path]: true }));
                      void onNewSession(ws.path);
                    }}
                  >
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                      <path d="M12 5v14M5 12h14" />
                    </svg>
                  </button>
                </div>
                <ul className="thread-list" style={{ display: isOpen ? "flex" : "none" }}>
                  {ws.threads.map((t) => (
                    <li
                      key={t.id}
                      className={`thread-item${t.id === currentThreadId ? " active" : ""}`}
                      onClick={() => onOpenThread(t.id)}
                    >
                      <span className="t-title">
                        {t.title}
                        {t.busy && <span className="t-busy" title="answering" />}
                      </span>
                      <span className="t-meta">
                        {t.model ? `${t.model} · ${t.message_count} items` : "no messages yet"}
                        {" · "}
                        {t.settings.sandbox_mode}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            );
          })}
        </div>
      </div>

      <div className="sidebar-footer">
        <button className="nav-item" onClick={onOpenExtensions}>
          <span className="n-label">
            <svg
              className="n-icon"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
            >
              <path d="M12 3v6M12 15v6M3 12h6M15 12h6" />
              <circle cx="12" cy="12" r="2.6" />
            </svg>
            Extensions
          </span>
        </button>
      </div>
    </aside>
  );
}
