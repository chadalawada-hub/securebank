"""
migrate.py  –  Run this once to create all tables.
Also serves as the Alembic env.py target.

Usage:
  python migrate.py
"""
import asyncio
from database import engine
from models import Base


async def create_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("✅  All tables created successfully.")


if __name__ == "__main__":
    asyncio.run(create_tables())