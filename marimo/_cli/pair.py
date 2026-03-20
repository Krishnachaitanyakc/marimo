# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

import click

from marimo._cli.errors import MarimoCLIMissingDependencyError


@click.command(hidden=True, help="Pair with an AI agent via MCP.")
@click.option(
    "--transport",
    type=click.Choice(["stdio", "http"]),
    default="stdio",
    help="Transport protocol.",
)
@click.option(
    "-p",
    "--port",
    type=int,
    default=None,
    help="Port for the marimo server (auto-selects a free port if not set).",
)
@click.option(
    "--host",
    type=str,
    default="127.0.0.1",
    help="Host of running marimo server (HTTP transport only).",
)
@click.option(
    "--sandbox/--no-sandbox",
    default=True,
    help="Run each session in an isolated sandbox environment.",
)
def pair(transport: str, port: int | None, host: str, sandbox: bool) -> None:
    from marimo._dependencies.dependencies import DependencyManager
    from marimo._mcp.pair import pair_http, pair_stdio

    if not DependencyManager.mcp.has():
        raise MarimoCLIMissingDependencyError(
            "The 'mcp' package is required for marimo pair.",
            "marimo[mcp]",
        )

    if transport == "http":
        pair_http(host, port or 2718)
    else:
        pair_stdio(port=port, sandbox=sandbox)
