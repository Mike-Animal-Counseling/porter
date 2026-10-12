"""Apply pending SQL migrations. Usage: python scripts/migrate.py"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from porter.config import settings
from porter.db import run_migrations


async def main() -> None:
    applied = await run_migrations(settings.database_url)
    if applied:
        print("applied:", ", ".join(applied))
    else:
        print("nothing to apply (up to date)")


if __name__ == "__main__":
    asyncio.run(main())
