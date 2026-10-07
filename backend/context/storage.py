"""
context.storage
===============

In-memory storage layer for the context-awareness subsystem.
Everything is keyed by room_id; the module-level `context_store` singleton
is the single point of access, mirroring the `room_manager` pattern.

Classes
-------
ContentRegistry  — dict-backed store of SemanticRecords, one per canvas object
EventLog         — capped append-only list of CanvasEvents (max 200)
SessionMemory    — current SessionSummary + compressed action history (max 30)
RoomContextStore — bundles all three stores for one room
ContextStore     — dict[room_id, RoomContextStore]; lazily creates rooms

Usage
-----
    from context.storage import context_store

    room = context_store.get_room(room_id)
    room.registry.set(object_id, record)
    room.event_log.append(event)
    room.session_memory.add_action_summary("Created 3 sticky notes about auth flow")
"""

from __future__ import annotations

import math
import logging
from typing import Optional

from context.models import (
    CanvasEvent,
    EventType,
    ObjectType,
    SemanticRecord,
    SessionSummary,
)

logger = logging.getLogger(__name__)

_EVENT_LOG_CAP = 200
_ACTION_HISTORY_CAP = 30


# ---------------------------------------------------------------------------
# ContentRegistry
# ---------------------------------------------------------------------------


class ContentRegistry:
    """Keyed store of SemanticRecords for one room.

    # UPGRADE: replace dict with Postgres table + pgvector for embeddings
    #          so that search_tags() becomes a nearest-neighbour ANN query
    #          and get_nearby() can use a spatial index.
    """

    def __init__(self) -> None:
        self._records: dict[str, SemanticRecord] = {}  # UPGRADE: Postgres + pgvector

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def get(self, object_id: str) -> Optional[SemanticRecord]:
        """Return the record for *object_id*, or None if not registered."""
        return self._records.get(object_id)

    def set(self, object_id: str, record: SemanticRecord) -> None:
        """Insert or replace the record for *object_id*."""
        self._records[object_id] = record

    def delete(self, object_id: str) -> None:
        """Remove the record for *object_id* (no-op if absent)."""
        self._records.pop(object_id, None)

    def get_all(self) -> list[SemanticRecord]:
        """Return every registered record (order not guaranteed)."""
        return list(self._records.values())

    def __len__(self) -> int:
        return len(self._records)

    # ------------------------------------------------------------------
    # Filtered reads
    # ------------------------------------------------------------------

    def get_by_type(self, object_type: ObjectType) -> list[SemanticRecord]:
        """Return all records whose object_type matches *object_type*."""
        return [r for r in self._records.values() if r.object_type == object_type]

    def get_nearby(
        self, x: float, y: float, radius: float
    ) -> list[SemanticRecord]:
        """Return records whose centre lies within *radius* px of (x, y).

        Centre is computed as (position.x + w/2, position.y + h/2) when
        size is available, otherwise the anchor point is used directly.

        Records are returned sorted by distance (closest first).
        """
        results: list[tuple[float, SemanticRecord]] = []
        for record in self._records.values():
            cx = record.position.x + (record.size.w / 2 if record.size else 0)
            cy = record.position.y + (record.size.h / 2 if record.size else 0)
            dist = math.hypot(cx - x, cy - y)
            if dist <= radius:
                results.append((dist, record))
        results.sort(key=lambda t: t[0])
        return [r for _, r in results]

    def get_connected(self, object_id: str) -> list[SemanticRecord]:
        """Return records that the given object connects to via arrows.

        Reads the `connections` list on the record for *object_id* and
        resolves each ID against the registry.  Missing IDs are silently
        skipped (the target may have been deleted).
        """
        record = self._records.get(object_id)
        if record is None:
            return []
        results: list[SemanticRecord] = []
        for cid in record.connections:
            connected = self._records.get(cid)
            if connected is not None:
                results.append(connected)
        return results

    def search_tags(self, keywords: list[str]) -> list[SemanticRecord]:
        """Return records where any tag contains any keyword (case-insensitive substring).

        Example:
            search_tags(["auth", "login"])
            → records tagged with "authentication", "oauth", "login-flow", etc.
        """
        if not keywords:
            return []
        lower_keywords = [kw.lower() for kw in keywords]
        results: list[SemanticRecord] = []
        for record in self._records.values():
            lower_tags = [t.lower() for t in record.tags]
            if any(
                kw in tag
                for kw in lower_keywords
                for tag in lower_tags
            ):
                results.append(record)
        return results

    # ------------------------------------------------------------------
    # Stats helper (used to populate ContextPacket.canvas_stats)
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        """Return {"total": int, "by_type": {type_name: count}}."""
        by_type: dict[str, int] = {}
        for record in self._records.values():
            key = record.object_type.value
            by_type[key] = by_type.get(key, 0) + 1
        return {"total": len(self._records), "by_type": by_type}


# ---------------------------------------------------------------------------
# EventLog
# ---------------------------------------------------------------------------


