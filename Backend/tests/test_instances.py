"""Register/manage-instances CRUD + scan-pickup test.

Uses a unique temp instance id in the existing "Production" group and cleans up
after itself so the seeded inventory is left intact.
"""
import importlib

TEMP_ID = "i-pytest-temp-0001"


def _client():
    app_mod = importlib.import_module("app")
    flask_app = app_mod.create_app()
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


def _ids_in_scan(client):
    scan = client.post("/api/scan").get_json()
    return {r["instance_id"] for r in scan["results"]}


def test_instance_crud_and_scan_pickup():
    c = _client()
    # ensure clean slate
    c.delete(f"/api/instances/{TEMP_ID}")

    # create
    resp = c.post("/api/instances", json={
        "id": TEMP_ID, "name": "Pytest Temp", "role": "backend",
        "host": "", "group_name": "Production",
    })
    assert resp.status_code == 201, resp.get_data(as_text=True)
    assert resp.get_json()["group"] == "Production"

    # duplicate rejected
    assert c.post("/api/instances", json={
        "id": TEMP_ID, "name": "dup", "role": "db", "group_name": "Production",
    }).status_code == 409

    # listed + picked up by a scan
    listing = {i["id"] for i in c.get("/api/instances").get_json()}
    assert TEMP_ID in listing
    assert TEMP_ID in _ids_in_scan(c)

    # disable -> drops out of the scan
    assert c.patch(f"/api/instances/{TEMP_ID}", json={"enabled": False}).status_code == 200
    assert TEMP_ID not in _ids_in_scan(c)

    # delete -> gone
    assert c.delete(f"/api/instances/{TEMP_ID}").status_code == 200
    assert TEMP_ID not in {i["id"] for i in c.get("/api/instances").get_json()}


def test_create_validation():
    c = _client()
    assert c.post("/api/instances", json={"name": "x", "role": "db"}).status_code == 400  # no id
    assert c.post("/api/instances", json={
        "id": "i-novalidgroup", "name": "x", "role": "db",
    }).status_code == 400  # no group
