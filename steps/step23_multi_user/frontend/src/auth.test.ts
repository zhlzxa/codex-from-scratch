import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, setUnauthorizedHandler } from "./api";

/**
 * The one piece of chapter 23's frontend that is logic rather than layout.
 *
 * A session expires on its own -- twelve hours from sign-in, two hours idle --
 * so an open tab becomes a signed-out tab without anybody clicking anything.
 * What the console does at that moment is a decision, and the wrong version of
 * it (show the error, keep the stale page) is what most consoles do.
 *
 * `fetch` is stubbed rather than mocked at the module boundary, because the
 * behaviour under test is exactly "what does `call` do with a 401" and
 * replacing `call` would leave nothing.
 */

function respond(status: number, body: unknown = {}) {
  return vi.fn().mockResolvedValue({
    ok: status < 400,
    status,
    statusText: String(status),
    json: async () => body,
  } as unknown as Response);
}

afterEach(() => {
  setUnauthorizedHandler(null);
  vi.unstubAllGlobals();
});

describe("a 401 on a guarded route", () => {
  it("tells the app it has been signed out", async () => {
    vi.stubGlobal("fetch", respond(401, { detail: "not signed in" }));
    const signedOut = vi.fn();
    setUnauthorizedHandler(signedOut);

    await expect(api.workspaces()).rejects.toBeInstanceOf(ApiError);
    expect(signedOut).toHaveBeenCalledOnce();
  });

  it("still reports the error, so a caller that wants it gets it", async () => {
    vi.stubGlobal("fetch", respond(401, { detail: "not signed in" }));
    setUnauthorizedHandler(vi.fn());
    await expect(api.workspaces()).rejects.toMatchObject({ status: 401 });
  });
});

describe("a 401 from the sign-in routes themselves", () => {
  it("is a wrong password, not an expired session", async () => {
    // The distinction matters: bouncing the app back to the sign-in screen on
    // a failed sign-in would clear the form the person is typing into and lose
    // the message telling them why it failed.
    vi.stubGlobal("fetch", respond(401, { detail: "that account name and password do not match" }));
    const signedOut = vi.fn();
    setUnauthorizedHandler(signedOut);

    await expect(api.login("alice", "wrong")).rejects.toMatchObject({ status: 401 });
    expect(signedOut).not.toHaveBeenCalled();
  });
});

describe("everything else", () => {
  it("does not fire the handler", async () => {
    vi.stubGlobal("fetch", respond(404, { detail: "no thread 'nope'" }));
    const signedOut = vi.fn();
    setUnauthorizedHandler(signedOut);

    await expect(api.thread("nope")).rejects.toMatchObject({ status: 404 });
    expect(signedOut).not.toHaveBeenCalled();
  });

  it("passes a successful response through untouched", async () => {
    vi.stubGlobal("fetch", respond(200, { has_account: true, signed_in: false, account: null }));
    setUnauthorizedHandler(vi.fn());
    await expect(api.authStatus()).resolves.toMatchObject({ has_account: true });
  });
});
