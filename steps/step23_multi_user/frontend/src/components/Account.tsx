import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../api";
import type { Account, AuthStatus, BrowserSession } from "../types";

function when(seconds: number): string {
  return new Date(seconds * 1000).toLocaleString();
}

/**
 * Everything about the person using this console, on one tab.
 *
 * Three things, and each of them exists because the alternative is a thing
 * only the operator can do:
 *
 * - **the signed-in devices**, so a session that was opened somewhere you no
 *   longer trust can be ended from here rather than by restarting the server;
 * - **the password**, with the note that changing it signs every other browser
 *   out, because that is what a password change is for;
 * - **the accounts**, for whoever bootstrapped the console.
 *
 * The quota is shown rather than hidden until it bites. A limit a person only
 * discovers as a 429 is a limit they experience as a bug.
 */
export function AccountPanel({ status, onSignedOut }: { status: AuthStatus; onSignedOut: () => void }) {
  const [sessions, setSessions] = useState<BrowserSession[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [error, setError] = useState("");
  const account = status.account;

  const reload = useCallback(async () => {
    setSessions(await api.mySessions());
    if (account?.admin) setAccounts(await api.accounts());
  }, [account?.admin]);

  useEffect(() => {
    void reload().catch((exc) => setError(String(exc)));
  }, [reload]);

  if (!account) return null;

  async function guard(work: () => Promise<void>) {
    setError("");
    try {
      await work();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    }
  }

  return (
    <div className="ext-panel active">
      <div className="ext-toolbar">
        <p className="ic-desc" style={{ maxWidth: "46ch" }}>
          Signed in as <strong>{account.key}</strong>
          {account.admin && " (administrator)"}. Threads, providers, MCP servers, memories and
          OAuth tokens all belong to this account and are not visible to any other.
        </p>
        <button className="btn btn-sm" onClick={() => void guard(async () => {
          await api.logout();
          onSignedOut();
        })}>
          Sign out
        </button>
      </div>

      {error && <p className="form-error">{error}</p>}

      {status.quota && (
        <div className="card-list">
          <div className="item-card">
            <div className="ic-main">
              <div className="ic-title-row">
                <span className="ic-name">Usage</span>
              </div>
              <div className="ic-meta">
                {status.quota.running} of {status.quota.concurrent_limit} turns running &middot;{" "}
                {status.quota.used_this_hour} of {status.quota.hourly_limit} started this hour
              </div>
              <p className="settings-help">
                Charged when a turn is claimed, not when it finishes: a turn that fails costs the
                same model call as one that succeeds.
              </p>
            </div>
          </div>
        </div>
      )}

      <h3 className="settings-label" style={{ marginTop: 18 }}>
        Signed-in browsers
      </h3>
      <div className="card-list">
        {sessions.map((s) => (
          <div className="item-card" key={s.id}>
            <div className="ic-main">
              <div className="ic-title-row">
                <span className="ic-name">{s.user_agent || "unknown browser"}</span>
                {s.current && <span className="ic-active-tag">This one</span>}
              </div>
              <div className="ic-meta">
                started {when(s.created)} &middot; last seen {when(s.last_seen)}
              </div>
            </div>
            <div className="ic-actions">
              {!s.current && (
                <button
                  className="btn btn-ghost btn-sm"
                  onClick={() =>
                    void guard(async () => {
                      await api.revokeSession(s.id);
                      await reload();
                    })
                  }
                >
                  Sign out
                </button>
              )}
            </div>
          </div>
        ))}
      </div>
      <p className="settings-help">
        Sessions live in the server process, so restarting it signs everybody out. Signing out
        everywhere also cancels any turn this account left running &mdash; revoking access that
        leaves work running has not revoked anything.
      </p>
      <button
        className="btn btn-ghost btn-sm"
        onClick={() =>
          void guard(async () => {
            await api.revokeAllSessions();
            onSignedOut();
          })
        }
      >
        Sign out everywhere
      </button>

      <ChangePassword onChanged={() => void reload()} />

      {account.admin && <Accounts accounts={accounts} onChanged={() => void reload()} />}
    </div>
  );
}

function ChangePassword({ onChanged }: { onChanged: () => void }) {
  const [current, setCurrent] = useState("");
  const [replacement, setReplacement] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState("");

  return (
    <div className="add-form open" style={{ marginTop: 18 }}>
      <h3 className="settings-label">Change password</h3>
      <div className="form-grid">
        <div className="form-row">
          <span className="settings-label">Current</span>
          <input
            className="settings-input"
            type="password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
            autoComplete="current-password"
          />
        </div>
        <div className="form-row">
          <span className="settings-label">New</span>
          <input
            className="settings-input"
            type="password"
            value={replacement}
            onChange={(e) => setReplacement(e.target.value)}
            autoComplete="new-password"
          />
        </div>
      </div>
      <p className="settings-help">
        Every other signed-in browser is signed out. This one stays, on a new session.
      </p>
      <div className="form-actions">
        <span className="form-error">{error}</span>
        {note && <span className="ic-meta">{note}</span>}
        <button
          className="btn btn-primary btn-sm"
          onClick={() =>
            void (async () => {
              setError("");
              setNote("");
              try {
                await api.changePassword(current, replacement);
                setCurrent("");
                setReplacement("");
                setNote("changed; other browsers signed out");
                onChanged();
              } catch (exc) {
                setError(exc instanceof ApiError ? exc.message : String(exc));
              }
            })()
          }
        >
          Change it
        </button>
      </div>
    </div>
  );
}

function Accounts({ accounts, onChanged }: { accounts: Account[]; onChanged: () => void }) {
  const [key, setKey] = useState("");
  const [password, setPassword] = useState("");
  const [roots, setRoots] = useState("");
  const [error, setError] = useState("");

  return (
    <div style={{ marginTop: 22 }}>
      <h3 className="settings-label">Accounts</h3>
      <div className="card-list">
        {accounts.map((a) => (
          <div className="item-card" key={a.key}>
            <div className="ic-main">
              <div className="ic-title-row">
                <span className="ic-name">{a.key}</span>
                {a.admin && <span className="ic-active-tag">admin</span>}
                {a.disabled && <span className="status-chip status-bad">disabled</span>}
              </div>
              <div className="ic-meta">
                {a.workspace_roots.length
                  ? `may open: ${a.workspace_roots.join(", ")}`
                  : "may open any directory on this server"}
              </div>
            </div>
            <div className="ic-actions">
              {!a.disabled && (
                <button
                  className="btn btn-ghost btn-sm"
                  onClick={() =>
                    void (async () => {
                      setError("");
                      try {
                        await api.disableAccount(a.key);
                        onChanged();
                      } catch (exc) {
                        setError(exc instanceof ApiError ? exc.message : String(exc));
                      }
                    })()
                  }
                >
                  Disable
                </button>
              )}
            </div>
          </div>
        ))}
      </div>

      <div className="add-form open">
        <div className="form-grid">
          <div className="form-row">
            <span className="settings-label">Account name</span>
            <input
              className="settings-input"
              value={key}
              onChange={(e) => setKey(e.target.value)}
              spellCheck={false}
            />
          </div>
          <div className="form-row">
            <span className="settings-label">Password</span>
            <input
              className="settings-input"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="new-password"
            />
          </div>
        </div>
        <div className="form-row">
          <span className="settings-label">Directories this account may open</span>
          <input
            className="settings-input"
            value={roots}
            onChange={(e) => setRoots(e.target.value)}
            spellCheck={false}
            placeholder="/srv/work/bob    (comma separated; empty means anywhere)"
          />
        </div>
        <p className="settings-help">
          Leave it empty only for an account you would give a shell to. A workspace is any absolute
          path on this server, and the agent&rsquo;s whole job is to read files and run commands
          there &mdash; so this, not the tenancy underneath it, is what keeps two accounts out of
          each other&rsquo;s directories.
        </p>
        <div className="form-actions">
          <span className="form-error">{error}</span>
          <button
            className="btn btn-primary btn-sm"
            onClick={() =>
              void (async () => {
                setError("");
                try {
                  await api.addAccount({
                    key: key.trim(),
                    password,
                    admin: false,
                    workspace_roots: roots
                      .split(",")
                      .map((r) => r.trim())
                      .filter(Boolean),
                  });
                  setKey("");
                  setPassword("");
                  setRoots("");
                  onChanged();
                } catch (exc) {
                  setError(exc instanceof ApiError ? exc.message : String(exc));
                }
              })()
            }
          >
            Create account
          </button>
        </div>
      </div>
    </div>
  );
}
