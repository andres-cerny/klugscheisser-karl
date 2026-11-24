"""Read-only lookup: what Karl has stored for a person/team in a date window. See docs/mcp-gap-loop.md.

uv run python scripts/gap_have.py --team Schweiz --person "Alina Müller" --since 14d
"""

from klugscheisser.gaploop.have import main

if __name__ == "__main__":
    raise SystemExit(main())
