"""Consumer-facing API contract tests.

These tests document and enforce the producer/consumer interface between
gs-pot and the teammate's front-end. Treat them as the spec — if the
teammate's client breaks, one of these tests should have caught it first.

Domain model:
    Property (apartment / listing)
        └── Scan (one room)
              └── scene.ply  (one Gaussian splat asset)
"""

from fastapi.testclient import TestClient


# ── Property endpoints ────────────────────────────────────────────────────────


def test_create_property_returns_201_and_property_id(client: TestClient) -> None:
    r = client.post("/properties", json={"name": "Apt 3F", "address": "123 Main St"})
    assert r.status_code == 201
    body = r.json()
    assert body["property_id"].startswith("prop_")


def test_create_property_rejects_blank_name(client: TestClient) -> None:
    r = client.post("/properties", json={"name": ""})
    assert r.status_code == 422


def test_get_property_returns_full_shape_with_scans(client: TestClient, property_id: str) -> None:
    r = client.get(f"/properties/{property_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["property_id"] == property_id
    assert body["name"] == "Test Apt 3F"
    assert body["address"] is None
    assert "created_at" in body
    assert body["scans"] == []  # no scans yet


def test_get_unknown_property_returns_404(client: TestClient) -> None:
    r = client.get("/properties/prop_nope")
    assert r.status_code == 404


def test_list_properties(client: TestClient) -> None:
    p1 = client.post("/properties", json={"name": "Apt 3F"}).json()["property_id"]
    p2 = client.post("/properties", json={"name": "Apt 5F"}).json()["property_id"]
    r = client.get("/properties")
    assert r.status_code == 200
    ids = [p["property_id"] for p in r.json()]
    assert p1 in ids and p2 in ids


# ── Scan endpoints ────────────────────────────────────────────────────────────


def test_create_scan_under_property(client: TestClient, property_id: str) -> None:
    r = client.post(
        "/scans",
        json={
            "property_id": property_id,
            "scene_name": "living_room",
            "source": "images",
        },
    )
    assert r.status_code == 202
    assert r.json()["scan_id"].startswith("scn_")


def test_create_scan_requires_property_id(client: TestClient) -> None:
    r = client.post("/scans", json={"scene_name": "x", "source": "images"})
    assert r.status_code == 422


def test_create_scan_rejects_unknown_property(client: TestClient) -> None:
    r = client.post(
        "/scans",
        json={"property_id": "prop_nope", "scene_name": "x", "source": "images"},
    )
    assert r.status_code == 400


def test_create_scan_rejects_blank_scene_name(client: TestClient, property_id: str) -> None:
    r = client.post(
        "/scans",
        json={"property_id": property_id, "scene_name": "", "source": "images"},
    )
    assert r.status_code == 422


def test_create_scan_accepts_trainer_field(client: TestClient, property_id: str) -> None:
    for trainer in ("brush", "opensplat"):
        r = client.post(
            "/scans",
            json={
                "property_id": property_id,
                "scene_name": "x",
                "source": "images",
                "trainer": trainer,
            },
        )
        assert r.status_code == 202, (trainer, r.text)


def test_create_scan_rejects_unknown_trainer(client: TestClient, property_id: str) -> None:
    r = client.post(
        "/scans",
        json={
            "property_id": property_id,
            "scene_name": "x",
            "source": "images",
            "trainer": "magic",
        },
    )
    assert r.status_code == 422


def test_get_scan_returns_full_status_shape(client: TestClient, property_id: str) -> None:
    sid = client.post(
        "/scans",
        json={"property_id": property_id, "scene_name": "kitchen", "source": "images"},
    ).json()["scan_id"]

    r = client.get(f"/scans/{sid}")
    assert r.status_code == 200
    body = r.json()
    # Spec fields the consumer relies on:
    assert body["scan_id"] == sid
    assert body["property_id"] == property_id
    assert body["scene_name"] == "kitchen"
    assert body["source"] == "images"
    assert body["status"] in {"queued", "capturing", "poses", "training", "ready", "error"}
    assert isinstance(body["progress"], float)
    assert body["scene_url"] is None
    assert body["thumb_url"] is None
    assert body["error"] is None
    assert "created_at" in body


def test_get_unknown_scan_returns_404(client: TestClient) -> None:
    r = client.get("/scans/scn_nope")
    assert r.status_code == 404


def test_property_detail_includes_its_scans(client: TestClient, property_id: str) -> None:
    sid_a = client.post(
        "/scans",
        json={"property_id": property_id, "scene_name": "living_room", "source": "images"},
    ).json()["scan_id"]
    sid_b = client.post(
        "/scans",
        json={"property_id": property_id, "scene_name": "kitchen", "source": "images"},
    ).json()["scan_id"]

    r = client.get(f"/properties/{property_id}")
    assert r.status_code == 200
    scan_ids = sorted(s["scan_id"] for s in r.json()["scans"])
    assert scan_ids == sorted([sid_a, sid_b])


def test_property_detail_does_not_leak_other_property_scans(client: TestClient) -> None:
    p1 = client.post("/properties", json={"name": "P1"}).json()["property_id"]
    p2 = client.post("/properties", json={"name": "P2"}).json()["property_id"]
    s1 = client.post(
        "/scans", json={"property_id": p1, "scene_name": "a", "source": "images"}
    ).json()["scan_id"]
    s2 = client.post(
        "/scans", json={"property_id": p2, "scene_name": "b", "source": "images"}
    ).json()["scan_id"]

    p1_scans = [s["scan_id"] for s in client.get(f"/properties/{p1}").json()["scans"]]
    p2_scans = [s["scan_id"] for s in client.get(f"/properties/{p2}").json()["scans"]]
    assert p1_scans == [s1]
    assert p2_scans == [s2]


# ── Scene asset endpoints ─────────────────────────────────────────────────────


def test_list_scenes_only_returns_ready(client: TestClient, property_id: str) -> None:
    # Create a queued scan; it should NOT appear in /scenes
    client.post(
        "/scans",
        json={"property_id": property_id, "scene_name": "den", "source": "images"},
    )
    r = client.get("/scenes")
    assert r.status_code == 200
    assert r.json() == []  # nothing ready yet


def test_get_scene_ply_404_when_missing(client: TestClient) -> None:
    r = client.get("/scenes/scn_nope.ply")
    assert r.status_code == 404


def test_get_scene_thumb_404_when_missing(client: TestClient) -> None:
    r = client.get("/scenes/scn_nope/thumb.jpg")
    assert r.status_code == 404


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_openapi_advertises_all_endpoints(client: TestClient) -> None:
    """The teammate consumes /openapi.json to generate their client."""
    r = client.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    expected = {
        "/properties",
        "/properties/{property_id}",
        "/scans",
        "/scans/{scan_id}",
        "/scenes",
        "/scenes/{scan_id}.ply",
        "/scenes/{scan_id}/thumb.jpg",
        "/api/runs/{run_id}/process",
        "/healthz",
    }
    assert expected.issubset(set(paths.keys())), set(paths.keys()) - expected


# ── Robohack run-processing webhook ───────────────────────────────────────────


def test_process_run_returns_202_scan_id_and_queue_depth(client: TestClient) -> None:
    r = client.post(
        "/api/runs/run_abc/process",
        json={"robohack_base": "http://localhost:9999", "ingest_token": "dummy"},
    )
    assert r.status_code == 202
    body = r.json()
    assert body["scan_id"].startswith("scn_")
    assert isinstance(body["queue_depth"], int)
    assert body["queue_depth"] >= 1


def test_process_run_auto_creates_property_keyed_by_run(client: TestClient) -> None:
    run_id = "run_xyz123"
    sid = client.post(
        f"/api/runs/{run_id}/process",
        json={"robohack_base": "http://localhost:9999", "ingest_token": "t"},
    ).json()["scan_id"]
    pid = f"prop_run_{run_id[:12]}"
    r = client.get(f"/properties/{pid}")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == f"run:{run_id}"
    scan_ids = [s["scan_id"] for s in body["scans"]]
    assert sid in scan_ids


def test_process_run_400_when_no_creds_in_body_or_env(
    client: TestClient, monkeypatch
) -> None:
    """Empty body + no env vars → 400 with a useful message."""
    monkeypatch.delenv("GS_POT_ROBOHACK_BASE", raising=False)
    monkeypatch.delenv("GS_POT_INGEST_TOKEN", raising=False)
    r = client.post("/api/runs/r/process", json={})
    assert r.status_code == 400
    assert "robohack_base" in r.json()["detail"]


def test_process_run_falls_back_to_env(client: TestClient, monkeypatch) -> None:
    """Body-less POST succeeds when env vars supply credentials."""
    monkeypatch.setenv("GS_POT_ROBOHACK_BASE", "http://localhost:9999")
    monkeypatch.setenv("GS_POT_INGEST_TOKEN", "env-token")
    r = client.post("/api/runs/run_env/process", json={})
    assert r.status_code == 202
    assert r.json()["scan_id"].startswith("scn_")


def test_process_run_accepts_trainer_and_quality_overrides(client: TestClient) -> None:
    r = client.post(
        "/api/runs/r/process",
        json={
            "robohack_base": "http://localhost:9999",
            "ingest_token": "t",
            "trainer": "opensplat",
            "steps": 2500,
            "quality": "medium",
            "scene_name": "kitchen-run",
        },
    )
    assert r.status_code == 202


def test_cors_preflight_open(client: TestClient) -> None:
    """Front-end at robohack's domain must be able to POST cross-origin."""
    r = client.options(
        "/api/runs/r/process",
        headers={
            "Origin": "https://robohack.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "*"
