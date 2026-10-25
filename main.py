"""
main.py
-------
Backward-compatible entry point for Beycord.
Delegates directly to app.py.
"""

import asyncio
from app import main

if __name__ == "__main__":
    asyncio.run(main())
