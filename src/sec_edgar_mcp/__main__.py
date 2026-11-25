"""Run the server.

    python -m sec_edgar_mcp            # HTTP (for hosting / claude.ai custom connector)
    python -m sec_edgar_mcp stdio      # stdio (for Claude Desktop / Claude Code on your own machine)

Settings come from environment variables or a .env file in the project folder:
    SEC_USER_AGENT   required, e.g. "Ola Nordmann ola@example.com"
    MCP_AUTH_TOKEN   optional for HTTP; if set, clients must send "Authorization: Bearer <token>"
    PORT             HTTP port (default 8000; hosting platforms set this)
"""

from __future__ import annotations

import os
import sys


def main() -> None:
    from .config import PROJECT_ROOT, load_env

    load_env()
    transport = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MCP_TRANSPORT", "http")).lower()

    if not os.environ.get("SEC_USER_AGENT"):
        print(
            "SEC_USER_AGENT is not set. SEC requires a name and e-mail. Either export it, e.g.\n"
            '  export SEC_USER_AGENT="Ola Nordmann ola@example.com"\n'
            f"or put that line in {PROJECT_ROOT / '.env'}",
            file=sys.stderr,
        )
        sys.exit(1)

    from .server import build_http_app, mcp

    if transport == "stdio":
        mcp.run("stdio")
        return
    if transport != "http":
        print(f"Unknown transport '{transport}'. Use 'http' or 'stdio'.", file=sys.stderr)
        sys.exit(2)

    import uvicorn

    if not os.environ.get("MCP_AUTH_TOKEN"):
        print("WARNING: MCP_AUTH_TOKEN is not set; anyone who knows the URL can use this server.", file=sys.stderr)
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(build_http_app(), host="0.0.0.0", port=port, log_level="info", proxy_headers=True)


def run() -> None:
    """Console entry point: like main(), but Ctrl+C exits quietly."""
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    run()
