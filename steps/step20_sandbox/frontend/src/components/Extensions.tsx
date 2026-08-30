import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { McpServer, MemoryEntry, Provider, Rule, Skill } from "../types";

type Tab = "provider" | "mcp" | "skills" | "memory" | "rules" | "plugins";

function StatusChip({ tone, text }: { tone: "ok" | "info" | "danger"; text: string }) {
  return (
    <span className={`status-chip status-${tone}`}>
      <span className="dot" />
      {text}
    </span>
  );
}

function Providers({ providers, reload }: { providers: Provider[]; reload: () => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const [form, setForm] = useState({
    name: "",
    provider: "openai",
    base_url: "",
    model: "",
    api_key: "",
  });

  const save = async () => {
    setError("");
    if (!form.name.trim() || !form.base_url.trim() || !form.model.trim()) {
      setError("name, base URL and model are required");
      return;
    }
    try {
      await api.addProvider({
        name: form.name.trim(),
        provider: form.provider,
        base_url: form.base_url.trim(),
        model: form.model.trim(),
        api_key: form.api_key.trim() || null,
      });
    } catch (exc) {
      setError((exc as Error).message);
      return;
    }
    setForm({ name: "", provider: "openai", base_url: "", model: "", api_key: "" });
    setOpen(false);
    await reload();
  };

  return (
    <div className="ext-panel active">
      <div className="ext-toolbar">
        <p className="ic-desc" style={{ maxWidth: "46ch" }}>
          minicodex talks to one provider at a time. Add every model you use and switch the active
          one from here or from the chat input.
        </p>
        <button className="btn btn-sm" onClick={() => setOpen((v) => !v)}>
          + Add provider
        </button>
      </div>

      <div className={`add-form${open ? " open" : ""}`}>
        <div className="form-grid">
          <div className="form-row">
            <label htmlFor="p-name">Name</label>
            <input
              id="p-name"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
            />
          </div>
          <div className="form-row">
            <label htmlFor="p-kind">Kind</label>
            <select
              id="p-kind"
              value={form.provider}
              onChange={(e) => setForm({ ...form, provider: e.target.value })}
            >
              <option value="openai">openai-compatible</option>
              <option value="ollama">ollama</option>
            </select>
          </div>
        </div>
        <div className="form-grid">
          <div className="form-row">
            <label htmlFor="p-url">Base URL</label>
            <input
              id="p-url"
              placeholder="https://api.example.com/v1"
              value={form.base_url}
              onChange={(e) => setForm({ ...form, base_url: e.target.value })}
            />
          </div>
          <div className="form-row">
            <label htmlFor="p-model">Model</label>
            <input
              id="p-model"
              placeholder="e.g. gpt-4o-mini"
              value={form.model}
              onChange={(e) => setForm({ ...form, model: e.target.value })}
            />
          </div>
        </div>
        <div className="form-row">
          <label htmlFor="p-key">API key</label>
          <input
            id="p-key"
            type="password"
            placeholder="Leave blank for a local provider or to use $OPENAI_API_KEY"
            value={form.api_key}
            onChange={(e) => setForm({ ...form, api_key: e.target.value })}
          />
        </div>
        <div className="form-actions">
          <span className="form-error">{error}</span>
          <button className="btn btn-ghost btn-sm" onClick={() => setOpen(false)}>
            Cancel
          </button>
          <button className="btn btn-primary btn-sm" onClick={() => void save()}>
            Save provider
          </button>
        </div>
      </div>

      <div className="card-list">
        {providers.map((p) => (
          <div className="item-card" key={p.id}>
            <div className="ic-main">
              <div className="ic-title-row">
                <span className="ic-name">{p.name}</span>
                {p.active && <span className="ic-active-tag">Active</span>}
              </div>
              <div className="ic-meta">
                {p.model} · {p.base_url}
              </div>
            </div>
            <div className="ic-actions">
              <StatusChip
                tone="ok"
                text={
                  p.api_key
                    ? "Key set"
                    : p.provider === "ollama"
                      ? "No key needed"
                      : "Using $OPENAI_API_KEY"
                }
              />
              {!p.active && (
                <button
                  className="btn btn-ghost btn-sm"
                  onClick={async () => {
                    await api.activateProvider(p.id);
                    await reload();
                  }}
                >
                  Set active
                </button>
              )}
              <button
                className="btn btn-ghost btn-sm"
                onClick={async () => {
                  await api.deleteProvider(p.id);
                  await reload();
                }}
              >
                Remove
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function Mcp({ servers, reload }: { servers: McpServer[]; reload: () => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const [form, setForm] = useState({
    name: "",
    command: "",
    env: "",
    cwd: "",
    startup_timeout: "30",
    tool_timeout: "60",
  });

  const save = async () => {
    setError("");
    if (!form.name.trim() || !form.command.trim()) {
      setError("name and command are required");
      return;
    }
    // `KEY=value` per line -- the shape a person already knows from a shell,
    // and the shape `ServerConfig.env` wants.
    const env: Record<string, string> = {};
    for (const line of form.env.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      const eq = trimmed.indexOf("=");
      if (eq <= 0) {
        setError(`env line is not KEY=value: ${trimmed}`);
        return;
      }
      env[trimmed.slice(0, eq)] = trimmed.slice(eq + 1);
    }
    try {
      await api.addMcp({
        name: form.name.trim(),
        command: form.command.trim(),
        env,
        cwd: form.cwd.trim() || null,
        startup_timeout: Number(form.startup_timeout),
        tool_timeout: Number(form.tool_timeout),
      });
    } catch (exc) {
      setError((exc as Error).message);
      return;
    }
    setForm({ name: "", command: "", env: "", cwd: "", startup_timeout: "30", tool_timeout: "60" });
    setOpen(false);
    await reload();
  };

  return (
    <div className="ext-panel active">
      <div className="ext-toolbar">
        <p className="ic-desc" style={{ maxWidth: "46ch" }}>
          Outside tool servers minicodex can call over MCP. Each server's tools load under its own
          namespace, so two servers can both offer <code>search</code>.
        </p>
        <button className="btn btn-sm" onClick={() => setOpen((v) => !v)}>
          + Add server
        </button>
      </div>

      <div className={`add-form${open ? " open" : ""}`}>
        <div className="form-row">
          <label htmlFor="m-name">Name</label>
          <input
            id="m-name"
            placeholder="e.g. postgres"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
          />
        </div>
        <div className="form-row">
          <label htmlFor="m-cmd">Command</label>
          <input
            id="m-cmd"
            placeholder="npx @org/mcp-server-name --flag"
            value={form.command}
            onChange={(e) => setForm({ ...form, command: e.target.value })}
          />
        </div>
        {/* env / cwd / the two timeouts were readable by the backend from the
            start and settable by nobody, so every server ran with the
            defaults for ever. */}
        <div className="form-row">
          <label htmlFor="m-env">Environment</label>
          <textarea
            id="m-env"
            rows={3}
            placeholder={"KEY=value, one per line"}
            value={form.env}
            onChange={(e) => setForm({ ...form, env: e.target.value })}
          />
        </div>
        <div className="form-grid">
          <div className="form-row">
            <label htmlFor="m-cwd">Working directory</label>
            <input
              id="m-cwd"
              placeholder="optional"
              value={form.cwd}
              onChange={(e) => setForm({ ...form, cwd: e.target.value })}
            />
          </div>
          <div className="form-row">
            <label htmlFor="m-start">Startup timeout (s)</label>
            <input
              id="m-start"
              value={form.startup_timeout}
              onChange={(e) => setForm({ ...form, startup_timeout: e.target.value })}
            />
          </div>
        </div>
        <div className="form-row">
          <label htmlFor="m-tool">Tool call timeout (s)</label>
          <input
            id="m-tool"
            value={form.tool_timeout}
            onChange={(e) => setForm({ ...form, tool_timeout: e.target.value })}
          />
        </div>
        <div className="form-actions">
          <span className="form-error">{error}</span>
          <button className="btn btn-ghost btn-sm" onClick={() => setOpen(false)}>
            Cancel
          </button>
          <button className="btn btn-primary btn-sm" onClick={() => void save()}>
            Add server
          </button>
        </div>
      </div>

      <div className="card-list">
        {servers.map((m) => (
          <div className="item-card" key={m.id}>
            <div className="ic-main">
              <div className="ic-title-row">
                <span className="ic-name">{m.name}</span>
              </div>
              <div className="ic-meta">{m.command.join(" ")}</div>
            </div>
            <div className="ic-actions">
              <button
                className={`switch${m.enabled ? " on" : ""}`}
                role="switch"
                aria-checked={m.enabled}
                onClick={async () => {
                  await api.toggleMcp(m.id, !m.enabled);
                  await reload();
                }}
              >
                <span className="knob" />
              </button>
              <button
                className="btn btn-ghost btn-sm"
                onClick={async () => {
                  await api.deleteMcp(m.id);
                  await reload();
                }}
              >
                Remove
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function Skills({ threadId }: { threadId: string | null }) {
  const [data, setData] = useState<{
    directory: string;
    describe: string;
    skipped: string[];
    items: Skill[];
  } | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  useEffect(() => {
    if (!threadId) return;
    void api.skills(threadId).then(setData).catch(() => setData(null));
  }, [threadId]);

  if (!threadId) {
    return (
      <div className="ext-panel active">
        <div className="ext-empty">
          <div className="ee-title">Open a session first</div>
          Skills are discovered per workspace, so this needs to know which one.
        </div>
      </div>
    );
  }

  return (
    <div className="ext-panel active">
      <div className="ext-toolbar">
        <p className="ic-desc" style={{ maxWidth: "52ch" }}>
          A skill is a directory with a <code>SKILL.md</code> in it. Only the name and one line of
          description ride in every request; the body is fetched by the model with{" "}
          <code>read_skill</code> when it decides it wants it.
        </p>
      </div>
      <p className="ic-desc">
        <code>{data?.directory}</code> — {data?.describe}
      </p>
      {data && data.skipped.length > 0 && (
        <p className="form-error">skipped: {data.skipped.join(", ")}</p>
      )}
      <div className="card-list">
        {(data?.items ?? []).map((skill) => (
          <div className="item-card skill-card" key={skill.name}>
            <div className="ic-main">
              <div className="ic-title-row">
                <span className="ic-name">{skill.name}</span>
              </div>
              <div className="ic-meta">{skill.description}</div>
              {open === skill.name && <pre className="skill-body">{skill.body}</pre>}
            </div>
            <div className="ic-actions">
              <button
                className="btn btn-ghost btn-sm"
                onClick={() => setOpen(open === skill.name ? null : skill.name)}
              >
                {open === skill.name ? "Hide" : "Read"}
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function Memory({ threadId }: { threadId: string | null }) {
  const [data, setData] = useState<{
    directory: string;
    summary?: string;
    describe?: string;
    error?: string;
    entries: MemoryEntry[];
  } | null>(null);
  const [jobs, setJobs] = useState<Record<string, number>>({});
  const [confirming, setConfirming] = useState(false);

  const load = useCallback(async () => {
    if (!threadId) return;
    setData(await api.memory(threadId));
    setJobs((await api.memoryJobs(threadId)).counts);
  }, [threadId]);

  useEffect(() => {
    void load().catch(() => undefined);
  }, [load]);

  if (!threadId) {
    return (
      <div className="ext-panel active">
        <div className="ext-empty">
          <div className="ee-title">Open a session first</div>
          Memory lives in the workspace, so this needs to know which one.
        </div>
      </div>
    );
  }

  return (
    <div className="ext-panel active">
      <div className="ext-toolbar">
        <p className="ic-desc" style={{ maxWidth: "52ch" }}>
          What this project has learned about itself. Switch reading and writing on per session
          under <strong>Session settings</strong>; both are off until you say otherwise.
        </p>
        <button className="btn btn-danger btn-sm" onClick={() => setConfirming(true)}>
          Erase everything
        </button>
      </div>

      {confirming && (
        <div className="confirm-box">
          <div className="confirm-title">Erase all memories for this workspace?</div>
          <p>
            Deletes <code>MEMORY.md</code>, the summary and the usage counts, and clears the
            pending write queue. It cannot be undone from here.
          </p>
          <div className="ac-actions">
            <button
              className="btn btn-danger btn-sm"
              onClick={async () => {
                await api.forgetAll(threadId);
                setConfirming(false);
                await load();
              }}
            >
              Erase
            </button>
            <button className="btn btn-ghost btn-sm" onClick={() => setConfirming(false)}>
              Cancel
            </button>
          </div>
        </div>
      )}

      <p className="ic-desc">
        <code>{data?.directory}</code> — {data?.error ?? data?.describe}
      </p>
      {Object.keys(jobs).length > 0 && (
        <p className="ic-desc">
          write queue:{" "}
          {Object.entries(jobs)
            .map(([state, n]) => `${state} ${n}`)
            .join(" · ")}
        </p>
      )}

      {data?.summary && <pre className="memory-summary">{data.summary}</pre>}

      <div className="card-list">
        {(data?.entries ?? []).map((entry) => (
          <div className="item-card" key={entry.id}>
            <div className="ic-main">
              <div className="ic-title-row">
                <span className="ic-name">{entry.title}</span>
              </div>
              <div className="ic-meta">
                {entry.lines} line(s)
                {entry.usage?.uses ? ` · cited ${entry.usage.uses}x` : " · never cited"}
              </div>
            </div>
            <div className="ic-actions">
              <StatusChip tone={entry.usage?.uses ? "ok" : "info"} text={entry.id} />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function Rules({ threadId }: { threadId: string | null }) {
  const [rules, setRules] = useState<Rule[]>([]);

  const load = useCallback(async () => {
    if (!threadId) return;
    setRules(await api.rules(threadId));
  }, [threadId]);

  useEffect(() => {
    void load().catch(() => undefined);
  }, [load]);

  if (!threadId) {
    return (
      <div className="ext-panel active">
        <div className="ext-empty">
          <div className="ee-title">Open a session first</div>
          Remembered approvals belong to a workspace.
        </div>
      </div>
    );
  }

  return (
    <div className="ext-panel active">
      <div className="ext-toolbar">
        <p className="ic-desc" style={{ maxWidth: "52ch" }}>
          Every &ldquo;always allow&rdquo; you have clicked, and the command that prompted it. A
          rule you cannot find is a rule you cannot take back, which is why this tab exists at all.
        </p>
      </div>
      <div className="card-list">
        {rules.length === 0 && (
          <div className="ext-empty">
            <div className="ee-title">Nothing remembered</div>
            Approving with &ldquo;Always in this project&rdquo; puts a rule here.
          </div>
        )}
        {rules.map((rule) => (
          <div className="item-card" key={rule.index}>
            <div className="ic-main">
              <div className="ic-title-row">
                <span className="ic-name">{rule.words.join(" ")}</span>
                <span className="ic-active-tag">{rule.scope}</span>
              </div>
              <div className="ic-meta">
                prompted by: {rule.prompted_by} · {rule.created_at}
              </div>
            </div>
            <div className="ic-actions">
              <button
                className="btn btn-danger btn-sm"
                onClick={async () => {
                  await api.revokeRule(threadId, rule.index);
                  await load();
                }}
              >
                Revoke
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

interface Props {
  onBack: () => void;
  threadId: string | null;
  providers: Provider[];
  servers: McpServer[];
  reloadProviders: () => Promise<void>;
  reloadMcp: () => Promise<void>;
}

export function Extensions({
  onBack,
  threadId,
  providers,
  servers,
  reloadProviders,
  reloadMcp,
}: Props) {
  const [tab, setTab] = useState<Tab>("provider");
  const tabs: { id: Tab; label: string; count?: number | string }[] = [
    { id: "provider", label: "Providers", count: providers.length },
    { id: "mcp", label: "MCP servers", count: servers.length },
    { id: "skills", label: "Skills" },
    { id: "memory", label: "Memory" },
    { id: "rules", label: "Rules" },
    { id: "plugins", label: "Plugins", count: "--" },
  ];

  return (
    <section className="view active">
      <div className="ext-shell">
        <div className="ext-header">
          <button className="btn btn-ghost btn-sm ext-back" onClick={onBack}>
            ← Back to chat
          </button>
          <h1 className="ext-title">Extensions</h1>
          <p className="ext-sub">
            Everything minicodex can reach beyond its own code: which model answers, which outside
            tools it can call, and what it has learned to do without being told twice.
          </p>
        </div>

        <div className="ext-tabs">
          {tabs.map((t) => (
            <button
              key={t.id}
              className={`ext-tab${tab === t.id ? " active" : ""}`}
              onClick={() => setTab(t.id)}
            >
              {t.label}
              {t.count !== undefined && <span className="tab-count">{t.count}</span>}
            </button>
          ))}
        </div>

        <div className="ext-body">
          {tab === "provider" && <Providers providers={providers} reload={reloadProviders} />}
          {tab === "mcp" && <Mcp servers={servers} reload={reloadMcp} />}
          {tab === "skills" && <Skills threadId={threadId} />}
          {tab === "memory" && <Memory threadId={threadId} />}
          {tab === "rules" && <Rules threadId={threadId} />}
          {tab === "plugins" && (
            <div className="ext-panel active">
              <div className="ext-empty">
                <div className="ee-title">Not available yet</div>
                minicodex has no plugin or bundle mechanism to install from here — codex does, this
                does not, and an empty list would imply this tab had looked and found nothing.
                Reserved for when one exists.
              </div>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
