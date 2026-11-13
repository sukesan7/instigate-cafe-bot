"""Offline benchmark script: connection-per-query vs managed connection vs transaction batching.

Measures representative read and write workloads with WAL mode and busy timeout.
Never connects to external networks or touches production databases.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

import aiosqlite


async def benchmark_connection_per_query(db_path: str, iterations: int = 100) -> tuple[float, float]:
    """Measure open-query-close for each operation."""
    # Writes
    t0 = time.perf_counter()
    for i in range(iterations):
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("PRAGMA busy_timeout = 5000")
            await db.execute("INSERT INTO bench (val) VALUES (?)", (f"val_{i}",))
            await db.commit()
    write_time = time.perf_counter() - t0

    # Reads
    t0 = time.perf_counter()
    for i in range(iterations):
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            cur = await db.execute("SELECT val FROM bench WHERE id = ?", (i + 1,))
            await cur.fetchone()
    read_time = time.perf_counter() - t0

    return write_time, read_time


async def benchmark_managed_connection(db_path: str, iterations: int = 100) -> tuple[float, float]:
    """Measure single managed connection across operations."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("PRAGMA busy_timeout = 5000")

        # Writes (auto-commit or individual commits)
        t0 = time.perf_counter()
        for i in range(iterations):
            await db.execute("INSERT INTO bench (val) VALUES (?)", (f"val_{i}",))
            await db.commit()
        write_time = time.perf_counter() - t0

        # Reads
        t0 = time.perf_counter()
        for i in range(iterations):
            cur = await db.execute("SELECT val FROM bench WHERE id = ?", (i + 1,))
            await cur.fetchone()
        read_time = time.perf_counter() - t0

    return write_time, read_time


async def benchmark_transaction_batched(db_path: str, iterations: int = 100) -> tuple[float, float]:
    """Measure operations grouped in transactions on a managed connection."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("PRAGMA busy_timeout = 5000")

        # Writes in single transaction
        t0 = time.perf_counter()
        await db.execute("BEGIN IMMEDIATE")
        for i in range(iterations):
            await db.execute("INSERT INTO bench (val) VALUES (?)", (f"val_{i}",))
        await db.commit()
        write_time = time.perf_counter() - t0

        # Reads in single transaction
        t0 = time.perf_counter()
        for i in range(iterations):
            cur = await db.execute("SELECT val FROM bench WHERE id = ?", (i + 1,))
            await cur.fetchone()
        read_time = time.perf_counter() - t0

    return write_time, read_time


async def run_benchmarks(iterations: int = 200) -> dict[str, dict[str, float]]:
    results: dict[str, dict[str, float]] = {}

    for mode in ("DELETE", "WAL"):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / f"bench_{mode.lower()}.db")
            # Setup table and mode
            async with aiosqlite.connect(db_path) as db:
                await db.execute(f"PRAGMA journal_mode = {mode}")
                await db.execute(
                    "CREATE TABLE bench (id INTEGER PRIMARY KEY AUTOINCREMENT, val TEXT NOT NULL)"
                )
                await db.commit()

            # 1. Connection-per-query
            w1, r1 = await benchmark_connection_per_query(db_path, iterations)

            # Reset table for managed connection
            async with aiosqlite.connect(db_path) as db:
                await db.execute("DELETE FROM bench")
                await db.commit()

            # 2. Managed connection
            w2, r2 = await benchmark_managed_connection(db_path, iterations)

            # Reset table for batched transactions
            async with aiosqlite.connect(db_path) as db:
                await db.execute("DELETE FROM bench")
                await db.commit()

            # 3. Transaction batched
            w3, r3 = await benchmark_transaction_batched(db_path, iterations)

            results[mode] = {
                "conn_per_query_write_s": w1,
                "conn_per_query_read_s": r1,
                "managed_write_s": w2,
                "managed_read_s": r2,
                "batched_write_s": w3,
                "batched_read_s": r3,
            }

    return results


def main() -> None:
    iterations = 200
    print(f"Running SQLite benchmark ({iterations} operations per phase)...")
    results = asyncio.run(run_benchmarks(iterations))

    for mode, metrics in results.items():
        print(f"\n=== Journal Mode: {mode} ===")
        print(
            f"  Connection-per-query: Write: {metrics['conn_per_query_write_s']:.4f}s | "
            f"Read: {metrics['conn_per_query_read_s']:.4f}s"
        )
        print(
            f"  Managed connection:   Write: {metrics['managed_write_s']:.4f}s | "
            f"Read: {metrics['managed_read_s']:.4f}s"
        )
        print(
            f"  Batched transaction:  Write: {metrics['batched_write_s']:.4f}s | "
            f"Read: {metrics['batched_read_s']:.4f}s"
        )

        read_speedup = metrics["conn_per_query_read_s"] / max(metrics["managed_read_s"], 1e-6)
        write_speedup = metrics["conn_per_query_write_s"] / max(metrics["batched_write_s"], 1e-6)
        print(f"  -> Managed read speedup: {read_speedup:.1f}x")
        print(f"  -> Batched transaction write speedup: {write_speedup:.1f}x")


if __name__ == "__main__":
    main()
