"""API contract + input hygiene. /api/* answers JSON, never an HTML 500."""


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.get_json()["ok"] is True


def test_add_list_toggle_delete(client):
    r = client.post("/api/items", json={"text": "  hello   world  "})
    assert r.status_code == 201
    item = r.get_json()["item"]
    assert item["text"] == "hello world"          # whitespace-collapsed at the edge

    assert client.put(f"/api/items/{item['id']}", json={"done": True}).get_json()["item"]["done"] == 1
    rows = client.get("/api/items").get_json()["items"]
    assert rows[0]["done"] == 1

    assert client.delete(f"/api/items/{item['id']}").status_code == 200
    assert client.get("/api/items").get_json()["items"] == []


def test_input_hygiene(client):
    assert client.post("/api/items", data="not json").status_code == 400
    assert client.post("/api/items", json=[1, 2]).status_code == 400
    assert client.post("/api/items", json={}).status_code == 400
    assert client.post("/api/items", json={"text": "   "}).status_code == 400
    assert client.post("/api/items", json={"text": 42}).status_code == 400
    it = client.post("/api/items", json={"text": "x"}).get_json()["item"]
    assert client.put(f"/api/items/{it['id']}", json={"done": "yes"}).status_code == 400
    assert client.put(f"/api/items/{it['id']}", json={"text": ""}).status_code == 400
    assert client.put("/api/items/999999", json={"done": True}).status_code == 404
    assert client.delete("/api/items/999999").status_code == 404


def test_errors_under_api_are_json(client):
    for r in (client.post("/api/items", data="x"),
              client.get("/api/items/999999"),
              client.delete("/api/items/nope")):
        assert r.status_code >= 400
        assert r.content_type.startswith("application/json"), r.content_type


def test_long_text_is_capped_not_crashing(client):
    r = client.post("/api/items", json={"text": "a" * 5000})
    assert r.status_code == 201
    assert len(r.get_json()["item"]["text"]) <= 500
