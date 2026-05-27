import pytest
from fastapi.testclient import TestClient

from gs_pot.server import app
from gs_pot.store import get_property_store, get_store


@pytest.fixture
def client() -> TestClient:
    get_store().clear()
    get_property_store().clear()
    return TestClient(app)


@pytest.fixture
def property_id(client: TestClient) -> str:
    """Create a fresh property and return its id — most tests need one."""
    r = client.post("/properties", json={"name": "Test Apt 3F"})
    assert r.status_code == 201
    return r.json()["property_id"]
