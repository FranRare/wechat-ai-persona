import sqlite3
import os
from datetime import datetime
from pathlib import Path

DB_PATH = os.environ.get("MEMORY_DB_PATH", "./data/memory.db")

def _conn():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            category TEXT DEFAULT 'general',
            importance INTEGER DEFAULT 5,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn

def remember(content: str, category: str = "general", importance: int = 5) -> int:
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO memories (content, category, importance) VALUES (?, ?, ?)",
        (content, category, importance)
    )
    conn.commit()
    conn.close()
    return cur.lastrowid

def search(query: str, limit: int = 10) -> list[dict]:
    conn = _conn()
    cur = conn.execute(
        "SELECT id, content, category, importance, created_at FROM memories WHERE content LIKE ? ORDER BY importance DESC, created_at DESC LIMIT ?",
        (f"%{query}%", limit)
    )
    rows = [{"id": r[0], "content": r[1], "category": r[2], "importance": r[3], "created_at": r[4]} for r in cur.fetchall()]
    conn.close()
    return rows

def list_recent(limit: int = 20) -> list[dict]:
    conn = _conn()
    cur = conn.execute(
        "SELECT id, content, category, importance, created_at FROM memories ORDER BY created_at DESC LIMIT ?",
        (limit,)
    )
    rows = [{"id": r[0], "content": r[1], "category": r[2], "importance": r[3], "created_at": r[4]} for r in cur.fetchall()]
    conn.close()
    return rows
