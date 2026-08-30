import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { HistoryItem, PlanStep, SandboxMode, ServerEvent } from "./types";

/** One renderable thing in the transcript, in arrival order. */
export type Entry =
  | { kind: "item"; key: string; item: HistoryItem }
  | { kind: "note"; key: string; text: string; tone: "plain" | "danger" }
  | { kind: "mark"; key: string; mark: string; payload: Record<string, unknown> }
  | {
      kind: "approval";
      key: string;
      id: string;
      what: string;
      reason: string;
      risk: string;
      suggested_rule: string[] | null;
      status: null | "approved" | "denied" | "timeout" | "failed";
      note?: string;
    };

/**
 * `Omit<Entry, "key">` does not do what it looks like it does: over a union it
 * collapses to the keys every member shares, so it accepts `{kind: "note"}` and
 * rejects `text`. The conditional makes it distribute over the members instead.
 */
type Unkeyed<T> = T extends unknown ? Omit<T, "key"> : never;

export type Connection = "connecting" | "open" | "retrying" | "closed";

/** Reconnect backoff, in milliseconds, by consecutive failure count. */
const BACKOFF = [500, 1000, 2000, 4000, 8000, 15000];

let seq = 0;
const nextKey = () => `e${++seq}`;

export interface ThreadView {
  entries: Entry[];
  results: Record<string, string>;
  plan: PlanStep[];
  mode: SandboxMode | null;
  busy: boolean;
  connection: Connection;
  reports: string[];
  send: (text: string) => Promise<void>;
  stop: () => Promise<void>;
  respond: (
    id: string,
    approved: boolean,
    command: string,
    remember: "session" | "project" | null,
  ) => Promise<void>;
  error: string | null;
}

/**
 * Subscribe to one thread: load its history, follow its socket, reconnect.
 *
 * The reconnect is the point. The previous console set `onmessage` and nothing
 * else -- no `onclose`, no `onerror` -- so a `--reload` on the server, a laptop
 * sleeping, or any dropped connection left the page showing `thinking…`
 * forever with no way back but F5. Worse, it was silent: the transcript simply
 * stopped growing while the turn kept running on the server.
 *
 * Reconnecting is not enough on its own, because the backend drops events for a
 * thread nobody is watching (see channel.py). So every successful reconnect is
 * followed by a **resync** from `GET /api/threads/{id}`, which reads the same
 * rollout file the events were derived from. The socket is an optimisation over
 * polling that file; it is not the only copy of the truth, and that is what
 * makes losing it survivable.
 */
