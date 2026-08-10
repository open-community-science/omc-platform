"""The admin's local-model pick is authoritative for every author.

The local LLM server holds one model in memory at a time and cannot swap it per
request, so a per-user choice is not physically deliverable: whatever the admin
has loaded is what everyone gets. These pin down that precedence, and the cases
where the pin must NOT win.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import portal.app.llm_backends as lb
from portal.app.database import User


def _pin(monkeypatch, value):
    async def fake_get_site_config(key):
        return value
    monkeypatch.setattr(lb, "get_site_config", fake_get_site_config)


def _serving(monkeypatch, models):
    async def fake_list():
        return models
    monkeypatch.setattr(lb, "list_local_models", fake_list)


def test_pinned_model_overrides_the_users_saved_choice(monkeypatch):
    _serving(monkeypatch, ["site-model", "user-model"])
    _pin(monkeypatch, "site-model")

    user = User(llm_backend=lb.BACKEND_LOCAL, llm_model="user-model")
    res = asyncio.run(lb.resolve_llm(user))
    assert res["model"] == "site-model"
    assert res["label"] == "Local LLM · site-model"


def test_pinned_model_applies_to_a_user_with_no_choice(monkeypatch):
    _serving(monkeypatch, ["site-model", "other"])
    _pin(monkeypatch, "site-model")

    res = asyncio.run(lb.resolve_llm(User(llm_backend=lb.BACKEND_LOCAL)))
    assert res["model"] == "site-model"


def test_pin_is_trusted_when_the_model_list_is_unreadable(monkeypatch):
    # /models unreachable — an admin who set the pin knows what is loaded
    # better than a failed probe does.
    _serving(monkeypatch, [])
    _pin(monkeypatch, "site-model")

    user = User(llm_backend=lb.BACKEND_LOCAL, llm_model="user-model")
    assert asyncio.run(lb.resolve_llm(user))["model"] == "site-model"


def test_pin_that_is_not_being_served_does_not_win(monkeypatch):
    # The server demonstrably has other models loaded, so the pin is stale;
    # sending it would just fail the request.
    _serving(monkeypatch, ["actually-loaded"])
    _pin(monkeypatch, "stale-pin")

    user = User(llm_backend=lb.BACKEND_LOCAL, llm_model="also-not-served")
    assert asyncio.run(lb.resolve_llm(user))["model"] == "actually-loaded"


def test_without_a_pin_the_users_choice_still_wins(monkeypatch):
    _serving(monkeypatch, ["user-model", "something-else"])
    _pin(monkeypatch, None)

    user = User(llm_backend=lb.BACKEND_LOCAL, llm_model="user-model")
    assert asyncio.run(lb.resolve_llm(user))["model"] == "user-model"


# ── /settings/models?source=local serves two callers with opposite needs ──────
#
# Settings must offer only the pinned model (anything else would be a choice
# resolve_llm ignores), while the admin picker must see everything the server
# serves — choosing the pin is what that control is for. Narrowing the list for
# both is what broke the admin panel once already.

def _list_local(monkeypatch, all_models):
    # list_models_for_source imports these from llm_backends at call time, so
    # patching them there is what the endpoint actually sees.
    import portal.app.openrouter as orr
    _serving(monkeypatch, ["a-model", "b-model", "pinned-model"])
    _pin(monkeypatch, "pinned-model")
    return asyncio.run(orr.list_models_for_source(
        source="local", request=None, user=User(), all_models=all_models))


def test_settings_picker_sees_only_the_pinned_model(monkeypatch):
    data = _list_local(monkeypatch, all_models=False)
    assert [m["id"] for m in data["models"]] == ["pinned-model"]
    assert data["pinned"] is True


def test_admin_picker_sees_every_served_model(monkeypatch):
    data = _list_local(monkeypatch, all_models=True)
    assert [m["id"] for m in data["models"]] == ["a-model", "b-model", "pinned-model"]
    assert data["pinned"] is False
