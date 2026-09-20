"""What real MCP servers and real authorization servers actually do.

Every other probe in this book measures a model. This one measures *servers*,
because the questions this chapter has to answer are not about behaviour under
a prompt -- they are about facts on the real internet: does a bearer token
alone get you into GitHub's MCP server, does a real authorization server
advertise dynamic client registration, how long does a real network round trip
actually take compared to chapter 9's subprocess-startup budget.

`sse`/`shapes`/wire-level framing sections that used to live here are gone: the
transport itself -- content negotiation, session ids, SSE framing -- is the
`mcp` SDK's job now, and measuring it would be measuring the SDK's own test
suite's job, not this chapter's.

Sections marked `network` reach the public internet and nothing else -- no
credential is sent unless `GITHUB_MCP_TOKEN` is set, no account is touched
without one, and every request is an unauthenticated `initialize`, a `GET` of
a well-known document, or (with a token) a read-only `tools/list`.

    uv run python probe_mcp_remote.py all
    uv run python probe_mcp_remote.py challenge     # network
    uv run python probe_mcp_remote.py wellknown     # network
    uv run python probe_mcp_remote.py bearer        # network, needs GITHUB_MCP_TOKEN
    uv run python probe_mcp_remote.py latency       # network
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx

from minicodex.mcp import McpClient
from minicodex.remote import RemoteServerConfig

# Two public MCP endpoints. Neither is operated by this project; both are
# reached read-only, and only GitHub's requires authentication.
GITHUB = "https://api.githubcopilot.com/mcp/"
DEEPWIKI = "https://mcp.deepwiki.com/mcp"

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "minicodex-probe", "version": "0"},
    },
}

_CHALLENGE_PARAM = re.compile(r'resource_metadata\s*=\s*"([^"]+)"', re.IGNORECASE)


def _resource_metadata_url(header: str) -> str | None:
    match = _CHALLENGE_PARAM.search(header or "")
    return match.group(1) if match else None


def rule(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 68 - len(title)))


async def challenge() -> None:
    """F21-07/F21-08. What a server says when you arrive with no credential."""
    rule("challenge")
    print("  an unauthenticated initialize, and what comes back\n")
    async with httpx.AsyncClient(timeout=10.0) as client:
        for url in (GITHUB, DEEPWIKI):
            try:
                response = await client.post(url, headers=HEADERS, json=INIT)
            except httpx.HTTPError as exc:
                print(f"  {url}\n    UNREACHABLE: {type(exc).__name__}")
                continue
            header = response.headers.get("www-authenticate", "")
            print(f"  {url}")
            print(f"    status                {response.status_code}")
            print(f"    content-type          {response.headers.get('content-type')}")
            print(f"    WWW-Authenticate      {header[:88] or '(none)'}")
            print(f"    resource_metadata     {_resource_metadata_url(header) or '(none)'}")


async def wellknown() -> None:
    """F21-08/F21-08b. Which `.well-known` path a real authorization server
    serves, and whether it advertises RFC 7591 dynamic client registration.

    RFC 8414 says the metadata for an issuer with a path lives at
    `/.well-known/oauth-authorization-server` *with the issuer's path
    appended*, and codex tries that form first
    (`rmcp-client/src/perform_oauth_login.rs:808-815`). It is easy to read
    that as belt-and-braces. It is not: GitHub serves only the suffixed form.

    The `registration_endpoint` check answers F21-08b's premise directly,
    against the one real authorization server this probe can reach without a
    token: does GitHub's own authorization server support the thing codex
    never implemented and the SDK does. Reading the metadata document is
    read-only; nothing here calls `/register`.
    """
    rule("wellknown")
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(GITHUB, headers=HEADERS, json=INIT)
        metadata_url = _resource_metadata_url(response.headers.get("www-authenticate", ""))
        if metadata_url is None:
            print("  GitHub did not name a metadata document; nothing to follow")
            return
        print(f"  hop 1  {metadata_url}")
        document = (await client.get(metadata_url, headers={"Accept": "application/json"})).json()
        servers = document.get("authorization_servers") or []
        print(f"  hop 2  authorization_servers = {servers}")
        if not servers:
            return
        issuer = servers[0]
        parsed = urlparse(issuer)
        base = f"{parsed.scheme}://{parsed.netloc}"
        suffix = parsed.path.rstrip("/")
        print(f"\n  issuer {issuer} -- two candidate metadata paths:\n")
        asm: dict | None = None
        for candidate in (
            urljoin(base + "/", f".well-known/oauth-authorization-server{suffix}"),
            urljoin(base + "/", ".well-known/oauth-authorization-server"),
        ):
            try:
                probe = await client.get(candidate, headers={"Accept": "application/json"})
            except httpx.HTTPError as exc:
                print(f"    ERR   {candidate}  ({type(exc).__name__})")
                continue
            usable = probe.status_code == 200 and "authorization_endpoint" in probe.text
            print(f"    {probe.status_code}   {'USED ' if usable else 'no   '} {candidate}")
            if usable and asm is None:
                asm = probe.json()
        print()
        if asm is None:
            print("  could not read authorization server metadata; skipping the DCR check")
            return
        endpoint = asm.get("registration_endpoint")
        print(f"  registration_endpoint  {endpoint or '(absent)'}")
        print(
            "  -> "
            + (
                "this authorization server advertises RFC 7591; the SDK's "
                "OAuthClientProvider would attempt dynamic registration here "
                "rather than needing a preset client_id (F21-08b)."
                if endpoint
                else "no registration_endpoint advertised; a client without a "
                "preset client_id has nothing to register against, which is "
                "the shape codex assumes for every server it talks to."
            )
        )


async def bearer() -> None:
    """F21-07, measured rather than assumed: does a static personal access
    token alone get past GitHub's real MCP server?

    Skips, rather than fails, when `GITHUB_MCP_TOKEN` is not set -- this is
    the one section of this probe that can move data if it runs (a real
    `tools/list` against a real account's token), and CI does not carry that
    secret. Running it locally with a real token is how this chapter's claim
    about F21-07 was actually established, not assumed.
    """
    rule("bearer")
    token_var = "GITHUB_MCP_TOKEN"
    if not os.environ.get(token_var):
        print(
            f"  {token_var} is not set; skipping (this section needs a real token to mean anything)"
        )
        print("  to run it: export GITHUB_MCP_TOKEN=<a github PAT with the MCP scopes>")
        return
    config = RemoteServerConfig(
        name="github",
        url=GITHUB,
        bearer_token_env_var=token_var,
        startup_timeout=25.0,
        tool_timeout=25.0,
    )
    client = McpClient(config)
    t0 = time.monotonic()
    try:
        await client.start()
    except Exception as exc:
        print(f"  a static bearer token was NOT sufficient: {type(exc).__name__}: {exc}")
        return
    elapsed = time.monotonic() - t0
    try:
        tools = await client.list_tools()
        print(f"  a static bearer token WAS sufficient -- connected in {elapsed:.2f}s")
        print(f"  serverInfo   {client.server_info}")
        names = ", ".join(t.name for t in tools[:5])
        more = ", ..." if len(tools) > 5 else ""
        print(f"  tools        {len(tools)} ({names}{more})")
    finally:
        await client.close()


async def latency() -> None:
    """F21-06, re-measured: chapter 9's timeouts were sized around a
    subprocess's `fork`+`exec`. This is the network round trip they now also
    have to cover."""
    rule("latency")
    for name, url in (("github", GITHUB), ("deepwiki", DEEPWIKI)):
        config = RemoteServerConfig(name=name, url=url, startup_timeout=25.0, tool_timeout=25.0)
        client = McpClient(config)
        t0 = time.monotonic()
        try:
            await client.start()
            elapsed = time.monotonic() - t0
            state = client.failure or "ok"
            print(f"  {name:<10} connect+initialize in {elapsed:.2f}s (unauthenticated: {state})")
        except Exception as exc:
            elapsed = time.monotonic() - t0
            print(f"  {name:<10} failed after {elapsed:.2f}s: {type(exc).__name__}: {exc}")
        finally:
            await client.close()
    print(
        "\n  chapter 9's startup budget is 30s, sized around a subprocess that "
        "might compile something on first run. Every measurement above should "
        "land in low single-digit seconds -- if it does not on your network, "
        "that is this section doing its job."
    )


SECTIONS = {
    "challenge": challenge,
    "wellknown": wellknown,
    "bearer": bearer,
    "latency": latency,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("section", choices=[*SECTIONS, "all"])
    args = parser.parse_args()
    names = list(SECTIONS) if args.section == "all" else [args.section]
    for name in names:
        asyncio.run(SECTIONS[name]())


if __name__ == "__main__":
    main()
