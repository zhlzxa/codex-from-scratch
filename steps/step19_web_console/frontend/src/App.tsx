import { useCallback, useEffect, useRef, useState } from "react";
import { api, type PolicyInfo } from "./api";
import { Chat, PlanPanel } from "./components/Chat";
import { Extensions } from "./components/Extensions";
import { Sidebar } from "./components/Sidebar";
import { ThreadSettingsForm } from "./components/ThreadSettings";
import type { McpServer, Provider, Thread, ThreadSettings, WorkspaceGroup } from "./types";
import { useThread } from "./useThread";

export default function App() {
  const [policy, setPolicy] = useState<PolicyInfo | null>(null);
  const [workspaces, setWorkspaces] = useState<WorkspaceGroup[]>([]);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [servers, setServers] = useState<McpServer[]>([]);
  const [threadId, setThreadId] = useState<string | null>(null);
  const [thread, setThread] = useState<Thread | null>(null);
  const [view, setView] = useState<"chat" | "extensions">("chat");
  const [showSettings, setShowSettings] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [modelMenu, setModelMenu] = useState(false);
  const [booted, setBooted] = useState(false);

  const live = useThread(threadId);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const reloadWorkspaces = useCallback(async () => {
    setWorkspaces(await api.workspaces());
  }, []);
  const reloadProviders = useCallback(async () => {
    setProviders(await api.providers());
  }, []);
  const reloadMcp = useCallback(async () => {
    setServers(await api.mcp());
  }, []);

  useEffect(() => {
    void (async () => {
      const [pol, spaces, provs, mcp] = await Promise.all([
        api.policy(),
        api.workspaces(),
        api.providers(),
        api.mcp(),
      ]);
      setPolicy(pol);
      setWorkspaces(spaces);
      setProviders(provs);
      setServers(mcp);
      // Land somewhere useful rather than on an empty chat.
      const first = spaces.find((w) => w.threads.length > 0)?.threads[0];
      if (first) setThreadId(first.id);
      setBooted(true);
    })().catch(() => setBooted(true));
  }, []);

  useEffect(() => {
    if (!threadId) {
      setThread(null);
      return;
    }
    void api.thread(threadId).then(setThread).catch(() => setThread(null));
  }, [threadId]);

  // The sidebar's per-thread metadata (item counts, busy dots) is derived from
  // files the turn writes, so it is refreshed when a turn ends rather than on
  // a timer.
  useEffect(() => {
    if (!live.busy) void reloadWorkspaces().catch(() => undefined);
  }, [live.busy, reloadWorkspaces]);

  const active = providers.find((p) => p.active) ?? null;
  const ready = Boolean(active && threadId);

  const openThread = (id: string) => {
    setThreadId(id);
    setView("chat");
    setShowSettings(false);
    setSidebarOpen(false);
  };

  const newSession = async (workspace: string) => {
    const created = await api.createThread(workspace, "New session", policy?.defaults ?? {});
    await reloadWorkspaces();
    openThread(created.id);
  };

  const send = async () => {
    const text = draft.trim();
    if (!text || !ready || live.busy) return;
    setDraft("");
    if (textareaRef.current) textareaRef.current.style.height = "auto";
    await live.send(text);
  };

  const saveSettings = async (next: ThreadSettings) => {
    if (!threadId) return;
    const updated = await api.patchThread(threadId, { settings: next });
    setThread(updated);
    await reloadWorkspaces();
  };

  if (!booted) return <div className="boot">starting…</div>;

  if (view === "extensions") {
    return (
      <div className="app">
        <Sidebar
          workspaces={workspaces}
          currentThreadId={threadId}
          onOpenThread={openThread}
          onAddWorkspace={newSession}
          onNewSession={newSession}
          onOpenExtensions={() => setView("extensions")}
          open={sidebarOpen}
        />
        <main className="main">
          <Extensions
            onBack={() => setView("chat")}
            threadId={threadId}
            providers={providers}
            servers={servers}
            reloadProviders={reloadProviders}
            reloadMcp={reloadMcp}
          />
        </main>
      </div>
    );
  }

  return (
    <div className="app">
      {sidebarOpen && <div className="scrim show" onClick={() => setSidebarOpen(false)} />}
      <Sidebar
        workspaces={workspaces}
        currentThreadId={threadId}
        onOpenThread={openThread}
        onAddWorkspace={newSession}
        onNewSession={newSession}
        onOpenExtensions={() => setView("extensions")}
        open={sidebarOpen}
      />

      <main className="main">
        <div className="mobile-bar">
          <button className="hamburger" aria-label="Open sidebar" onClick={() => setSidebarOpen(true)}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M3 6h18M3 12h18M3 18h18" />
            </svg>
          </button>
          <span className="mb-title">minicodex</span>
        </div>

        <section className="view active">
          <div className="chat-header">
            <div>
              <div className="chat-title">{thread?.title ?? "No session open"}</div>
              <div className="chat-path">{thread?.workspace ?? "Add a workspace to start"}</div>
            </div>
            {thread && (
              <div className="chat-header-right">
                {/* The permission state, permanently on screen. `live.mode`
                    tracks a mid-turn `request_permissions` escalation, so this
                    shows what is true now, not what was chosen at creation. */}
                <span className={`mode-chip mode-${live.mode ?? thread.settings.sandbox_mode}`}>
                  {live.mode ?? thread.settings.sandbox_mode} · {thread.settings.approval_policy}
                </span>
                <span className={`conn-chip conn-${live.connection}`} title={live.connection}>
                  {live.connection === "open" ? "live" : live.connection}
                </span>
                <button className="btn btn-ghost btn-sm" onClick={() => setShowSettings((v) => !v)}>
                  Session settings
                </button>
              </div>
            )}
          </div>

          {showSettings && thread && policy && (
            <div className="settings-drawer">
              <ThreadSettingsForm
                policy={policy}
                value={thread.settings}
                disabled={live.busy}
                onChange={(next) => void saveSettings(next)}
              />
              {live.busy && (
                <p className="settings-help">
                  Settings are read once, when a turn starts. They are locked while this one runs.
                </p>
              )}
            </div>
          )}

          <div className="chat-body">
            <Chat
              entries={live.entries}
              results={live.results}
              reports={live.reports}
              onRespond={live.respond}
              empty={
                threadId
                  ? "This session has no messages yet. Say what you want done."
                  : "Add a workspace on the left, then send the first message. minicodex will show its work as it goes -- tool calls, results, and anything it needs to ask you about."
              }
            />
            <PlanPanel steps={live.plan} />
          </div>

          <div className="composer">
            <div className="composer-col">
              {modelMenu && (
                <div className="popover popover-right show">
                  <div className="pop-label">Switch model</div>
                  <div>
                    {providers.map((p) => (
                      <button
                        key={p.id}
                        className={`pop-row${p.active ? " on" : ""}`}
                        onClick={async () => {
                          await api.activateProvider(p.id);
                          await reloadProviders();
                          setModelMenu(false);
                        }}
                      >
                        <span>{p.model}</span>
                        <span className="pop-sub">{p.name}</span>
                      </button>
                    ))}
                  </div>
                </div>
              )}

              <div className={`composer-box${live.busy ? " disabled" : ""}`}>
                <textarea
                  ref={textareaRef}
                  rows={1}
                  disabled={!ready}
                  value={draft}
                  placeholder="Ask minicodex to fix something. It will show its work."
                  onChange={(e) => {
                    setDraft(e.target.value);
                    e.target.style.height = "auto";
                    e.target.style.height = `${Math.min(e.target.scrollHeight, 160)}px`;
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      void send();
                    }
                  }}
                />
                <button
                  className="model-btn"
                  aria-expanded={modelMenu}
                  onClick={() => setModelMenu((v) => !v)}
                >
                  <span>{active ? active.model : "no provider"}</span>
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M6 9l6 6 6-6" />
                  </svg>
                </button>
                {live.busy ? (
                  <button className="btn btn-danger btn-sm" onClick={() => void live.stop()}>
                    Stop
                  </button>
                ) : (
                  <button
                    className="btn btn-primary btn-sm"
                    disabled={!ready || !draft.trim()}
                    onClick={() => void send()}
                  >
                    Send
                  </button>
                )}
              </div>

              <div className="composer-meta">
                <span>Shift+Enter for a new line</span>
                <span className={live.error ? "danger" : ""}>
                  {live.error ?? (live.busy ? "thinking…" : "")}
                </span>
              </div>
            </div>
          </div>
        </section>
      </main>
    </div>
  );
}
