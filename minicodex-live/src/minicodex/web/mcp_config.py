"""Turning one stored `mcp.json` record into the config object `minicodex.mcp`
and `minicodex.remote` already understand.

One function, called from two places -- `routes.py`, to build a
`RemoteServerConfig` for `POST /api/mcp/oauth/start`, and `runtime.py`, to
build the list `connect()` takes for a turn. It lives here rather than in
either of them because it is the same translation both times, and a
translation written twice is a translation that drifts: the schema snapshot
exact repair once already, for the tool descriptions duplicated between the
prompt and the schema.

Which shape a record is (`"stdio"` or `"remote"`) is **derived**, the same
rule `mcp.load_config` already applies to a config file's own `command`/`url`
split -- an older record carried no `"kind"` field at
all, and there is no reason to make every one of them re-save itself just to
gain one.
"""

from __future__ import annotations

from typing import Any

from minicodex.mcp import AnyServerConfig, ServerConfig
from minicodex.remote import RemoteConfigError, RemoteOAuthConfig, RemoteServerConfig

DEFAULT_STARTUP_TIMEOUT = 30.0
DEFAULT_TOOL_TIMEOUT = 60.0


def record_kind(record: dict[str, Any]) -> str:
    """`"stdio"` or `"remote"` for one persisted `mcp.json` record."""
    kind = record.get("kind")
    if kind in ("stdio", "remote"):
        return kind
    return "remote" if record.get("url") else "stdio"


def server_config(record: dict[str, Any]) -> AnyServerConfig:
    """One stored record -> the config `mcp.connect` actually takes.

    The raw `bearer_token` a browser typed into the add-server form is folded
    into a static `Authorization` header here, not passed through
    `RemoteServerConfig.bearer_token_env_var` -- that field exists so a config
    *file* never has to hold a secret in the clear (upstream's reasoning,
    still true for `mcp.json` the CLI loads). The console's own `mcp.json` is
    not that file: it is local runtime state a browser wrote to, exactly like
    `providers.json`'s `api_key` already is, and it is kept the
    same way for the same reason -- there is nowhere else for it to live.
    """
    startup = float(record.get("startup_timeout") or DEFAULT_STARTUP_TIMEOUT)
    tool = float(record.get("tool_timeout") or DEFAULT_TOOL_TIMEOUT)
    if record_kind(record) == "stdio":
        return ServerConfig(
            name=record["name"],
            command=tuple(record["command"]),
            env=dict(record.get("env") or {}),
            cwd=record.get("cwd"),
            startup_timeout=startup,
            tool_timeout=tool,
        )

    headers = dict(record.get("http_headers") or {})
    token = record.get("bearer_token")
    oauth_spec = record.get("oauth")
    if token and oauth_spec:
        # `add_mcp` already refuses this combination at the door (the
        # sibling check); this is the same rule enforced again where the
        # record is turned into something that actually connects, in case a
        # record ever reaches here some other way -- a hand-edited store file,
        # for instance.
        raise RemoteConfigError(
            f"{record.get('name', '?')}: a server is authenticated one way or "
            "the other -- a bearer token and OAuth cannot both be configured"
        )
    if token:
        headers.setdefault("Authorization", f"Bearer {token}")
    oauth = (
        RemoteOAuthConfig(client_id=oauth_spec.get("client_id"), scope=oauth_spec.get("scope"))
        if oauth_spec
        else None
    )
    return RemoteServerConfig(
        name=record["name"],
        url=record["url"],
        bearer_token_env_var=record.get("bearer_token_env_var"),
        http_headers=headers,
        env_http_headers=dict(record.get("env_http_headers") or {}),
        oauth=oauth,
        startup_timeout=startup,
        tool_timeout=tool,
    )


__all__ = ["DEFAULT_STARTUP_TIMEOUT", "DEFAULT_TOOL_TIMEOUT", "record_kind", "server_config"]
