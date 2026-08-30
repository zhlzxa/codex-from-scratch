import { describe, expect, it } from "vitest";
import { runOAuthConnect } from "./mcpOauth";

// `sleep` is faked out with an immediately-resolving promise in every test
// here, so "poll a few times" costs microtasks rather than wall-clock time --
// the same reason `test_faults_ch19.py`'s fakes never actually wait on a
// socket.
const instant = () => Promise.resolve();

describe("runOAuthConnect", () => {
  it("opens the authorize URL and reports connected once status flips", async () => {
    const opened: string[] = [];
    let calls = 0;
    const phases: string[] = [];

    await runOAuthConnect(
      {
        start: async () => ({ authorize_url: "https://auth.example/authorize?x=1" }),
        status: async () => {
          calls += 1;
          return { connected: calls >= 3 };
        },
        openTab: (url) => opened.push(url),
        sleep: instant,
      },
      (phase) => phases.push(phase),
    );

    expect(opened).toEqual(["https://auth.example/authorize?x=1"]);
    expect(calls).toBe(3);
    expect(phases).toEqual(["starting", "waiting", "connected"]);
  });

  it("reports error and never opens a tab when start() fails", async () => {
    const opened: string[] = [];
    const phases: [string, string | undefined][] = [];

    await runOAuthConnect(
      {
        start: async () => {
          throw new Error("no client_id and this server has no registration_endpoint");
        },
        status: async () => ({ connected: false }),
        openTab: (url) => opened.push(url),
        sleep: instant,
      },
      (phase, detail) => phases.push([phase, detail]),
    );

    expect(opened).toEqual([]);
    expect(phases).toEqual([
      ["starting", undefined],
      ["error", "no client_id and this server has no registration_endpoint"],
    ]);
  });

  it("times out rather than polling forever when nobody finishes authorizing", async () => {
    const phases: string[] = [];
    let now = 0;
    // A fake clock local to this test: `Date.now` is not stubbed globally,
    // `sleep` just advances a counter the deadline math reads through
    // `pollIntervalMs`/`timeoutMs` set small enough that two polls elapse the
    // budget without a real timer ever running.

    await runOAuthConnect(
      {
        start: async () => ({ authorize_url: "https://auth.example/authorize" }),
        status: async () => ({ connected: false }),
        openTab: () => undefined,
        sleep: async () => {
          now += 10;
        },
        pollIntervalMs: 10,
        timeoutMs: 25,
      },
      (phase) => phases.push(phase),
    );

    expect(now).toBeGreaterThan(0);
    expect(phases.at(-1)).toBe("timed_out");
  });

  it("keeps polling through a transient status failure instead of giving up", async () => {
    const phases: string[] = [];
    let calls = 0;

    await runOAuthConnect(
      {
        start: async () => ({ authorize_url: "https://auth.example/authorize" }),
        status: async () => {
          calls += 1;
          if (calls === 1) throw new Error("network blip");
          return { connected: calls >= 2 };
        },
        openTab: () => undefined,
        sleep: instant,
      },
      (phase) => phases.push(phase),
    );

    expect(calls).toBe(2);
    expect(phases).toEqual(["starting", "waiting", "connected"]);
  });
});
