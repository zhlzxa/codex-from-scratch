import { useState } from "react";
import { ApiError, api } from "../api";
import type { AuthStatus } from "../types";

/**
 * The first screen, and for a fresh console the only one there is.
 *
 * Two forms, chosen by `has_account` -- which is the whole reason
 * `GET /api/auth/status` is public. A console nobody has claimed yet shows the
 * bootstrap form and asks for the token the server printed on startup; a
 * console with an account shows the login form.
 *
 * The bootstrap form says where the token comes from, because "paste the
 * one-time token" is not a helpful instruction to somebody who did not read
 * the terminal they started the server in.
 */
export function SignIn({ status, onSignedIn }: { status: AuthStatus; onSignedIn: () => void }) {
  const [key, setKey] = useState("");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const first = !status.has_account;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      if (first) await api.bootstrap(token.trim(), key.trim(), password);
      else await api.login(key.trim(), password);
      onSignedIn();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setBusy(false);
      setPassword("");
    }
  }

  return (
    <div className="signin-screen">
      <form className="signin-card" onSubmit={(e) => void submit(e)}>
        <h1 className="signin-title">minicodex</h1>
        <p className="signin-lede">
          {first
            ? "Nobody has claimed this console yet. The terminal running it printed a one-time token; paste it here to create the first account."
            : "Sign in to the console."}
        </p>

        {first && (
          <label className="form-row">
            <span className="settings-label">One-time token</span>
            <input
              className="settings-input"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              autoComplete="off"
              spellCheck={false}
              placeholder="printed by minicodex serve"
            />
          </label>
        )}

        <label className="form-row">
          <span className="settings-label">Account name</span>
          <input
            className="settings-input"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            autoComplete="username"
            spellCheck={false}
            placeholder="lower case, digits, - and _"
          />
        </label>

        <label className="form-row">
          <span className="settings-label">Password</span>
          <input
            className="settings-input"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete={first ? "new-password" : "current-password"}
          />
        </label>

        {first && (
          <p className="settings-help">
            At least 10 characters. Length only &mdash; no character classes, which push people
            towards <code>Passw0rd!</code> and are worth less than four more characters.
          </p>
        )}

        {error && <p className="form-error">{error}</p>}

        <button className="btn btn-primary" type="submit" disabled={busy}>
          {busy ? "Working…" : first ? "Create this account" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
