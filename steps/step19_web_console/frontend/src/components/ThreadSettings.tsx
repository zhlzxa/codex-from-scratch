import { useState } from "react";
import type { PolicyInfo } from "../api";
import type { ThreadSettings } from "../types";

interface Props {
  policy: PolicyInfo;
  value: ThreadSettings;
  onChange: (next: ThreadSettings) => void;
  disabled?: boolean;
}

/**
 * The whole 3x3 permission matrix, plus the three feature switches.
 *
 * The previous console hardcoded `workspace-write` / `on-request` in the
 * backend and showed neither in the UI, so the one thing a person most needs
 * to know before letting an agent loose in a directory -- what it may do
 * without asking -- was not on the screen at all. The options come from
 * `GET /api/policy`, which reads `policy.SANDBOX_MODES` and
 * `policy.APPROVAL_POLICIES`, so this list cannot drift from what the core
 * actually implements.
 */
export function ThreadSettingsForm({ policy, value, onChange, disabled }: Props) {
  // Memory *writing* asks twice. Not a nag: chapter 17's F17-11 is that
  // switching it on changes what every later turn does, in the background,
  // using the same quota, on transcripts the person has not re-read. codex's
  // TUI puts `Enable memories?` behind a confirmation for the same reason.
  const [confirmingRemember, setConfirmingRemember] = useState(false);

  const set = <K extends keyof ThreadSettings>(key: K, next: ThreadSettings[K]) =>
    onChange({ ...value, [key]: next });

  const modeHelp: Record<string, string> = {
    "read-only": "It may read and run commands that only read. Anything that writes gets asked.",
    "workspace-write": "It may write inside this workspace without asking. Outside it, asked.",
    "full-access": "No sandbox. Everything the server user can do, it can do.",
  };
  const policyHelp: Record<string, string> = {
    never: "Never ask. Anything the sandbox does not allow is refused outright.",
    "on-request": "Ask when the sandbox is not enough, and let the model request more.",
    "unless-trusted": "Ask for everything except commands already known to be safe.",
  };

  return (
    <div className="settings-form">
      <div className="settings-group">
        <label className="settings-label">Sandbox mode</label>
        <div className="radio-row">
          {policy.sandbox_modes.map((mode) => (
            <button
              key={mode}
              type="button"
              disabled={disabled}
              className={`radio-pill${value.sandbox_mode === mode ? " on" : ""}`}
              onClick={() => set("sandbox_mode", mode)}
            >
              {mode}
            </button>
          ))}
        </div>
        <p className="settings-help">{modeHelp[value.sandbox_mode]}</p>
      </div>

      <div className="settings-group">
        <label className="settings-label">Approval policy</label>
        <div className="radio-row">
          {policy.approval_policies.map((item) => (
            <button
              key={item}
              type="button"
              disabled={disabled}
              className={`radio-pill${value.approval_policy === item ? " on" : ""}`}
              onClick={() => set("approval_policy", item)}
            >
              {item}
            </button>
          ))}
        </div>
        <p className="settings-help">{policyHelp[value.approval_policy]}</p>
      </div>

      <div className="settings-group">
        <label className="settings-label" htmlFor="ctx">
          Context window (tokens)
        </label>
        <input
          id="ctx"
          className="settings-input"
          inputMode="numeric"
          disabled={disabled}
          placeholder="leave empty to never compact"
          value={value.context_window ?? ""}
          onChange={(e) => {
            const raw = e.target.value.trim();
            set("context_window", raw === "" ? null : Number(raw));
          }}
        />
        <p className="settings-help">
          Compaction is <strong>off</strong> without this. A long thread then grows until the
          provider refuses it rather than summarising itself (chapter 6).
        </p>
      </div>

      <div className="settings-group">
        <label className="settings-label">Features</label>

        <label className="check-row">
          <input
            type="checkbox"
            disabled={disabled}
            checked={value.skills}
            onChange={(e) => set("skills", e.target.checked)}
          />
          <span>
            <strong>Skills</strong> — put the catalog of{" "}
            <code>.minicodex/skills/*/SKILL.md</code> in every request, and let the model open one
            when it wants the body (chapter 18).
          </span>
        </label>

        <label className="check-row">
          <input
            type="checkbox"
            disabled={disabled}
            checked={value.memory}
            onChange={(e) => set("memory", e.target.checked)}
          />
          <span>
            <strong>Read memory</strong> — inject <code>.minicodex/memories</code> into every
            request (chapter 16).
          </span>
        </label>

        <label className="check-row">
          <input
            type="checkbox"
            disabled={disabled}
            checked={value.remember}
            onChange={(e) => {
              if (e.target.checked) setConfirmingRemember(true);
              else set("remember", false);
            }}
          />
          <span>
            <strong>Write memory</strong> — after each turn, a background job reads this thread's
            transcript and updates the memory files (chapter 17).
          </span>
        </label>

        {confirmingRemember && (
          <div className="confirm-box">
            <div className="confirm-title">Let it write down what it learns?</div>
            <p>
              A second model call runs beside every turn, reads the whole transcript — including
              the contents of every file the agent opened — and edits{" "}
              <code>.minicodex/memories</code>. It costs tokens you did not ask for, and it
              changes what every later turn sends.
            </p>
            <p>
              Secrets are scrubbed before anything is written, and you can erase all of it from{" "}
              <strong>Extensions → Memory</strong> at any time.
            </p>
            <div className="ac-actions">
              <button
                className="btn btn-primary btn-sm"
                onClick={() => {
                  set("remember", true);
                  setConfirmingRemember(false);
                }}
              >
                Turn it on
              </button>
              <button
                className="btn btn-ghost btn-sm"
                onClick={() => setConfirmingRemember(false)}
              >
                Cancel
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
