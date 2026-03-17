"""Minimal JSON-file data layer for Chainlit thread persistence.

Stores threads as JSON files in /app/.omc/threads/. Enables the sidebar
with multiple conversation threads (one per pipeline phase).
"""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from chainlit.data.base import BaseDataLayer
from chainlit.types import Feedback, Pagination, PaginatedResponse, ThreadDict, ThreadFilter

THREADS_DIR = Path("/app/.omc/threads")


class JSONDataLayer(BaseDataLayer):
    """File-based data layer using one JSON file per thread."""

    def __init__(self):
        THREADS_DIR.mkdir(parents=True, exist_ok=True)

    def build_debug_url(self) -> str:
        return ""

    async def close(self) -> None:
        pass

    def _thread_path(self, thread_id: str) -> Path:
        return THREADS_DIR / f"{thread_id}.json"

    def _read_thread(self, thread_id: str) -> Optional[dict]:
        path = self._thread_path(thread_id)
        if path.exists():
            try:
                t = json.loads(path.read_text())
                # Ensure required fields exist (backfill old threads)
                t.setdefault("userId", "author")
                t.setdefault("userIdentifier", "author")
                t.setdefault("name", None)
                t.setdefault("tags", [])
                t.setdefault("metadata", {})
                t.setdefault("steps", [])
                t.setdefault("elements", [])
                return t
            except (json.JSONDecodeError, OSError):
                return None
        return None

    def _write_thread(self, thread: dict):
        path = self._thread_path(thread["id"])
        path.write_text(json.dumps(thread, indent=2, default=str))

    # ── Users (minimal — single-user containers) ────────────────────────────

    async def get_user(self, identifier: str):
        from chainlit.user import PersistedUser
        return PersistedUser(id=identifier, identifier=identifier, createdAt=datetime.now(timezone.utc).isoformat())

    async def create_user(self, user):
        from chainlit.user import PersistedUser
        return PersistedUser(id=user.identifier, identifier=user.identifier, createdAt=datetime.now(timezone.utc).isoformat())

    # ── Threads ─────────────────────────────────────────────────────────────

    async def list_threads(self, pagination: Pagination, filters: ThreadFilter) -> PaginatedResponse[ThreadDict]:
        threads = []
        for path in sorted(THREADS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                t = json.loads(path.read_text())
                threads.append(t)
            except (json.JSONDecodeError, OSError):
                continue

        # Apply search filter
        if filters.search:
            search = filters.search.lower()
            threads = [t for t in threads if search in (t.get("name") or "").lower()]

        return PaginatedResponse(data=threads, pageInfo={"hasNextPage": False, "startCursor": None, "endCursor": None})

    async def get_thread(self, thread_id: str) -> Optional[ThreadDict]:
        return self._read_thread(thread_id)

    async def get_thread_author(self, thread_id: str) -> str:
        t = self._read_thread(thread_id)
        return (t or {}).get("userIdentifier", "author")

    async def update_thread(self, thread_id: str, name=None, user_id=None, metadata=None, tags=None):
        t = self._read_thread(thread_id)
        if not t:
            t = {
                "id": thread_id,
                "createdAt": datetime.now(timezone.utc).isoformat(),
                "userId": "author",
                "userIdentifier": "author",
                "steps": [],
                "elements": [],
            }
        # Ensure these always exist
        t.setdefault("userId", "author")
        t.setdefault("userIdentifier", "author")
        t.setdefault("name", None)
        t.setdefault("tags", [])
        t.setdefault("metadata", {})
        if name is not None:
            t["name"] = name
        if user_id is not None:
            t["userId"] = user_id
            t["userIdentifier"] = user_id
        if metadata is not None:
            t["metadata"] = metadata
        if tags is not None:
            t["tags"] = tags
        self._write_thread(t)

    async def delete_thread(self, thread_id: str):
        path = self._thread_path(thread_id)
        if path.exists():
            path.unlink()

    # ── Steps (messages) ────────────────────────────────────────────────────

    async def create_step(self, step_dict: dict):
        thread_id = step_dict.get("threadId")
        if not thread_id:
            return
        t = self._read_thread(thread_id)
        if not t:
            t = {
                "id": thread_id,
                "createdAt": datetime.now(timezone.utc).isoformat(),
                "steps": [],
                "elements": [],
            }
        # Avoid duplicates
        existing_ids = {s["id"] for s in t.get("steps", [])}
        if step_dict.get("id") not in existing_ids:
            t.setdefault("steps", []).append(step_dict)
        self._write_thread(t)

    async def update_step(self, step_dict: dict):
        thread_id = step_dict.get("threadId")
        if not thread_id:
            return
        t = self._read_thread(thread_id)
        if not t:
            return
        steps = t.get("steps", [])
        for i, s in enumerate(steps):
            if s.get("id") == step_dict.get("id"):
                steps[i] = step_dict
                break
        self._write_thread(t)

    async def delete_step(self, step_id: str):
        # Not critical — skip
        pass

    # ── Elements (files, images — not used yet) ─────────────────────────────

    async def create_element(self, element):
        pass

    async def get_element(self, thread_id: str, element_id: str):
        return None

    async def delete_element(self, element_id: str, thread_id=None):
        pass

    # ── Feedback (not used) ─────────────────────────────────────────────────

    async def upsert_feedback(self, feedback: Feedback) -> str:
        return str(uuid.uuid4())

    async def delete_feedback(self, feedback_id: str) -> bool:
        return True

    # ── Favorites (not used) ────────────────────────────────────────────────

    async def get_favorite_steps(self, user_id: str):
        return []

    async def set_step_favorite(self, step_dict, favorite: bool):
        return step_dict
