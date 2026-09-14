"""捕获队列：存储原子写/去重、HTTP 端点契约。"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from crawler_tool.application import CrawlService
from crawler_tool.application.capture_queue import CaptureQueueStore
from crawler_tool.interfaces import create_app
from crawler_tool.sources import SourceRegistry


def test_store_add_dedups_casefold_and_persists(tmp_path):
    store = CaptureQueueStore(tmp_path / "capture_queue.json")
    first = store.add(["华为 Mate XT2", "  小米  "], source="agent")
    assert first == {"added": 2, "count": 2}
    second = store.add(["华为 mate xt2", "折叠屏"], source="manual")
    assert second == {"added": 1, "count": 3}

    entries = store.list_keywords()
    assert [entry["keyword"] for entry in entries] == ["华为 Mate XT2", "小米", "折叠屏"]
    assert entries[0]["source"] == "agent"
    assert all(entry["addedAt"] for entry in entries)

    # 文件已落盘，新实例可读（重启不丢）
    reloaded = CaptureQueueStore(tmp_path / "capture_queue.json")
    assert reloaded.list_keywords()[0]["keyword"] == "华为 Mate XT2"


def test_store_remove_is_casefold_exact(tmp_path):
    store = CaptureQueueStore(tmp_path / "capture_queue.json")
    store.add(["华为", "小米"])
    assert store.remove("华为") is True
    assert store.remove("华为") is False
    assert [entry["keyword"] for entry in store.list_keywords()] == ["小米"]


def test_store_survives_corrupted_file(tmp_path):
    path = tmp_path / "capture_queue.json"
    path.write_text("{broken", encoding="utf-8")
    store = CaptureQueueStore(path)
    assert store.list_keywords() == []
    store.add(["华为"])
    assert json.loads(path.read_text(encoding="utf-8"))[0]["keyword"] == "华为"


def _client(tmp_path):
    app = create_app(lambda: CrawlService(SourceRegistry({})),
                     capture_queue=CaptureQueueStore(tmp_path / "capture_queue.json"))
    return TestClient(app)


def test_capture_queue_endpoints_round_trip(tmp_path):
    client = _client(tmp_path)

    empty = client.get("/api/v1/tool/capture-queue")
    assert empty.status_code == 200
    assert empty.json() == {"count": 0, "keywords": []}

    pushed = client.post("/api/v1/tool/capture-queue",
                         json={"keywords": ["大模型 发布", "大模型 发布", "AI 客服"], "source": "agent"})
    assert pushed.status_code == 200
    assert pushed.json() == {"added": 2, "count": 2}

    listed = client.get("/api/v1/tool/capture-queue").json()
    assert listed["count"] == 2
    assert listed["keywords"][0]["source"] == "agent"

    removed = client.delete("/api/v1/tool/capture-queue", params={"keyword": "大模型 发布"})
    assert removed.status_code == 200
    assert removed.json() == {"removed": True}
    assert client.get("/api/v1/tool/capture-queue").json()["count"] == 1


def test_capture_queue_push_rejects_bad_payload(tmp_path):
    client = _client(tmp_path)
    assert client.post("/api/v1/tool/capture-queue", json={"keywords": []}).status_code == 422
    assert client.post("/api/v1/tool/capture-queue", json={"keywords": ["  "]}).status_code == 422
    assert client.post("/api/v1/tool/capture-queue",
                       json={"keywords": ["华为"], "source": "robot"}).status_code == 422
    assert client.post("/api/v1/tool/capture-queue",
                       json={"keywords": ["华为"], "extra": 1}).status_code == 422
