"""Stand-in client (Claude Sonnet as Perplexity) against Karl's live MCP server. See docs/mcp-gap-loop.md.

with-secrets klugscheisser -- uv run python scripts/mcp_standin.py run --only p05 s01 --max-cost 0.10
uv run python scripts/mcp_standin.py compare data/gap-runs/A data/gap-runs/B --out compare.md
"""

from klugscheisser.gaploop.standin import main

if __name__ == "__main__":
    raise SystemExit(main())
