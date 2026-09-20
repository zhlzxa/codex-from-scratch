// The shapes the backend actually sends. Written out rather than inferred,
// because the previous console and its backend disagreed about four field
// names and nothing said so -- `mark` events were emitted and dropped,
// `turn_complete.model` was sent and never read, and the skills endpoint had
// no caller at all. A type is the cheapest place for that disagreement to
// become visible.

export type SandboxMode = "read-only" | "workspace-write" | "full-access";
export type ApprovalPolicy = "never" | "on-request" | "unless-trusted";
export type StepStatus = "pending" | "in_progress" | "completed";

export interface ThreadSettings {
  sandbox_mode: SandboxMode;
  approval_policy: ApprovalPolicy;
  context_window: number | null;
  memory: boolean;
  remember: boolean;
  skills: boolean;
}

export interface Thread {
  id: string;
  workspace: string;
  title: string;
  created: number;
  settings: ThreadSettings;
  message_count?: number;
  model?: string | null;
  busy?: boolean;
}

export interface WorkspaceGroup {
  path: string;
  threads: Thread[];
}

export interface ToolCall {
  call_id: string;
  name: string;
  arguments: Record<string, unknown>;
}

export type HistoryItem =
  | { type: "user"; text: string }
  | { type: "assistant"; text?: string; tool_calls?: ToolCall[] }
  | { type: "tool_result"; call_id: string; name?: string; content: string }
  | { type: "system_note"; text: string }
  | { type: "developer_note"; text: string };

export interface PlanStep {
  text: string;
  status: StepStatus;
}

export interface Provider {
  id: string;
  name: string;
  provider: string;
  base_url: string;
  model: string;
  api_key: boolean;
  active: boolean;
}

export interface McpOAuthConfig {
  client_id: string | null;
  scope: string | null;
}

/** One `mcp.json` record. `kind` is the discriminator chapter 22 added
 * (F22-04): "stdio" carries `command`/`env`/`cwd`, "remote" carries `url` and
 * one of `bearer_token`/`oauth`. A record from before chapter 22 has no
 * `kind` at all, which is why the backend derives it rather than requiring
 * it -- the frontend only ever reads records the backend has already
 * normalised, so it can rely on `kind` being present. */
export interface McpServer {
  id: string;
  name: string;
  kind: "stdio" | "remote";
  // stdio only
  command?: string[];
  env?: Record<string, string>;
  cwd?: string | null;
  // remote only
  url?: string;
  // `true` once a token is on file; the raw value never reaches the browser.
  bearer_token?: boolean;
  http_headers?: Record<string, string>;
  env_http_headers?: Record<string, string>;
  oauth?: McpOAuthConfig | null;
  // both
  startup_timeout: number;
  tool_timeout: number;
  enabled: boolean;
}

export interface Rule {
  index: number;
  words: string[];
  scope: "session" | "project";
  prompted_by: string;
  created_at: string;
  describe: string;
}

export interface MemoryEntry {
  id: string;
  title: string;
  headline: string;
  lines: number;
  usage: { uses?: number; last_used?: string } | null;
}

export interface Skill {
  name: string;
  description: string;
  body: string;
  path: string;
  catalog_line: string;
}

/** Every event the backend can put on the socket. Exhaustive on purpose. */
export type ServerEvent =
  | { type: "turn_started"; text: string }
  | { type: "turn_finished" }
  | { type: "history_item"; item: HistoryItem }
  | { type: "mark"; mark: string; payload: Record<string, unknown> }
  | { type: "notice"; text: string }
  | { type: "error"; text: string }
  | { type: "mcp_status"; name: string; status: string; detail?: string; tools?: number }
  | { type: "plan_updated"; steps: PlanStep[]; revisions: number }
  | { type: "session_mode_changed"; from: SandboxMode; mode: SandboxMode; policy: ApprovalPolicy }
  | {
      type: "approval_request";
      id: string;
      what: string;
      reason: string;
      risk: string;
      suggested_rule: string[] | null;
    }
  | { type: "approval_resolved"; id: string; approved: boolean; remember: string | null }
  | { type: "approval_timeout"; id: string }
  | {
      type: "turn_complete";
      final_text: string;
      stop_reason: string;
      turns_used: number;
      model: string;
      plan: PlanStep[];
      reports: string[];
      transcript: string;
      session_file: string;
      citations: string[];
    };

/** Chapter 23. `password_hash` is deliberately absent: `Account.public()`
 *  never puts it in a response, and a type that admitted it would be an
 *  invitation to start reading it. */
export interface Account {
  key: string;
  admin: boolean;
  disabled: boolean;
  created: number;
  workspace_roots: string[];
  limits: Record<string, number>;
}

export interface QuotaSnapshot {
  running: number;
  concurrent_limit: number;
  used_this_hour: number;
  hourly_limit: number;
}

/** What `GET /api/auth/status` says to a caller who may be nobody.
 *
 *  `has_account` is a boolean rather than a count on purpose: the frontend
 *  needs it to choose between the bootstrap form and the login form, and
 *  "how many people use this server" is not something an anonymous caller
 *  has any business learning. */
export interface AuthStatus {
  has_account: boolean;
  signed_in: boolean;
  account: Account | null;
  quota?: QuotaSnapshot;
}

export interface BrowserSession {
  id: string;
  created: number;
  last_seen: number;
  user_agent: string;
  current: boolean;
}
