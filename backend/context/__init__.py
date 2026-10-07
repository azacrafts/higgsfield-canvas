"""
context — canvas context-awareness subsystem
=============================================

This package makes the AI deeply aware of everything on the canvas without
dumping the entire raw board into every prompt.

Pattern:
    Raw canvas object
      → type-specific preprocessor  (runs once on create/update, cached)
      → SemanticRecord               (lightweight, meaningful summary)
      → ContentRegistry              (in-memory store, per room)
      → relevance retrieval          (selected / nearby / retrieved tiers)
      → ContextPacket                (what Claude actually sees each turn)

Public surface
--------------
Models   : SemanticRecord, CanvasEvent, SessionSummary, ContextPacket,
           CanvasDiff, ObjectType, EventType, Position, Size
           content_hash_for()
Storage  : ContentRegistry, EventLog, SessionMemory,
           RoomContextStore, context_store
State    : ContextAwareAgentState
Diff     : diff_canvas(), apply_diff_to_registry()
Preproc  : preprocess_shape(), should_reprocess()
Retrieval: retrieve_relevant()
Assembly : build_context_packet()
"""

from context.models import (
    CanvasDiff,
    CanvasEvent,
    ContextPacket,
    EventType,
    ObjectType,
    Position,
    SemanticRecord,
    SessionSummary,
    Size,
    content_hash_for,
)
from context.storage import (
    ContentRegistry,
    ContextStore,
    EventLog,
    RoomContextStore,
    SessionMemory,
    context_store,
)
from context.graph_state import ContextAwareAgentState
from context.diff import apply_diff_to_registry, diff_canvas
from context.preprocessors import preprocess_shape, should_reprocess
from context.retrieval import retrieve_relevant
from context.assembly import build_context_packet
from context.prompt_builder import build_messages
from context.session_updater import update_session_summary
from context.graph import context_aware_graph, run_context_agent

__all__ = [
    # models
    "CanvasDiff",
    "CanvasEvent",
    "ContextPacket",
    "EventType",
    "ObjectType",
    "Position",
    "SemanticRecord",
    "SessionSummary",
    "Size",
    "content_hash_for",
    # storage
    "ContentRegistry",
    "ContextStore",
    "EventLog",
    "RoomContextStore",
    "SessionMemory",
    "context_store",
    # graph state
    "ContextAwareAgentState",
    # diff engine
    "diff_canvas",
    "apply_diff_to_registry",
    # preprocessors
    "preprocess_shape",
    "should_reprocess",
    # retrieval
    "retrieve_relevant",
    # assembly
    "build_context_packet",
    # prompt builder
    "build_messages",
    # session updater
    "update_session_summary",
    # graph
    "context_aware_graph",
    "run_context_agent",
]
