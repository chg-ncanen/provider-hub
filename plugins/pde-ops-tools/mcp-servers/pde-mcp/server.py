"""Compatibility MCP entrypoint.

Prefer `app.py` for core MCP wiring.
"""

import asyncio

from app import main


if __name__ == "__main__":
    asyncio.run(main())
