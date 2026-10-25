import os
from contextlib import asynccontextmanager

import aiosqlite

DB_NAME = "chat_history.db"


@asynccontextmanager
async def _db():
    """Connection with common security settings. Commit only on success, otherwise rollback."""
    db = await aiosqlite.connect(DB_NAME)  # <--- Здесь была DB_Name с большой N
    try:
        await db.execute("PRAGMA secure_delete = ON")   # Securely overwrite deleted data physically
        await db.execute("PRAGMA foreign_keys = ON")
        yield db
        await db.commit()
    finally:
        await db.close()



async def init_db() -> None:
    async with _db() as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users (user_id),
                role TEXT NOT NULL CHECK (role IN ('user', 'model')),
                content TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_messages_user ON messages (user_id, id)")
    if os.path.exists(DB_NAME):
        os.chmod(DB_NAME, 0o600)  # Restricted access: owner read/write only (does not work on Windows)


async def add_user(user_id: int) -> None:
    """Data minimization: store only user_id, without username or first name."""
    async with _db() as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))


async def save_message(user_id: int, role: str, content: str) -> None:
    async with _db() as db:
        await db.execute(
            "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
            (user_id, role, content),
        )


async def get_user_history(user_id: int, limit: int = 10) -> list[dict]:
    async with _db() as db:
        async with db.execute(
            "SELECT role, content FROM messages WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, int(limit)),
        ) as cursor:
            rows = await cursor.fetchall()
    return [{"role": role, "content": content} for role, content in reversed(rows)]


async def clear_user_history(user_id: int) -> None:
    """/reset: clears conversation history only, user profile remains intact."""
    async with _db() as db:
        await db.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))


async def delete_user_data(user_id: int) -> None:
    """/delete_me: complete user data erasure (GDPR Right to be Forgotten)."""
    async with _db() as db:
        await db.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))


async def purge_old_messages(days: int) -> int:
    """Deletes messages older than the specified number of days. Returns the number of deleted rows."""
    modifier = f"-{int(days)} days"  # int() protects against invalid values
    async with _db() as db:
        cursor = await db.execute(
            "DELETE FROM messages WHERE timestamp < datetime('now', ?)",
            (modifier,),
        )
        removed = cursor.rowcount
        await db.execute(
            """
            DELETE FROM users
            WHERE created_at < datetime('now', ?)
              AND user_id NOT IN (SELECT user_id FROM messages)
            """,
            (modifier,),
        )
    return removed
