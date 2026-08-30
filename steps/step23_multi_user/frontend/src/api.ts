import type {
  Account,
  ApprovalPolicy,
  AuthStatus,
  BrowserSession,
  McpServer,
  MemoryEntry,
  Provider,
  Rule,
  SandboxMode,
  Skill,
  Thread,
  ThreadSettings,
  WorkspaceGroup,
} from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

/**
 * What to do when the server says nobody is signed in.
 *
 * A session ends on its own -- twelve hours from sign-in, two hours idle
 * (`sessions.py`) -- so this is not an edge case, it is what happens to
 * anybody who leaves a tab open over lunch. Without this hook the console
 * shows a page full of stale data and every button reports an error; with it,
 * the app drops back to the sign-in screen at the first 401.
 *
 * A module-level callback rather than a React context because `call` is not a
 * component and never will be, and threading a setter through every one of
 * these thirty methods to reach one `if` is not an abstraction, it is a tax.
 */
let onUnauthorized: (() => void) | null = null;

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler;
}

/** The three routes that answer without a session; a 401 from one of them is
 *  a wrong password, not an expired session, and must not bounce the app. */
const PUBLIC_PATHS = new Set(["/api/auth/status", "/api/auth/login", "/api/auth/bootstrap"]);

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 401 && !PUBLIC_PATHS.has(path)) onUnauthorized?.();
  if (!res.ok) {
    // FastAPI puts the message in `detail`. Reading it rather than showing
    // "500" is the difference between "no such directory: /tpm/foo" and a
    // number the person cannot act on.
    let detail = res.statusText;
    try {
      const payload = await res.json();
      if (payload && typeof payload.detail === "string") detail = payload.detail;
    } catch {
      /* not JSON; keep the status text */
    }
    throw new ApiError(detail, res.status);
  }
  if (res.status === 204 || res.status === 202) return undefined as T;
  return (await res.json()) as T;
}

export interface PolicyInfo {
  sandbox_modes: SandboxMode[];
  approval_policies: ApprovalPolicy[];
  defaults: ThreadSettings;
  tables: Record<string, string[]>;
}

export const api = {
  authStatus: () => call<AuthStatus>("GET", "/api/auth/status"),
  bootstrap: (token: string, key: string, password: string) =>
    call<{ account: Account }>("POST", "/api/auth/bootstrap", { token, key, password }),
  login: (key: string, password: string) =>
    call<{ account: Account }>("POST", "/api/auth/login", { key, password }),
  logout: () => call<{ ok: boolean }>("POST", "/api/auth/logout"),
  mySessions: () => call<BrowserSession[]>("GET", "/api/auth/sessions"),
  revokeSession: (id: string) => call<{ ok: boolean }>("DELETE", `/api/auth/sessions/${id}`),
  revokeAllSessions: () =>
    call<{ sessions: number; turns: number }>("POST", "/api/auth/sessions/revoke-all"),
  changePassword: (current: string, replacement: string) =>
    call<{ account: Account }>("POST", "/api/auth/password", { current, replacement }),

  accounts: () => call<Account[]>("GET", "/api/accounts"),
  addAccount: (body: {
    key: string;
    password: string;
    admin: boolean;
    workspace_roots: string[];
  }) => call<Account>("POST", "/api/accounts", body),
  disableAccount: (key: string) => call<Account>("POST", `/api/accounts/${key}/disable`),

  policy: () => call<PolicyInfo>("GET", "/api/policy"),
  tools: () => call<{ name: string; description: string }[]>("GET", "/api/tools"),

  workspaces: () => call<WorkspaceGroup[]>("GET", "/api/workspaces"),
  thread: (id: string) =>
    call<Thread & { items: unknown[]; marks: unknown[]; busy: boolean; turns: number }>(
      "GET",
      `/api/threads/${id}`,
    ),
  createThread: (workspace: string, title: string, settings: Partial<ThreadSettings>) =>
    call<Thread>("POST", "/api/threads", { workspace, title, settings }),
  patchThread: (id: string, patch: { title?: string; settings?: Partial<ThreadSettings> }) =>
    call<Thread>("PATCH", `/api/threads/${id}`, patch),
  deleteThread: (id: string) => call<{ ok: boolean }>("DELETE", `/api/threads/${id}`),
  sessions: (id: string) =>
    call<
      { session_id: string; created: number; model: string; messages: number }[]
    >("GET", `/api/threads/${id}/sessions`),
  fork: (id: string, session: string, upto: number | null) =>
    call<Thread>("POST", `/api/threads/${id}/fork`, { session, upto }),

  send: (id: string, text: string) =>
    call<void>("POST", `/api/threads/${id}/messages`, { text }),
  cancel: (id: string) => call<{ ok: boolean }>("POST", `/api/threads/${id}/cancel`),

  respond: (
    approvalId: string,
    approved: boolean,
    command: string,
    remember: "session" | "project" | null,
  ) => call<{ ok: boolean }>("POST", `/api/approvals/${approvalId}/respond`, {
    approved,
    command,
    remember,
  }),

  rules: (id: string) => call<Rule[]>("GET", `/api/threads/${id}/rules`),
  revokeRule: (id: string, index: number) =>
    call<{ revoked: string }>("DELETE", `/api/threads/${id}/rules/${index}`),

  memory: (id: string) =>
    call<{
      directory: string;
      summary?: string;
      describe?: string;
      error?: string;
      entries: MemoryEntry[];
    }>("GET", `/api/threads/${id}/memory`),
  memoryJobs: (id: string) =>
    call<{ counts: Record<string, number>; rows: unknown[] }>(
      "GET",
      `/api/threads/${id}/memory/jobs`,
    ),
  forgetAll: (id: string) =>
    call<{ removed: string[] }>("POST", `/api/threads/${id}/memory/forget-all`),

  skills: (id: string) =>
    call<{
      supported: boolean;
      directory: string;
      describe: string;
      skipped: string[];
      items: Skill[];
    }>("GET", `/api/threads/${id}/skills`),
  plugins: () => call<{ supported: boolean; items: unknown[] }>("GET", "/api/plugins"),

  providers: () => call<Provider[]>("GET", "/api/providers"),
  addProvider: (body: Omit<Provider, "id" | "active" | "api_key"> & { api_key: string | null }) =>
    call<Provider>("POST", "/api/providers", body),
  activateProvider: (id: string) =>
    call<{ ok: boolean }>("POST", `/api/providers/${id}/activate`),
  deleteProvider: (id: string) => call<{ ok: boolean }>("DELETE", `/api/providers/${id}`),

  mcp: () => call<McpServer[]>("GET", "/api/mcp"),
  addMcp: (body: {
    name: string;
    command?: string;
    env?: Record<string, string>;
    cwd?: string | null;
    url?: string;
    bearer_token?: string;
    oauth?: boolean;
    oauth_client_id?: string;
    oauth_scope?: string;
    startup_timeout: number;
    tool_timeout: number;
  }) => call<McpServer>("POST", "/api/mcp", body),
  toggleMcp: (id: string, enabled: boolean) =>
    call<McpServer>("POST", `/api/mcp/${id}/toggle`, { enabled }),
  deleteMcp: (id: string) => call<{ ok: boolean }>("DELETE", `/api/mcp/${id}`),

  startMcpOauth: (serverId: string) =>
    call<{ authorize_url: string }>("POST", "/api/mcp/oauth/start", { server_id: serverId }),
  mcpOauthStatus: (serverId: string) =>
    call<{ connected: boolean }>("GET", `/api/mcp/${serverId}/oauth/status`),
};
