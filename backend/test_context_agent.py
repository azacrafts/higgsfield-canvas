import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.prompts import ACTION_SCHEMA
from chat_agent import _build_moodboard_actions
from chat_agent import _moodboard_signatures
from chat_agent import router as chat_router
from context.diff import apply_diff_to_registry, diff_canvas
from context.models import ContextPacket, SessionSummary
from context.models import ObjectType, Position, SemanticRecord, Size, content_hash_for
from context.preprocessors import preprocess_shape
from context.prompt_builder import build_messages
from context.storage import ContentRegistry


class ContextDiffTests(unittest.TestCase):
    def test_diff_treats_resize_as_layout_change(self) -> None:
        registry = ContentRegistry()
        original = {
            "id": "shape:box",
            "type": "geo",
            "x": 120,
            "y": 240,
            "w": 220,
            "h": 120,
            "text": "Auth service",
            "color": "blue",
            "geo": "rectangle",
        }
        registry.set(
            "shape:box",
            SemanticRecord(
                object_id="shape:box",
                object_type=ObjectType.shape,
                position=Position(x=120, y=240),
                size=Size(w=220, h=120),
                content_summary='rectangle shape: "Auth service"',
                content_hash=content_hash_for(original),
            ),
        )

        resized = dict(original, w=320, h=180)
        diff = diff_canvas([resized], registry)

        self.assertEqual(diff.updated_shapes, [])
        self.assertEqual(len(diff.moved_shapes), 1)

        apply_diff_to_registry(diff, registry)
        updated = registry.get("shape:box")
        self.assertIsNotNone(updated)
        self.assertEqual(updated.size.w, 320)
        self.assertEqual(updated.size.h, 180)

    def test_content_hash_tracks_arrow_endpoints(self) -> None:
        left = {
            "id": "shape:arrow",
            "type": "arrow",
            "x": 0,
            "y": 0,
            "text": "",
            "fromId": "shape:a",
            "toId": "shape:b",
        }
        right = dict(left, toId="shape:c")

        self.assertNotEqual(content_hash_for(left), content_hash_for(right))


class ContextPreprocessorTests(unittest.TestCase):
    def test_preprocess_arrow_preserves_connections(self) -> None:
        record = asyncio.run(
            preprocess_shape(
                {
                    "id": "shape:arrow",
                    "type": "arrow",
                    "x": 10,
                    "y": 20,
                    "text": "depends on",
                    "fromId": "shape:service",
                    "toId": "shape:db",
                }
            )
        )

        self.assertEqual(record.object_type, ObjectType.arrow)
        self.assertEqual(record.connections, ["shape:service", "shape:db"])
        self.assertIn("from:shape:service", record.content_summary)
        self.assertIn("to:shape:db", record.content_summary)


class ChatRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        _moodboard_signatures.clear()
        app = FastAPI()
        app.include_router(chat_router)
        self.client = TestClient(app)

    @patch("chat_agent.run_context_agent", new_callable=AsyncMock)
    def test_chat_stream_uses_canvas_snapshot_payload(
        self,
        mock_run_context_agent: AsyncMock,
    ) -> None:
        snapshot = {
            "shapes": [
                {
                    "id": "shape:note",
                    "type": "note",
                    "x": 50,
                    "y": 80,
                    "text": "Current focus",
                    "color": "yellow",
                }
            ],
            "viewport": {"x": 0, "y": 0, "w": 1200, "h": 800},
            "selected_ids": ["shape:note"],
        }
        mock_run_context_agent.return_value = [{"_type": "message", "text": "Integrated"}]

        response = self.client.post(
            "/api/chat/stream",
            json={
                "message": "Summarize this cluster",
                "room_id": "room-7",
                "canvas_state": [],
                "canvas_snapshot": snapshot,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('"type": "action"', response.text)
        self.assertIn('"Integrated"', response.text)
        mock_run_context_agent.assert_awaited_once_with(
            message="Summarize this cluster",
            canvas_snapshot=snapshot,
            room_id="room-7",
        )

    @patch("chat_agent.run_context_agent", new_callable=AsyncMock)
    def test_chat_stream_wraps_legacy_canvas_state_when_needed(
        self,
        mock_run_context_agent: AsyncMock,
    ) -> None:
        canvas_state = [
            {
                "id": "shape:box",
                "type": "geo",
                "x": 100,
                "y": 120,
                "text": "Fallback canvas",
                "color": "blue",
                "geo": "rectangle",
            }
        ]
        mock_run_context_agent.return_value = [{"_type": "message", "text": "Fallback works"}]

        response = self.client.post(
            "/api/chat/stream",
            json={"message": "Use fallback", "canvas_state": canvas_state},
        )

        self.assertEqual(response.status_code, 200)
        mock_run_context_agent.assert_awaited_once_with(
            message="Use fallback",
            canvas_snapshot={"shapes": canvas_state, "selected_ids": []},
            room_id="main",
        )

    @patch("chat_agent.fetch_pinterest_images")
    @patch("chat_agent.run_context_agent", new_callable=AsyncMock)
    def test_chat_stream_builds_pinterest_only_moodboard_actions(
        self,
        mock_run_context_agent: AsyncMock,
        mock_fetch_pinterest_images,
    ) -> None:
        pinterest_images = [
            {"url": "https://example.com/1.jpg", "title": "dark academia"},
            {"url": "https://example.com/2.jpg", "title": "dark academia"},
        ]
        mock_fetch_pinterest_images.return_value = pinterest_images

        response = self.client.post(
            "/api/chat/stream",
            json={"message": "Make a moodboard for dark academia", "canvas_state": []},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('"create_image"', response.text)
        self.assertNotIn('"generate_image"', response.text)
        mock_run_context_agent.assert_not_awaited()
        mock_fetch_pinterest_images.assert_called_once()

    def test_moodboard_verify_deduplicates_same_note_text(self) -> None:
        first = self.client.post(
            "/api/moodboard/verify",
            json={
                "room_id": "room-1",
                "shape_id": "shape:note-1",
                "text": "Create a moodboard for brutalist interiors",
            },
        )
        second = self.client.post(
            "/api/moodboard/verify",
            json={
                "room_id": "room-1",
                "shape_id": "shape:note-1",
                "text": "Create a moodboard for brutalist interiors",
            },
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(first.json()["should_trigger"])
        self.assertFalse(second.json()["should_trigger"])
        self.assertEqual(second.json()["reason"], "duplicate")

    def test_build_moodboard_actions_places_board_outside_occupied_area(self) -> None:
        actions = _build_moodboard_actions(
            "dark academia",
            [
                {"url": "https://example.com/1.jpg", "title": "dark academia"},
                {"url": "https://example.com/2.jpg", "title": "dark academia"},
                {"url": "https://example.com/3.jpg", "title": "dark academia"},
                {"url": "https://example.com/4.jpg", "title": "dark academia"},
                {"url": "https://example.com/5.jpg", "title": "dark academia"},
            ],
            {
                "shapes": [
                    {
                        "id": "shape:occupied",
                        "type": "image",
                        "x": 40,
                        "y": 60,
                        "w": 600,
                        "h": 500,
                    }
                ]
            },
        )

        placed_images = [action for action in actions if action["_type"] == "create_image"]
        self.assertEqual(len(placed_images), 5)
        self.assertTrue(all(action["x"] >= 664 for action in placed_images))
        self.assertTrue(all(action["_type"] != "generate_image" for action in actions))

    def test_build_moodboard_actions_prefers_free_space_near_anchor_note(self) -> None:
        actions = _build_moodboard_actions(
            "brutalist interiors",
            [
                {"url": "https://example.com/1.jpg", "title": "brutalist"},
                {"url": "https://example.com/2.jpg", "title": "brutalist"},
                {"url": "https://example.com/3.jpg", "title": "brutalist"},
                {"url": "https://example.com/4.jpg", "title": "brutalist"},
                {"url": "https://example.com/5.jpg", "title": "brutalist"},
            ],
            {
                "shapes": [
                    {
                        "id": "shape:note-1",
                        "type": "note",
                        "x": 120,
                        "y": 140,
                        "w": 200,
                        "h": 200,
                    },
                    {
                        "id": "shape:block-right",
                        "type": "image",
                        "x": 380,
                        "y": 100,
                        "w": 620,
                        "h": 300,
                    },
                ]
            },
            anchor_shape_id="shape:note-1",
        )

        placed_images = [action for action in actions if action["_type"] == "create_image"]
        self.assertEqual(len(placed_images), 5)
        self.assertTrue(all(action["y"] >= 396 for action in placed_images))
        self.assertIn('near the triggering note', actions[-1]["text"])


class PromptCapabilityTests(unittest.TestCase):
    def test_shared_action_schema_includes_media_generation(self) -> None:
        self.assertIn('"create_image"', ACTION_SCHEMA)
        self.assertIn('"generate_image"', ACTION_SCHEMA)
        self.assertIn('"generate_video"', ACTION_SCHEMA)

    def test_context_prompt_mentions_image_generation(self) -> None:
        system_prompt, _ = build_messages(
            ContextPacket(
                user_message="Generate a horse-inspired leather texture",
                session_summary=SessionSummary(),
            )
        )

        self.assertIn("create_image", system_prompt)
        self.assertIn("generate_image", system_prompt)
        self.assertIn("sourceImageShapeId", system_prompt)


if __name__ == "__main__":
    unittest.main()