export function useThread(threadId: string | null): ThreadView {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [results, setResults] = useState<Record<string, string>>({});
  const [plan, setPlan] = useState<PlanStep[]>([]);
  const [mode, setMode] = useState<SandboxMode | null>(null);
  const [busy, setBusy] = useState(false);
  const [connection, setConnection] = useState<Connection>("closed");
  const [reports, setReports] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  const socketRef = useRef<WebSocket | null>(null);
  const retriesRef = useRef(0);
  const timerRef = useRef<number | null>(null);
  const liveRef = useRef(true);

  const push = useCallback((entry: Unkeyed<Entry>) => {
    setEntries((current) => [...current, { ...entry, key: nextKey() } as Entry]);
  }, []);

  const resync = useCallback(async (id: string) => {
    const record = await api.thread(id);
    const items = record.items as HistoryItem[];
    const answers: Record<string, string> = {};
    const calls = new Set<string>();
    for (const item of items) {
      if (item.type === "tool_result") answers[item.call_id] = item.content;
      if (item.type === "assistant") {
        for (const call of item.tool_calls ?? []) calls.add(call.call_id);
      }
    }
    // A tool result is drawn inside the call it answers, so it is not its own
    // entry -- unless the call it belongs to is not in this history, which is
    // possible after an interrupted turn was partly dropped on resume.
    const fresh: Entry[] = items
      .filter((item) => item.type !== "tool_result" || !calls.has(item.call_id))
      .map((item) => ({ kind: "item", key: nextKey(), item }));
    setEntries(fresh);
    setResults(answers);
    setBusy(record.busy);
    setMode(record.settings.sandbox_mode);
  }, []);

  const apply = useCallback(
    (event: ServerEvent) => {
      switch (event.type) {
        case "turn_started":
          setBusy(true);
          setReports([]);
          break;
        case "turn_finished":
          setBusy(false);
          break;
        case "history_item": {
          const item = event.item;
          if (item.type === "tool_result") {
            // Folded into the call it answers rather than appended as its own
            // block, so a turn reads as "it ran this, and got that".
            setResults((current) => ({ ...current, [item.call_id]: item.content }));
            return;
          }
          // The optimistic echo is replaced by the real thing rather than
          // duplicated: the server's copy is the one that got written down.
          setEntries((current) => {
            const last = current[current.length - 1];
            if (
              item.type === "user" &&
              last?.kind === "item" &&
              last.item.type === "user" &&
              last.item.text === item.text
            ) {
              return current;
            }
            return [...current, { kind: "item", key: nextKey(), item }];
          });
          break;
        }
        case "mark":
          push({ kind: "mark", mark: event.mark, payload: event.payload });
          break;
        case "notice":
          push({ kind: "note", text: event.text, tone: "plain" });
          break;
        case "error":
          push({ kind: "note", text: event.text, tone: "danger" });
          setBusy(false);
          break;
        case "mcp_status":
          push({
            kind: "note",
            tone: event.status === "connected" ? "plain" : "danger",
            text:
              event.status === "connected"
                ? `mcp: ${event.name} connected, ${event.tools} tool(s)`
                : `mcp: ${event.name} unavailable -- ${event.detail}`,
          });
          break;
        case "plan_updated":
          setPlan(event.steps);
          break;
        case "session_mode_changed":
          setMode(event.mode);
          push({
            kind: "note",
            tone: "danger",
            text: `permissions changed: ${event.from} -> ${event.mode}`,
          });
          break;
        case "approval_request":
          push({
            kind: "approval",
            id: event.id,
            what: event.what,
            reason: event.reason,
            risk: event.risk,
            suggested_rule: event.suggested_rule,
            status: null,
          });
          break;
        case "approval_resolved":
        case "approval_timeout":
          setEntries((current) =>
            current.map((entry) =>
              entry.kind === "approval" && entry.id === event.id && entry.status === null
                ? {
                    ...entry,
                    status:
                      event.type === "approval_timeout"
                        ? "timeout"
                        : event.approved
                          ? "approved"
                          : "denied",
                  }
                : entry,
            ),
          );
          break;
        case "turn_complete":
          setPlan(event.plan);
          setReports(event.reports);
          setBusy(false);
          break;
      }
    },
    [push],
  );

  useEffect(() => {
    liveRef.current = true;
    if (!threadId) {
      setEntries([]);
      setResults({});
      setPlan([]);
      setReports([]);
      setBusy(false);
      setConnection("closed");
      return;
    }

    let cancelled = false;

    const open = () => {
      if (cancelled) return;
      setConnection(retriesRef.current === 0 ? "connecting" : "retrying");
      const scheme = location.protocol === "https:" ? "wss:" : "ws:";
      const socket = new WebSocket(`${scheme}//${location.host}/ws/threads/${threadId}`);
      socketRef.current = socket;

      socket.onopen = () => {
        if (cancelled) return;
        setConnection("open");
        // Resync *after* the socket is listening, not before: doing it the
        // other way round leaves a gap where an event fires between the fetch
        // and the subscription and is lost with nothing to notice it.
        const wasRetry = retriesRef.current > 0;
        retriesRef.current = 0;
        if (wasRetry) void resync(threadId).catch(() => undefined);
      };
      socket.onmessage = (evt) => {
        if (cancelled) return;
        try {
          apply(JSON.parse(evt.data) as ServerEvent);
        } catch {
          /* a frame we cannot parse is not worth taking the page down for */
        }
      };
      socket.onclose = () => {
        if (cancelled) return;
        const wait = BACKOFF[Math.min(retriesRef.current, BACKOFF.length - 1)];
        retriesRef.current += 1;
        setConnection("retrying");
        timerRef.current = window.setTimeout(open, wait);
      };
      socket.onerror = () => socket.close();
    };

    setEntries([]);
    setResults({});
    setPlan([]);
    setReports([]);
    setError(null);
    retriesRef.current = 0;
    void resync(threadId)
      .then(() => open())
      .catch((exc: Error) => {
        setError(exc.message);
        open();
      });

    return () => {
      cancelled = true;
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
      const socket = socketRef.current;
      socketRef.current = null;
      if (socket) {
        socket.onclose = null; // do not schedule a retry for a deliberate close
        socket.close();
      }
    };
  }, [threadId, apply, resync]);

  const send = useCallback(
    async (text: string) => {
      if (!threadId) return;
      setError(null);
      // Shown before the request, so a dead socket or a 409 cannot make the
      // typed message disappear with nothing on screen to say it existed.
      push({ kind: "item", item: { type: "user", text } });
      setBusy(true);
      try {
        await api.send(threadId, text);
      } catch (exc) {
        setBusy(false);
        setError((exc as Error).message);
      }
    },
    [threadId, push],
  );

  const stop = useCallback(async () => {
    if (!threadId) return;
    try {
      await api.cancel(threadId);
    } catch (exc) {
      setError((exc as Error).message);
    }
  }, [threadId]);

  const respond = useCallback(
    async (
      id: string,
      approved: boolean,
      command: string,
      remember: "session" | "project" | null,
    ) => {
      try {
        await api.respond(id, approved, command, remember);
      } catch (exc) {
        // The card is *not* marked resolved before this returns. Doing that
        // was optimistic in the wrong direction: the turn stays suspended on a
        // reply the server never received, and the card said "Approved."
        setEntries((current) =>
          current.map((entry) =>
            entry.kind === "approval" && entry.id === id
              ? { ...entry, status: "failed", note: (exc as Error).message }
              : entry,
          ),
        );
      }
    },
    [],
  );

  return {
    entries,
    results,
    plan,
    mode,
    busy,
    connection,
    reports,
    send,
    stop,
    respond,
    error,
  };
}
