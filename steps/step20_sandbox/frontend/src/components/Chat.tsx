import { useEffect, useRef, useState } from "react";
import type { Entry } from "../useThread";
import type { HistoryItem, PlanStep, ToolCall } from "../types";

function summarise(args: Record<string, unknown>): string {
  try {
    const text = JSON.stringify(args);
    return text.length > 90 ? `${text.slice(0, 87)}...` : text;
  } catch {
    return "";
  }
}

function ToolCallBlock({ call, result }: { call: ToolCall; result?: string }) {
  const [open, setOpen] = useState(true);
  return (
    <div className="toolcall">
      <div className="toolcall-head" onClick={() => setOpen((v) => !v)}>
        <span className="tc-tag">{call.name}</span>
        <span className="tc-args">{summarise(call.arguments)}</span>
        {result === undefined && <span className="tc-spin" aria-label="running" />}
      </div>
      {open && <pre>{result ?? "running…"}</pre>}
    </div>
  );
}

function Item({ item, results }: { item: HistoryItem; results: Record<string, string> }) {
  if (item.type === "user") {
    return (
      <div className="msg msg-user">
        <span className="msg-role">You</span>
        <div className="msg-bubble">{item.text}</div>
      </div>
    );
  }
  if (item.type === "assistant") {
    return (
      <div className="msg msg-agent">
        <span className="msg-role">minicodex</span>
        <div className="msg-body">
          {item.text && <div>{item.text}</div>}
          {(item.tool_calls ?? []).map((call) => (
            <ToolCallBlock key={call.call_id} call={call} result={results[call.call_id]} />
          ))}
        </div>
      </div>
    );
  }
  if (item.type === "system_note" || item.type === "developer_note") {
    return <div className="msg-note">{item.text}</div>;
  }
  // A bare `tool_result` with no call to fold into: possible on a resync, if
  // the rollout's matching assistant item was in the part that got dropped.
  return (
    <div className="toolcall">
      <div className="toolcall-head">
        <span className="tc-tag">{item.name ?? "tool result"}</span>
      </div>
      <pre>{item.content}</pre>
    </div>
  );
}

/**
 * The three marks the agent writes, rendered.
 *
 * These were emitted by the backend and dropped by the frontend entirely, so a
 * run could compact half its history away, or hit its turn budget, with nothing
 * on screen to say so. They are the only signal that the conversation on screen
 * is no longer the conversation the model is being sent.
 */
function Mark({ mark, payload }: { mark: string; payload: Record<string, unknown> }) {
  const text =
    mark === "compacted"
      ? `history compacted — ${payload.replaced ?? "?"} message(s) replaced by a summary (generation ${payload.generation ?? "?"})`
      : mark === "budget_exhausted"
        ? `turn budget exhausted at turn ${payload.turn ?? "?"} — the model stopped because it ran out of turns, not because it finished`
        : mark === "interrupted"
          ? `interrupted at turn ${payload.turn ?? "?"} — the next message in this thread resumes from here`
          : `${mark}: ${JSON.stringify(payload)}`;
  return <div className="msg-mark">{text}</div>;
}

function ApprovalCard({
  entry,
  onRespond,
}: {
  entry: Extract<Entry, { kind: "approval" }>;
  onRespond: (
    id: string,
    approved: boolean,
    command: string,
    remember: "session" | "project" | null,
  ) => Promise<void>;
}) {
  const [command, setCommand] = useState(entry.what);
  const [sending, setSending] = useState(false);
  const risky = /high|critical|interpreter|network/i.test(entry.risk);
  const resolved = entry.status !== null;

  const answer = async (approved: boolean, remember: "session" | "project" | null) => {
    setSending(true);
    await onRespond(entry.id, approved, command, remember);
    setSending(false);
  };

  return (
    <div className={`approval-card${risky ? " risk-high" : ""}${resolved ? " resolved" : ""}`}>
      <div className="ac-title">minicodex wants to run something</div>
      <div className="ac-reason">
        {entry.reason} (risk: {entry.risk.toLowerCase()})
      </div>
      {/* An editable command, because approving something slightly different
          from what was asked is a real answer -- `ApprovalReply.command` is
          what actually runs. */}
      <input
        className="ac-cmd"
        value={command}
        disabled={resolved || sending}
        onChange={(e) => setCommand(e.target.value)}
      />
      <div className="ac-actions">
        <button
          className="btn btn-primary btn-sm"
          disabled={sending}
          onClick={() => void answer(true, null)}
        >
          Approve once
        </button>
        {entry.suggested_rule && entry.suggested_rule.length > 0 && (
          <>
            <button
              className="btn btn-sm"
              disabled={sending}
              onClick={() => void answer(true, "session")}
            >
              Always this session ({entry.suggested_rule.join(" ")})
            </button>
            {/* The backend has always accepted "project"; the old card never
                offered it, so the durable half of chapter 5's rule store was
                unreachable from the browser. */}
            <button
              className="btn btn-sm"
              disabled={sending}
              onClick={() => void answer(true, "project")}
              title="Remembered on disk for this workspace. Revoke under Extensions → Rules."
            >
              Always in this project
            </button>
          </>
        )}
        <button
          className="btn btn-danger btn-sm"
          disabled={sending}
          onClick={() => void answer(false, null)}
        >
          Deny
        </button>
      </div>
      <div className="ac-status">
        {entry.status === "approved" && "Approved."}
        {entry.status === "denied" && "Denied."}
        {entry.status === "timeout" && "Timed out -- treated as denied."}
        {entry.status === "failed" && `Failed to send: ${entry.note}`}
      </div>
    </div>
  );
}

export function PlanPanel({ steps }: { steps: PlanStep[] }) {
  if (steps.length === 0) return null;
  return (
    <div className="plan-panel">
      <div className="pop-label">Plan</div>
      <ul>
        {steps.map((step, i) => (
          <li key={i} className={`plan-step plan-${step.status}`}>
            <span className="plan-box" aria-hidden="true" />
            <span>{step.text}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

interface Props {
  entries: Entry[];
  results: Record<string, string>;
  reports: string[];
  empty: string;
  onRespond: (
    id: string,
    approved: boolean,
    command: string,
    remember: "session" | "project" | null,
  ) => Promise<void>;
}

export function Chat({ entries, results, reports, empty, onRespond }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);

  // Follow the bottom, but stop following the moment the reader scrolls up:
  // a transcript that yanks itself back down while you are reading a tool
  // result is a transcript you cannot read during a long run.
  useEffect(() => {
    const el = scrollRef.current;
    if (el && pinnedRef.current) el.scrollTop = el.scrollHeight;
  }, [entries, results, reports]);

  return (
    <div
      className="chat-scroll"
      ref={scrollRef}
      onScroll={(e) => {
        const el = e.currentTarget;
        pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
      }}
    >
      <div className="chat-col">
        {entries.length === 0 && <div className="chat-empty">{empty}</div>}
        {entries.map((entry) => {
          switch (entry.kind) {
            case "item":
              return <Item key={entry.key} item={entry.item} results={results} />;
            case "note":
              return (
                <div key={entry.key} className={`msg-note${entry.tone === "danger" ? " danger" : ""}`}>
                  {entry.text}
                </div>
              );
            case "mark":
              return <Mark key={entry.key} mark={entry.mark} payload={entry.payload} />;
            case "approval":
              return <ApprovalCard key={entry.key} entry={entry} onRespond={onRespond} />;
          }
        })}
        {reports.length > 0 && (
          <div className="run-reports">
            {reports.map((line, i) => (
              <div key={i}>[{line}]</div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
