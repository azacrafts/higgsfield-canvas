import aiosqlite
import json
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "higgsfield.db"


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS rooms (
                room_id TEXT PRIMARY KEY,
                canvas_shapes_json TEXT NOT NULL DEFAULT '[]',
                canvas_snapshot_json TEXT,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS transcripts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id TEXT NOT NULL,
                username TEXT NOT NULL,
                text TEXT NOT NULL,
                ts REAL NOT NULL
            );
        """)
        await db.commit()


async def load_room(room_id: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT canvas_shapes_json, canvas_snapshot_json FROM rooms WHERE room_id=?",
            (room_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return {"shapes": [], "snapshot": None}
            return {
                "shapes": json.loads(row[0]),
                "snapshot": json.loads(row[1]) if row[1] else None,
            }


async def save_canvas(room_id: str, shapes: list[dict], snapshot: dict | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO rooms(room_id, canvas_shapes_json, canvas_snapshot_json, updated_at)
               VALUES(?,?,?,?)
               ON CONFLICT(room_id) DO UPDATE SET
                 canvas_shapes_json=excluded.canvas_shapes_json,
                 canvas_snapshot_json=COALESCE(excluded.canvas_snapshot_json, canvas_snapshot_json),
                 updated_at=excluded.updated_at""",
            (room_id, json.dumps(shapes), json.dumps(snapshot) if snapshot else None, time.time())
        )
        await db.commit()


async def save_transcript(room_id: str, username: str, text: str, ts: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO transcripts(room_id, username, text, ts) VALUES(?,?,?,?)",
            (room_id, username, text, ts)
        )
        await db.commit()
