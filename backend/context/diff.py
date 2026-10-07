"""
context.diff
============

Canvas diff engine.

Compares an incoming simplified canvas snapshot (list[dict]) against the
current ContentRegistry to produce a CanvasDiff that drives which shapes
need preprocessing, position-only updates, or eviction this turn.

Public API
----------
    diff_canvas(snapshot_shapes, registry) -> CanvasDiff
"""

from __future__ import annotations

import logging

from context.models import CanvasDiff, SemanticRecord, Size, content_hash_for
from context.storage import ContentRegistry

logger = logging.getLogger(__name__)

# Shapes that move by less than this many px are treated as stationary.
# Avoids re-classifying shapes as "moved" due to floating-point drift
# from tldraw's layout engine.
_POSITION_EPSILON: float = 0.5


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def diff_canvas(
    snapshot_shapes: list[dict],
    registry: ContentRegistry,
) -> CanvasDiff:
    """Compare *snapshot_shapes* against *registry* and return a CanvasDiff.

    Classification rules
    --------------------
    new       - shape ID appears in snapshot but is absent from registry.
    updated   - shape ID exists in both; content_hash changed (text edited,
                URL swapped, color changed, etc.). Needs full reprocessing.
    moved     - shape ID exists in both; content_hash identical but layout
                changed beyond _POSITION_EPSILON. Only a coord/size update
                is needed.
    deleted   - shape ID present in registry but absent from snapshot.

    A shape that is both content-changed and moved is classified as *updated*
    (reprocessing will capture the new position anyway).

    Unchanged shapes (same hash, same position) are silently ignored.
    """
    diff = CanvasDiff()

    snapshot_index: dict[str, dict] = {
        s["id"]: s for s in snapshot_shapes if "id" in s
    }
    registry_ids: set[str] = {r.object_id for r in registry.get_all()}

    for shape_id, shape in snapshot_index.items():
        existing: SemanticRecord | None = registry.get(shape_id)

        if existing is None:
            diff.new_shapes.append(shape)
            continue

        new_hash = content_hash_for(shape)

        if new_hash != existing.content_hash:
            diff.updated_shapes.append(shape)
        elif _layout_changed(shape, existing):
            diff.moved_shapes.append(shape)

    snapshot_ids = set(snapshot_index.keys())
    diff.deleted_ids = sorted(registry_ids - snapshot_ids)

    if any([diff.new_shapes, diff.updated_shapes, diff.moved_shapes, diff.deleted_ids]):
        logger.debug(
            "canvas diff: +%d new  ~%d updated  ->%d moved  -%d deleted",
            len(diff.new_shapes),
            len(diff.updated_shapes),
            len(diff.moved_shapes),
            len(diff.deleted_ids),
        )

    return diff


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _layout_changed(shape: dict, record: SemanticRecord) -> bool:
    """Return True if the shape's position or size differs from the record."""
    dx = abs(shape.get("x", 0.0) - record.position.x)
    dy = abs(shape.get("y", 0.0) - record.position.y)
    new_w = _coerce_float(shape.get("w"))
    new_h = _coerce_float(shape.get("h"))
    old_w = record.size.w if record.size else None
    old_h = record.size.h if record.size else None
    return (
        dx > _POSITION_EPSILON
        or dy > _POSITION_EPSILON
        or _optional_changed(new_w, old_w)
        or _optional_changed(new_h, old_h)
    )


def _optional_changed(new_value: float | None, old_value: float | None) -> bool:
    if new_value is None and old_value is None:
        return False
    if new_value is None or old_value is None:
        return True
    return abs(new_value - old_value) > _POSITION_EPSILON


def _coerce_float(value: object) -> float | None:
    return None if value is None else float(value)


def apply_diff_to_registry(diff: CanvasDiff, registry: ContentRegistry) -> None:
    """Apply the layout-only and deletion parts of a diff to the registry.

    Call this *after* the preprocessor has already handled new/updated shapes.
    It handles:
      - Updating (x, y, w, h) for moved shapes.
      - Evicting deleted shapes.

    New and updated shapes are handled by the preprocessor node, which calls
    registry.set() directly after building the SemanticRecord.
    """
    for shape in diff.moved_shapes:
        sid = shape.get("id", "")
        record = registry.get(sid)
        if record is None:
            continue

        size = record.size
        new_w = _coerce_float(shape.get("w"))
        new_h = _coerce_float(shape.get("h"))
        if record.size is not None:
            size = record.size.model_copy(
                update={
                    "w": new_w if new_w is not None else record.size.w,
                    "h": new_h if new_h is not None else record.size.h,
                }
            )
        elif new_w is not None and new_h is not None:
            size = Size(w=new_w, h=new_h)

        updated = record.model_copy(
            update={
                "position": record.position.model_copy(
                    update={
                        "x": float(shape.get("x", record.position.x)),
                        "y": float(shape.get("y", record.position.y)),
                    }
                ),
                "size": size,
            }
        )
        registry.set(sid, updated)

    for sid in diff.deleted_ids:
        registry.delete(sid)

    if diff.moved_shapes or diff.deleted_ids:
        logger.debug(
            "registry patched: %d layout updates, %d evictions",
            len(diff.moved_shapes),
            len(diff.deleted_ids),
        )
