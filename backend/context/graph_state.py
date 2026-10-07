"""
context.graph_state
===================

LangGraph TypedDict state for the context-aware agent graph.

Design
------
`ContextAwareAgentState` is a superset of the existing `AgentState` defined in
`agent/graph.py`.  The required base fields (messages, canvas, room_id, _stop)
are inherited from `_RequiredAgentFields`; all context-awareness fields are
optional (total=False) so that graph nodes can return partial state updates —
LangGraph merges them automatically.

When the context-aware graph is built (next pass), it will use this state
instead of `AgentState`.  No changes to agent/graph.py are needed yet.

Field lifecycle per turn
------------------------
    1. canvas_snapshot   ← injected by the entry node from the incoming request
    2. user_message      ← injected by the entry node
    3. canvas_diff       ← computed by the diff node (snapshot vs registry)
    4. preprocessed_records ← output of the preprocessor node (new/updated)
    5. context_packet    ← assembled by the context builder node
    6. messages          ← populated with the curated prompt; fed to Claude
    7. agent_actions     ← parsed from Claude's JSON response
    8. agent_message     ← Claude's chat reply extracted from agent_actions
    9. needs_summary_update ← set True by the context builder when the board
                              has changed enough to warrant a summary refresh
"""

from __future__ import annotations

from typing import Any, Optional, TypedDict

from context.models import CanvasDiff, ContextPacket, SemanticRecord


# ---------------------------------------------------------------------------
# Required base fields
# Mirrors AgentState in agent/graph.py — kept separate so this module
# does not trigger compilation of the existing graph at import time.
# ---------------------------------------------------------------------------


class _RequiredAgentFields(TypedDict):
    """Fields that must be present from the moment the graph is invoked."""

    # Anthropic messages list: alternating user / assistant turns.
    messages: list[dict[str, Any]]

    # Simplified canvas shape list for optimistic state tracking.
    # Mutated in-place by tool_node after each agent action.
    canvas: list[dict[str, Any]]

    # Identifies which room (and therefore which ContentRegistry /
    # EventLog / SessionMemory) this invocation belongs to.
    room_id: str

    # Sentinel: True once the agent has called finish() or produced no
    # tool calls — signals the conditional edge to go to END.
    _stop: bool


# ---------------------------------------------------------------------------
# Context-awareness fields (all optional — nodes return partial updates)
# ---------------------------------------------------------------------------


class ContextAwareAgentState(_RequiredAgentFields, total=False):
    """Full graph state for the context-aware canvas agent.

    Optional fields default to absent; nodes set them as they run.
    LangGraph merges partial state dicts between nodes automatically.
    """

    # ------------------------------------------------------------------
    # Input (populated by the graph entry point before any node runs)
    # ------------------------------------------------------------------

    # Full canvas snapshot dict as received from the frontend this turn.
    # Expected keys (all optional, defaults applied where absent):
    #   "shapes":       list[dict]  — simplified tldraw shape list
    #   "viewport":     dict        — {x, y, w, h} visible region
    #   "selected_ids": list[str]   — currently selected shape IDs
    # Kept separate from `canvas` so diff logic can compare it against the
    # ContentRegistry without conflating agent-optimistic state.
    canvas_snapshot: dict[str, Any]

    # The user's natural-language message for this turn.
    user_message: str

    # ------------------------------------------------------------------
    # Diff node output
    # ------------------------------------------------------------------

    # Structured diff between canvas_snapshot and the ContentRegistry.
    # None until the diff node runs; used to decide needs_preprocessing.
    canvas_diff: Optional[CanvasDiff]

    # ------------------------------------------------------------------
    # Preprocessor node output
    # ------------------------------------------------------------------

    # SemanticRecords produced or updated this turn, ready to be written
    # into the ContentRegistry.  Empty list = no changes needed.
    preprocessed_records: list[SemanticRecord]

    # ------------------------------------------------------------------
    # Context builder node output
    # ------------------------------------------------------------------

    # The assembled context packet that the prompt-builder formats into
    # the system prompt for Claude.  None until the builder node runs.
    context_packet: Optional[ContextPacket]

    # ------------------------------------------------------------------
    # Agent node output
    # ------------------------------------------------------------------

    # Parsed list of canvas action dicts from Claude's JSON response.
    # Consumed by the tool/action dispatcher node.
    agent_actions: list[dict[str, Any]]

    # Claude's conversational reply extracted from the "message" action.
    agent_message: str

    # ------------------------------------------------------------------
    # Routing flags (set by nodes, read by conditional edges)
    # ------------------------------------------------------------------

    # True when canvas_diff contains new or updated shapes that need to
    # be preprocessed into SemanticRecords before the prompt is built.
    needs_preprocessing: bool

    # True when enough has changed (new cluster, deleted objects, significant
    # new content) that the SessionSummary should be regenerated this turn.
    needs_summary_update: bool
