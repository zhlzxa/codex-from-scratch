// Driving one MCP OAuth connection from the browser side.
//
// There is no `window.opener` handshake here on purpose. `window.open(url,
// "_blank", "noopener")` is what the component below actually calls -- and
// `noopener` is not incidental: it also means the new tab has no reference
// back to open a `postMessage` channel with, so relying on one would mean
// choosing a mechanism that only works when a *different*, less safe, call is
// made. Polling `GET /api/mcp/{id}/oauth/status` instead survives a popup
// blocker, a closed tab, and a page reload equally, and it is what the
// backend's own callback route (`routes.py`) was written to make sufficient:
// its whole job is to make the token show up there, not to talk back to a
// window it cannot be sure still exists.
//
// Written as one plain async function with every side effect injected,
// rather than a hook, so `mcpOauth.test.ts` can drive it with fakes and a
// zero-delay `sleep` instead of a real popup and a real clock.

export type OAuthConnectPhase = "starting" | "waiting" | "connected" | "timed_out" | "error";

export interface OAuthConnectDeps {
  start: () => Promise<{ authorize_url: string }>;
  status: () => Promise<{ connected: boolean }>;
  openTab: (url: string) => void;
  sleep?: (ms: number) => Promise<void>;
  pollIntervalMs?: number;
  timeoutMs?: number;
}

const DEFAULT_POLL_INTERVAL_MS = 1500;
// Long enough to read a real consent screen and click through it -- the same
// order of magnitude as the backend's own `PendingOAuthTable` TTL (10
// minutes), a little shorter because a person still watching this tab after
// two minutes of silence is more likely stuck than about to finish.
const DEFAULT_TIMEOUT_MS = 120_000;

export async function runOAuthConnect(
  deps: OAuthConnectDeps,
  onPhase: (phase: OAuthConnectPhase, detail?: string) => void,
): Promise<void> {
  const sleep = deps.sleep ?? ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
  const interval = deps.pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS;
  const timeout = deps.timeoutMs ?? DEFAULT_TIMEOUT_MS;

  onPhase("starting");
  let authorizeUrl: string;
  try {
    ({ authorize_url: authorizeUrl } = await deps.start());
  } catch (exc) {
    onPhase("error", (exc as Error).message);
    return;
  }

  deps.openTab(authorizeUrl);
  onPhase("waiting");

  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    await sleep(interval);
    try {
      const { connected } = await deps.status();
      if (connected) {
        onPhase("connected");
        return;
      }
    } catch {
      // A status check that fails once is not the same claim as OAuth having
      // failed -- keep polling until the deadline instead of ending the
      // attempt on one dropped request.
    }
  }
  onPhase("timed_out");
}
