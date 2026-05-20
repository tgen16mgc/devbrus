"""Tests for operator cockpit backend APIs."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from starlette.testclient import TestClient

from backend import main


def test_profile_operator_fields_roundtrip(app_client: TestClient):
    resp = app_client.post(
        "/api/profiles",
        json={
            "name": "Grouped",
            "group": "buyers",
            "sort_order": 7,
            "proxy_status": "unchecked",
        },
    )

    assert resp.status_code == 201
    data = resp.json()
    assert data["group"] == "buyers"
    assert data["sort_order"] == 7
    assert data["proxy_status"] == "unchecked"


def test_operator_import_csv_creates_profiles_and_allows_no_proxy(app_client: TestClient):
    csv_text = "\n".join(
        [
            "profile_name,proxy_url,group,notes",
            "Alpha,,local,No proxy profile",
            "Beta,http://proxy.example:8080,remote,Has proxy",
        ]
    )

    resp = app_client.post("/api/operator/import-csv", json={"csv_text": csv_text})

    assert resp.status_code == 200
    data = resp.json()
    assert data["created"] == 2
    assert data["skipped"] == []
    assert data["invalid"] == []
    assert len(data["profile_ids"]) == 2

    profiles = {p["name"]: p for p in app_client.get("/api/profiles").json()}
    assert profiles["Alpha"]["proxy"] is None
    assert profiles["Alpha"]["proxy_status"] == "no_proxy"
    assert profiles["Beta"]["group"] == "remote"


def test_operator_import_csv_skips_duplicate_and_invalid_proxy(app_client: TestClient):
    app_client.post("/api/profiles", json={"name": "Existing"})
    csv_text = "\n".join(
        [
            "name,proxy,group,notes",
            "Existing,,team,Duplicate",
            "Bad Proxy,ftp://proxy.example:21,team,Bad",
            "Good,http://proxy.example:8080,team,Good",
        ]
    )

    resp = app_client.post(
        "/api/operator/import-csv",
        json={"csv_text": csv_text, "validate_proxies": True},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["created"] == 1
    assert data["skipped"][0]["name"] == "Existing"
    assert data["invalid"][0]["name"] == "Bad Proxy"
    assert len(data["profile_ids"]) == 1


def test_operator_bulk_launch_returns_partial_results(app_client: TestClient, monkeypatch):
    first = app_client.post("/api/profiles", json={"name": "Launch OK"}).json()
    second = app_client.post("/api/profiles", json={"name": "Launch Fail"}).json()

    async def fake_launch(profile):
        if profile["id"] == second["id"]:
            raise RuntimeError("boom")
        running = MagicMock()
        running.ws_port = 6100
        running.display = 100
        return running

    monkeypatch.setattr(main.browser_mgr, "launch", AsyncMock(side_effect=fake_launch))

    resp = app_client.post(
        "/api/operator/bulk",
        json={"action": "launch", "profile_ids": [first["id"], second["id"], "missing"]},
    )

    assert resp.status_code == 200
    results = {r["profile_id"]: r for r in resp.json()["results"]}
    assert results[first["id"]]["status"] == "ok"
    assert results[second["id"]]["status"] == "error"
    assert "boom" in results[second["id"]]["detail"]
    assert results["missing"]["status"] == "error"


def test_operator_automation_open_url_and_inspect(app_client: TestClient):
    profile = app_client.post("/api/profiles", json={"name": "Automate"}).json()

    page = MagicMock()
    page.url = "about:blank"
    page.goto = AsyncMock(return_value=MagicMock(status=204))
    page.title = AsyncMock(return_value="Current Page")

    context = MagicMock()
    context.pages = [page]

    running = MagicMock()
    running.context = context
    main.browser_mgr.running[profile["id"]] = running

    open_resp = app_client.post(
        "/api/operator/automation",
        json={
            "action": "open_url",
            "profile_ids": [profile["id"]],
            "url": "https://example.com",
        },
    )
    assert open_resp.status_code == 200
    assert open_resp.json()["results"][0]["status"] == "ok"
    page.goto.assert_awaited_once_with("https://example.com")

    page.url = "https://example.com"
    inspect_resp = app_client.post(
        "/api/operator/automation",
        json={"action": "inspect", "profile_ids": [profile["id"]]},
    )
    assert inspect_resp.status_code == 200
    result = inspect_resp.json()["results"][0]
    assert result["status"] == "ok"
    assert result["url"] == "https://example.com"
    assert result["title"] == "Current Page"

    main.browser_mgr.running.pop(profile["id"], None)


def test_operator_automation_applies_concurrency_limit(app_client: TestClient):
    profiles = [
        app_client.post("/api/profiles", json={"name": f"Automate {idx}"}).json()
        for idx in range(3)
    ]
    active = 0
    max_active = 0

    async def slow_title():
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return "Current Page"

    for profile in profiles:
        page = MagicMock()
        page.url = "https://example.com"
        page.title = AsyncMock(side_effect=slow_title)

        context = MagicMock()
        context.pages = [page]

        running = MagicMock()
        running.context = context
        main.browser_mgr.running[profile["id"]] = running

    resp = app_client.post(
        "/api/operator/automation",
        json={
            "action": "inspect",
            "profile_ids": [profile["id"] for profile in profiles],
            "concurrency": 1,
        },
    )

    assert resp.status_code == 200
    assert [result["status"] for result in resp.json()["results"]] == ["ok", "ok", "ok"]
    assert max_active == 1

    for profile in profiles:
        main.browser_mgr.running.pop(profile["id"], None)


def test_operator_native_grid_arranges_running_profiles(app_client: TestClient, monkeypatch):
    first = app_client.post("/api/profiles", json={"name": "Native One"}).json()
    second = app_client.post("/api/profiles", json={"name": "Native Two"}).json()
    missing = app_client.post("/api/profiles", json={"name": "Stopped"}).json()

    main.browser_mgr.running[first["id"]] = MagicMock()
    main.browser_mgr.running[second["id"]] = MagicMock()

    calls = []

    def fake_grid_native_windows(*, titles, columns, rows, bounds, gap, scale, strategy, apply):
        calls.append(
            {
                "titles": titles,
                "columns": columns,
                "rows": rows,
                "bounds": bounds,
                "gap": gap,
                "scale": scale,
                "strategy": strategy,
                "apply": apply,
            }
        )
        return [
            main.native_window_helper.WindowFrame("Native One", 0, 0, 395, 295),
            main.native_window_helper.WindowFrame("Native Two", 405, 0, 395, 295),
        ]

    monkeypatch.setattr(main.native_window_helper, "grid_native_windows", fake_grid_native_windows)

    resp = app_client.post(
        "/api/operator/native-grid",
        json={
            "profile_ids": [first["id"], second["id"], missing["id"]],
            "columns": 2,
            "rows": 2,
            "bounds": {"left": 0, "top": 0, "width": 800, "height": 600},
            "gap": 10,
            "scale": 1,
            "strategy": "index",
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "partial"
    assert data["results"] == [
        {"profile_id": first["id"], "status": "ok", "detail": "arranged"},
        {"profile_id": second["id"], "status": "ok", "detail": "arranged"},
        {"profile_id": missing["id"], "status": "error", "detail": "Profile is not running"},
    ]
    assert data["frames"][0] == {"title": "Native One", "left": 0, "top": 0, "width": 395, "height": 295}
    assert calls[0]["titles"] == ["Native One", "Native Two"]
    assert calls[0]["strategy"] == "index"
    assert calls[0]["apply"] is True

    main.browser_mgr.running.pop(first["id"], None)
    main.browser_mgr.running.pop(second["id"], None)


def test_operator_layouts_crud_and_event_log(app_client: TestClient):
    create = app_client.post(
        "/api/operator/layouts",
        json={
            "name": "Desk",
            "mode": "dashboard",
            "columns": 3,
            "rows": 2,
            "tile_scale": 0.75,
            "monitor": "main",
            "profile_order": ["a", "b"],
            "group": "buyers",
        },
    )
    assert create.status_code == 201
    layout = create.json()
    assert layout["name"] == "Desk"

    listed = app_client.get("/api/operator/layouts")
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == layout["id"]

    update = app_client.put(
        f"/api/operator/layouts/{layout['id']}",
        json={"name": "Desk 2", "columns": 4},
    )
    assert update.status_code == 200
    assert update.json()["name"] == "Desk 2"
    assert update.json()["columns"] == 4

    delete = app_client.delete(f"/api/operator/layouts/{layout['id']}")
    assert delete.status_code == 200
    assert app_client.get("/api/operator/layouts").json() == []

    events = app_client.get("/api/operator/events").json()
    event_types = [e["event_type"] for e in events]
    assert "layout_save" in event_types
    assert "layout_delete" in event_types


def test_operator_metadata_export_import_skips_duplicate_profiles(app_client: TestClient):
    app_client.post("/api/profiles", json={"name": "Exported", "group": "team"})
    app_client.post(
        "/api/operator/layouts",
        json={
            "name": "Export Layout",
            "mode": "native",
            "columns": 2,
            "rows": 2,
            "tile_scale": 1,
            "monitor": "external",
            "profile_order": [],
            "group": None,
        },
    )

    exported = app_client.get("/api/operator/export-metadata").json()
    import_resp = app_client.post("/api/operator/import-metadata", json=exported)

    assert import_resp.status_code == 200
    data = import_resp.json()
    assert data["profiles"]["created"] == 0
    assert data["profiles"]["skipped"] == 1
    assert data["layouts"]["created"] == 0
    assert data["layouts"]["skipped"] == 1


def test_operator_metadata_import_imports_events(app_client: TestClient):
    import_resp = app_client.post(
        "/api/operator/import-metadata",
        json={
            "profiles": [],
            "layouts": [],
            "events": [
                {
                    "id": "event-1",
                    "event_type": "csv_import",
                    "detail": {"created": 2},
                    "created_at": "2026-05-18T00:00:00+00:00",
                }
            ],
        },
    )

    assert import_resp.status_code == 200
    data = import_resp.json()
    assert data["events"]["imported"] == 1
    assert data["events"]["skipped"] == 0

    events = app_client.get("/api/operator/events").json()
    imported = [event for event in events if event["id"] == "event-1"]
    assert imported[0]["event_type"] == "csv_import"
    assert imported[0]["detail"] == {"created": 2}


def test_operator_metadata_import_skips_malformed_events_without_500(app_client: TestClient):
    import_resp = app_client.post(
        "/api/operator/import-metadata",
        json={
            "profiles": [],
            "layouts": [],
            "events": [
                {"id": "bad-event", "event_type": 123, "detail": {"bad": True}},
                {"id": "good-event", "event_type": "automation_action", "detail": {"ok": 1}},
            ],
        },
    )

    assert import_resp.status_code == 200
    data = import_resp.json()
    assert data["events"]["imported"] == 1
    assert data["events"]["skipped"] == 1

    events = app_client.get("/api/operator/events").json()
    event_ids = {event["id"] for event in events}
    assert "good-event" in event_ids
    assert "bad-event" not in event_ids


def test_operator_metadata_import_skips_invalid_profiles_and_layouts(app_client: TestClient):
    import_resp = app_client.post(
        "/api/operator/import-metadata",
        json={
            "profiles": [
                {"name": "Bad Profile", "platform": "android"},
                {"name": "Good Profile", "platform": "windows"},
            ],
            "layouts": [
                {
                    "name": "Bad Layout",
                    "mode": "dashboard",
                    "columns": 0,
                    "rows": 1,
                    "tile_scale": 1,
                },
                {
                    "name": "Good Layout",
                    "mode": "dashboard",
                    "columns": 1,
                    "rows": 1,
                    "tile_scale": 1,
                },
            ],
        },
    )

    assert import_resp.status_code == 200
    data = import_resp.json()
    assert data["profiles"]["created"] == 1
    assert data["profiles"]["skipped"] == 1
    assert data["profiles"]["invalid"][0]["name"] == "Bad Profile"
    assert data["layouts"]["created"] == 1
    assert data["layouts"]["skipped"] == 1
    assert data["layouts"]["invalid"][0]["name"] == "Bad Layout"

    profiles = {profile["name"] for profile in app_client.get("/api/profiles").json()}
    assert "Good Profile" in profiles
    assert "Bad Profile" not in profiles

    layouts_resp = app_client.get("/api/operator/layouts")
    assert layouts_resp.status_code == 200
    layouts = {layout["name"] for layout in layouts_resp.json()}
    assert "Good Layout" in layouts
    assert "Bad Layout" not in layouts


def test_operator_metadata_import_skips_non_string_names_without_500(app_client: TestClient):
    import_resp = app_client.post(
        "/api/operator/import-metadata",
        json={
            "profiles": [
                {"name": 123, "platform": "windows"},
                {"name": "Good Typed Profile", "platform": "windows"},
            ],
            "layouts": [
                {
                    "name": 456,
                    "mode": "dashboard",
                    "columns": 1,
                    "rows": 1,
                    "tile_scale": 1,
                },
                {
                    "name": "Good Typed Layout",
                    "mode": "dashboard",
                    "columns": 1,
                    "rows": 1,
                    "tile_scale": 1,
                },
            ],
        },
    )

    assert import_resp.status_code == 200
    data = import_resp.json()
    assert data["profiles"]["created"] == 1
    assert data["profiles"]["skipped"] == 1
    assert data["layouts"]["created"] == 1
    assert data["layouts"]["skipped"] == 1

    profiles = {profile["name"] for profile in app_client.get("/api/profiles").json()}
    assert "Good Typed Profile" in profiles

    layouts_resp = app_client.get("/api/operator/layouts")
    assert layouts_resp.status_code == 200
    layouts = {layout["name"] for layout in layouts_resp.json()}
    assert "Good Typed Layout" in layouts


def test_operator_import_csv_rejects_oversized_body(app_client: TestClient):
    resp = app_client.post(
        "/api/operator/import-csv",
        json={"csv_text": "x" * 1_048_577},
    )

    assert resp.status_code == 422


def test_operator_import_csv_rejects_more_than_500_rows_without_creating(app_client: TestClient):
    rows = ["profile_name,proxy_url,group,notes"]
    rows.extend(f"Profile {idx},,,note" for idx in range(501))

    resp = app_client.post(
        "/api/operator/import-csv",
        json={"csv_text": "\n".join(rows)},
    )

    assert resp.status_code == 400
    assert "500" in resp.json()["detail"]
    assert app_client.get("/api/profiles").json() == []


def test_operator_bulk_rejects_abusive_profile_id_list(app_client: TestClient):
    resp = app_client.post(
        "/api/operator/bulk",
        json={
            "action": "stop",
            "profile_ids": [f"profile-{idx}" for idx in range(101)],
        },
    )

    assert resp.status_code == 422


def test_operator_automation_rejects_abusive_profile_id_list(app_client: TestClient):
    resp = app_client.post(
        "/api/operator/automation",
        json={
            "action": "inspect",
            "profile_ids": [f"profile-{idx}" for idx in range(101)],
        },
    )

    assert resp.status_code == 422