class EventLog:
    """Append-only, capped event log for one room.

    Entries beyond _EVENT_LOG_CAP (200) are dropped from the front
    (oldest-first eviction) to bound memory usage.

    # UPGRADE: replace with append-only DB table so the full audit trail
    #          survives restarts and supports time-range queries.
    """

    def __init__(self) -> None:
        self._events: list[CanvasEvent] = []  # UPGRADE: append-only DB table

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def append(self, event: CanvasEvent) -> None:
        """Append *event* and evict the oldest entry if at capacity."""
        self._events.append(event)
        if len(self._events) > _EVENT_LOG_CAP:
            self._events.pop(0)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_recent(self, n: int = 10) -> list[CanvasEvent]:
        """Return the *n* most recent events (newest last)."""
        return self._events[-n:]

    def get_by_object(self, object_id: str) -> list[CanvasEvent]:
        """Return all events referencing *object_id*."""
        return [e for e in self._events if e.object_id == object_id]

    def get_by_type(self, event_type: EventType) -> list[CanvasEvent]:
        """Return all events of the given *event_type*."""
        return [e for e in self._events if e.event_type == event_type]

    def __len__(self) -> int:
        return len(self._events)


# ---------------------------------------------------------------------------
# SessionMemory
# ---------------------------------------------------------------------------


class SessionMemory:
    """Per-room session state: a live SessionSummary + compressed action log.

    The SessionSummary is updated periodically by a dedicated graph node
    (when needs_summary_update is True in the state).  The action history
    is a rolling buffer of short plain-text summaries of what the agent
    has done, capped at _ACTION_HISTORY_CAP (30) entries.

    # UPGRADE: persist SessionSummary and action history to DB between
    #          sessions so the agent remembers the board across page reloads.
    """

    def __init__(self) -> None:
        self._summary: SessionSummary = SessionSummary()
        self._action_history: list[str] = []  # UPGRADE: persist to DB
        # Running count of canvas events logged since the last summary refresh.
        # Resets to 0 after mark_summary_updated() is called.
        self._events_since_last_summary: int = 0

    # ------------------------------------------------------------------
    # SessionSummary
    # ------------------------------------------------------------------

    def get_summary(self) -> SessionSummary:
        """Return the current SessionSummary (read-only view)."""
        return self._summary

    def update_summary(self, new_summary: SessionSummary) -> None:
        """Replace the current summary with *new_summary*."""
        self._summary = new_summary

    # ------------------------------------------------------------------
    # Action history
    # ------------------------------------------------------------------

    def add_action_summary(self, text: str) -> None:
        """Append a plain-text summary of one agent action turn.

        Example: "Created 3 sticky notes about the authentication flow,
                  connected them to the existing 'Auth Service' box."

        Oldest entries are evicted once the cap is reached.
        """
        self._action_history.append(text)
        if len(self._action_history) > _ACTION_HISTORY_CAP:
            self._action_history.pop(0)

    def get_recent_actions(self, n: int = 10) -> list[str]:
        """Return the *n* most recent action summaries (oldest first)."""
        return self._action_history[-n:]

    # ------------------------------------------------------------------
    # Summary-update trigger
    # ------------------------------------------------------------------

    def increment_event_count(self, count: int = 1) -> None:
        """Increment the running event counter (called by prepare_node)."""
        self._events_since_last_summary += count

    def should_update_summary(self, threshold: int = 5) -> bool:
        """Return True when *threshold* or more events have accumulated
        since the last summary refresh."""
        return self._events_since_last_summary >= threshold

    def mark_summary_updated(self) -> None:
        """Reset the event counter after a summary refresh."""
        self._events_since_last_summary = 0

    def __len__(self) -> int:
        return len(self._action_history)


# ---------------------------------------------------------------------------
# Per-room bundle
# ---------------------------------------------------------------------------


class RoomContextStore:
    """All three context stores bundled for a single room."""

    __slots__ = ("registry", "event_log", "session_memory")

    def __init__(self) -> None:
        self.registry = ContentRegistry()
        self.event_log = EventLog()
        self.session_memory = SessionMemory()


# ---------------------------------------------------------------------------
# Global ContextStore (mirrors room_manager singleton pattern)
# ---------------------------------------------------------------------------


class ContextStore:
    """Dict-backed registry of per-room context stores.

    Rooms are created lazily on first access and survive as long as the
    process is alive.  There is no explicit TTL — rooms are evicted only
    when `evict_room()` is called (e.g. when all users leave).
    """

    def __init__(self) -> None:
        self._rooms: dict[str, RoomContextStore] = {}

    def get_room(self, room_id: str) -> RoomContextStore:
        """Return the RoomContextStore for *room_id*, creating it if needed."""
        from config import get_settings  # noqa: PLC0415

        if room_id not in self._rooms:
            self._rooms[room_id] = RoomContextStore()
            if get_settings().context_debug:
                logger.info("context_store created room=%s", room_id)
            else:
                logger.debug("context_store: created room %s", room_id)
        elif get_settings().context_debug:
            room = self._rooms[room_id]
            logger.info(
                "context_store reused room=%s registry=%d events=%d actions=%d",
                room_id,
                len(room.registry),
                len(room.event_log),
                len(room.session_memory),
            )
        return self._rooms[room_id]

    def evict_room(self, room_id: str) -> None:
        """Drop all context state for *room_id* (call when room is closed)."""
        if room_id in self._rooms:
            del self._rooms[room_id]
            logger.debug("context_store: evicted room %s", room_id)

    def active_rooms(self) -> list[str]:
        """Return the IDs of all rooms currently in memory."""
        return list(self._rooms.keys())


# Module-level singleton — import this everywhere.
context_store = ContextStore()
