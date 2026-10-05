"""永久撤回 API 测试。

覆盖：
- 首次/重复撤回（幂等、不改写原因）、空原因拒绝
- 未知文档/版本按既有错误返回；撤回查询（未撤回 -> withdrawal_not_found）
- 撤回后公开元数据与图片读取 410、不暴露成品与遮盖详情
- 撤回不删发布记录/成品文件、不复用版本号；后续单份/批次发布正常
- 文档详情、发布列表、批次清单保留原字段及顺序并标识撤回状态
- 批次幂等键重放仍返回首次响应，但不恢复撤回版本的公开访问
- 生效前已开始的下载可完成；并发撤回与发布状态一致、无部分撤回记录
"""
import os
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.test_api import png_bytes, upload  # noqa: E402,F402  复用构造器与助手


@pytest.fixture()
def client(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"))
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _annotate(client, doc_id, revision, rects=None):
    rects = rects if rects is not None else [
        {"x": 1, "y": 1, "w": 5, "h": 5, "reason": "敏感"}
    ]
    return client.put(
        f"/documents/{doc_id}/annotations",
        json={"revision": revision, "rects": rects},
    )


def _publish(client, doc_id):
    return client.post(f"/documents/{doc_id}/publish")


def _withdraw(client, doc_id, version, reason="遮盖遗漏，撤回复核"):
    return client.post(
        f"/documents/{doc_id}/publications/{version}/withdrawal",
        json={"reason": reason},
    )


def _db(client):
    settings = client.app.state.settings
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return settings, conn


# ---------------- 首次撤回 / 重复撤回 / 空原因 ----------------

def test_withdraw_first_time_persists_reason_and_time(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    _publish(client, doc_id)
    r = _withdraw(client, doc_id, 1, reason="发现遮盖遗漏")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body == {
        "doc_id": doc_id,
        "version": 1,
        "reason": "发现遮盖遗漏",
        "withdrawn_at": body["withdrawn_at"],
    }
    assert body["withdrawn_at"]  # 非空 ISO 时间

    # 查询撤回时间与原因
    g = client.get(f"/documents/{doc_id}/publications/1/withdrawal")
    assert g.status_code == 200
    assert g.json() == body


def test_repeated_withdrawal_returns_first_record_without_overwriting(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    _publish(client, doc_id)
    first = _withdraw(client, doc_id, 1, reason="首次撤回原因").json()

    # 不同原因重复撤回 -> 200 + 首次记录，原因不被改写
    r = _withdraw(client, doc_id, 1, reason="另一个原因")
    assert r.status_code == 200
    assert r.json() == first

    # 空/纯空白原因重复撤回同样返回首次记录（不走 400 拒绝）
    r = client.post(
        f"/documents/{doc_id}/publications/1/withdrawal", json={"reason": "   "}
    )
    assert r.status_code == 200
    assert r.json() == first

    # 查询确认原因从未被改写，撤回记录只有一条
    g = client.get(f"/documents/{doc_id}/publications/1/withdrawal").json()
    assert g["reason"] == "首次撤回原因"
    assert g["withdrawn_at"] == first["withdrawn_at"]
    _, db = _db(client)
    n = db.execute(
        "SELECT COUNT(*) c FROM publication_withdrawals"
        " WHERE doc_id=? AND version=1",
        (doc_id,),
    ).fetchone()["c"]
    assert n == 1
    db.close()


def test_empty_reason_rejected_for_new_withdrawal(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    _publish(client, doc_id)
    for bad in ("", "   ", "\t\n "):
        r = client.post(
            f"/documents/{doc_id}/publications/1/withdrawal",
            json={"reason": bad},
        )
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "reason_required"
    # 被拒绝后版本仍公开，且没有留下撤回记录
    assert client.get(f"/publications/{doc_id}/1").status_code == 200
    assert client.get(
        f"/documents/{doc_id}/publications/1/withdrawal"
    ).status_code == 404


def test_withdraw_unknown_document_or_version_uses_existing_errors(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    _publish(client, doc_id)

    # 未知文档
    r = _withdraw(client, "nope", 1)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "document_not_found"
    r = client.get("/documents/nope/publications/1/withdrawal")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "document_not_found"

    # 未知版本（文档存在）
    r = _withdraw(client, doc_id, 99)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "unknown_version"
    r = client.get(f"/documents/{doc_id}/publications/99/withdrawal")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "unknown_version"


def test_withdrawal_lookup_on_non_withdrawn_version_404(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    _publish(client, doc_id)
    r = client.get(f"/documents/{doc_id}/publications/1/withdrawal")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "withdrawal_not_found"


# ---------------- 撤回对公开读取的影响 ----------------

def test_public_metadata_and_image_gone_after_withdrawal(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    _annotate(client, doc_id, 0,
              rects=[{"x": 2, "y": 2, "w": 6, "h": 6, "reason": "证件号"}])
    p = _publish(client, doc_id).json()

    # 撤回前公开可读
    assert client.get(f"/publications/{doc_id}/1").status_code == 200
    assert client.get(f"/publications/{doc_id}/1/image").status_code == 200

    _withdraw(client, doc_id, 1)

    # 元数据：410，不返回成品摘要与遮盖详情
    r = client.get(f"/publications/{doc_id}/1")
    assert r.status_code == 410
    detail = r.json()["detail"]
    assert detail["code"] == "version_withdrawn"
    assert detail["withdrawn_at"]
    assert "redactions" not in detail and "output_sha256" not in detail

    # 图片：410，不返回成品
    r = client.get(f"/publications/{doc_id}/1/image")
    assert r.status_code == 410
    assert r.json()["detail"]["code"] == "version_withdrawn"
    assert r.headers["content-type"] != "image/png"

    # 撤回前的版本摘要不再能经公开面获得（记录仍在、文件仍在）
    _, db = _db(client)
    row = db.execute(
        "SELECT output_path, output_sha256 FROM publications"
        " WHERE doc_id=? AND version=1",
        (doc_id,),
    ).fetchone()
    assert row["output_sha256"] == p["output_sha256"]
    assert os.path.exists(row["output_path"])
    db.close()


def test_download_started_before_withdrawal_can_complete(client, monkeypatch):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    _publish(client, doc_id)
    settings = client.app.state.settings

    # 模拟"公开读闸门已通过、文件尚未发完"的进行中下载：
    # 在 FileResponse 读盘前插入撤回。撤回不删文件，进行中的响应正常完成。
    import app.main as main_mod
    real_file_response = main_mod.FileResponse

    started = threading.Event()
    release = threading.Event()

    def gated_file_response(path, *args, **kwargs):
        started.set()
        release.wait(timeout=5)
        return real_file_response(path, *args, **kwargs)

    monkeypatch.setattr(main_mod, "FileResponse", gated_file_response)

    result = {}

    def downloader():
        result["r"] = client.get(f"/publications/{doc_id}/1/image")

    t = threading.Thread(target=downloader)
    t.start()
    assert started.wait(timeout=5)
    # 下载已过闸门：此时撤回生效
    w = _withdraw(client, doc_id, 1)
    assert w.status_code == 201
    release.set()
    t.join()
    r = result["r"]
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"

    # 撤回后新发起的下载被拒
    assert client.get(f"/publications/{doc_id}/1/image").status_code == 410


# ---------------- 撤回不删记录/文件、不复用版本号；后续发布正常 ----------------

def test_withdrawal_keeps_record_and_file_and_version_number(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    p1 = _publish(client, doc_id).json()
    assert p1["version"] == 1
    _, db = _db(client)
    path = db.execute(
        "SELECT output_path FROM publications WHERE doc_id=? AND version=1",
        (doc_id,),
    ).fetchone()["output_path"]
    db.close()

    assert _withdraw(client, doc_id, 1).status_code == 201

    # 发布记录仍在；版本号不复用：后续发布拿到 v2
    p2 = _publish(client, doc_id)
    assert p2.status_code == 201
    assert p2.json()["version"] == 2
    _, db = _db(client)
    rows = db.execute(
        "SELECT version FROM publications WHERE doc_id=? ORDER BY version", (doc_id,)
    ).fetchall()
    assert [r["version"] for r in rows] == [1, 2]
    ws = db.execute(
        "SELECT version FROM publication_withdrawals WHERE doc_id=?", (doc_id,)
    ).fetchall()
    assert [r["version"] for r in ws] == [1]
    db.close()
    assert os.path.exists(path)  # 成品文件未删除

    # 新版本公开读取按原规则工作
    assert client.get(f"/publications/{doc_id}/2").status_code == 200
    assert client.get(f"/publications/{doc_id}/2/image").status_code == 200


def test_non_withdrawn_versions_of_same_document_remain_public(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    _publish(client, doc_id)                      # v1
    _annotate(client, doc_id, 0, rects=[])
    _publish(client, doc_id)                      # v2
    _withdraw(client, doc_id, 1)

    assert client.get(f"/publications/{doc_id}/1").status_code == 410
    assert client.get(f"/publications/{doc_id}/1/image").status_code == 410
    assert client.get(f"/publications/{doc_id}/2").status_code == 200
    assert client.get(f"/publications/{doc_id}/2/image").status_code == 200


# ---------------- 列表/详情/批次清单：原字段顺序 + 撤回标识 ----------------

def test_document_detail_and_publication_list_mark_withdrawn(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    _annotate(client, doc_id, 0)
    _publish(client, doc_id)        # v1 rev1
    _publish(client, doc_id)        # v2 rev1
    _withdraw(client, doc_id, 1)

    detail = client.get(f"/documents/{doc_id}").json()
    pubs = detail["publications"]
    # 原字段及顺序保持不变（末尾仅新增 withdrawn/withdrawn_at）
    assert list(pubs[0].keys()) == [
        "version", "annotation_revision", "output_sha256", "created_at",
        "withdrawn", "withdrawn_at",
    ]
    assert [p["version"] for p in pubs] == [1, 2]
    assert pubs[0]["withdrawn"] is True and pubs[0]["withdrawn_at"]
    assert pubs[1]["withdrawn"] is False and pubs[1]["withdrawn_at"] is None

    lst = client.get(f"/documents/{doc_id}/publications").json()["publications"]
    assert list(lst[0].keys()) == [
        "version", "annotation_revision", "original_sha256", "output_sha256",
        "created_at", "withdrawn", "withdrawn_at",
    ]
    assert [p["withdrawn"] for p in lst] == [True, False]


def test_batch_query_marks_withdrawn_with_original_field_order(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes(color=(10, 200, 10))).json()["doc_id"]
    r = client.post(
        "/publication-batches",
        json={"documents": [
            {"doc_id": a, "expected_revision": 0},
            {"doc_id": b, "expected_revision": 0},
        ]},
    )
    assert r.status_code == 201
    body = r.json()
    batch_id = body["batch_id"]

    # 撤回批次中 a 的 v1
    assert _withdraw(client, a, 1).status_code == 201

    got = client.get(f"/publication-batches/{batch_id}")
    assert got.status_code == 200
    docs = got.json()["documents"]
    assert list(docs[0].keys()) == [
        "position", "doc_id", "version", "annotation_revision",
        "original_sha256", "output_sha256",
        "withdrawn", "withdrawn_at",
    ]
    assert [d["doc_id"] for d in docs] == [a, b]  # 顺序不变
    assert docs[0]["withdrawn"] is True
    assert docs[1]["withdrawn"] is False


# ---------------- 撤回后的批次发布与幂等重放 ----------------

def test_new_batch_publish_after_withdrawal_follows_normal_rules(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes()).json()["doc_id"]
    _publish(client, a)                       # a v1
    _withdraw(client, a, 1)
    # 撤回后批次发布仍成功；a 续号 v2，b 从 v1 开始；撤回状态不波及新版本
    r = client.post(
        "/publication-batches",
        json={"documents": [
            {"doc_id": a, "expected_revision": 0},
            {"doc_id": b, "expected_revision": 0},
        ]},
    )
    assert r.status_code == 201, r.text
    assert [d["version"] for d in r.json()["documents"]] == [2, 1]
    assert client.get(f"/publications/{a}/2").status_code == 200
    assert client.get(f"/publications/{a}/1").status_code == 410


def test_idempotency_replay_does_not_restore_withdrawn_version(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes()).json()["doc_id"]
    req = {"idempotency_key": "k-wd",
           "documents": [
               {"doc_id": a, "expected_revision": 0},
               {"doc_id": b, "expected_revision": 0},
           ]}
    first = client.post("/publication-batches", json=req)
    assert first.status_code == 201
    first_body = first.json()

    # 批次产物 a v1 被撤回
    assert _withdraw(client, a, 1).status_code == 201
    assert client.get(f"/publications/{a}/1").status_code == 410

    # 幂等键重放：仍逐字节返回首次保存的响应……
    replay = client.post("/publication-batches", json=req)
    assert replay.status_code == 200
    assert replay.json() == first_body
    # ……但撤回版本的公开访问不会因此恢复，也没有产生新版本
    assert client.get(f"/publications/{a}/1").status_code == 410
    assert client.get(f"/publications/{a}/1/image").status_code == 410
    _, db = _db(client)
    n = db.execute(
        "SELECT COUNT(*) c FROM publications WHERE doc_id=?", (a,)
    ).fetchone()["c"]
    assert n == 1
    db.close()


# ---------------- 并发：撤回与发布保持状态一致 ----------------

def test_concurrent_withdrawals_and_publishes_are_consistent(client):
    doc_id = upload(client, png_bytes(size=(60, 60))).json()["doc_id"]
    errors = []

    def withdrawer(v):
        # 版本可能尚未发布：404 unknown_version 是合法竞争结果
        r = _withdraw(client, doc_id, v)
        if r.status_code not in (200, 201, 404):
            errors.append(f"withdraw: {r.status_code} {r.text}")

    def publisher():
        for _ in range(10):
            r = client.post(f"/documents/{doc_id}/publish")
            if r.status_code != 201:
                errors.append(f"publish: {r.status_code} {r.text}")

    # 预先发布几个版本供撤回者竞争
    for _ in range(3):
        _publish(client, doc_id)

    threads = [threading.Thread(target=publisher) for _ in range(2)]
    threads += [threading.Thread(target=withdrawer, args=(v,)) for v in range(1, 13)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    settings, db = _db(client)
    pubs = db.execute(
        "SELECT version FROM publications WHERE doc_id=? ORDER BY version",
        (doc_id,),
    ).fetchall()
    versions = [r["version"] for r in pubs]
    # 版本号连续不复用
    assert versions == list(range(1, len(versions) + 1))

    withdrawals = db.execute(
        "SELECT version, COUNT(*) c FROM publication_withdrawals"
        " WHERE doc_id=? GROUP BY version",
        (doc_id,),
    ).fetchall()
    withdrawn_versions = set()
    for w in withdrawals:
        assert w["c"] == 1  # 每个版本至多一条撤回记录，无部分/重复撤回
        withdrawn_versions.add(w["version"])

    # DB 标记的撤回版本 == 公开面实际 410 的版本；未撤回版本仍 200
    db.close()
    for v in versions:
        r = client.get(f"/publications/{doc_id}/{v}")
        if v in withdrawn_versions:
            assert r.status_code == 410, f"v{v} 应已撤回"
        else:
            assert r.status_code == 200, f"v{v} 不应被撤回"


def test_failed_withdrawal_leaves_no_partial_record(client, monkeypatch):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    _publish(client, doc_id)

    # 让撤回 INSERT 在记录写入中途失败：事务回滚，不留部分撤回记录，
    # 版本仍公开，调用方可原样重试。（sqlite3.Connection 为内置不可变类型，
    # 用连接代理在 execute 上注入故障；TestClient 默认会把服务端异常向上抛，
    # 这里改为按真实 HTTP 行为返回 500。）
    import app.db as db_mod

    real_connect = db_mod.connect
    calls = {"n": 0}

    class FailingConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *args, **kwargs):
            if "INSERT INTO publication_withdrawals" in sql:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise sqlite3.OperationalError("simulated mid-write failure")
            return self._inner.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def flaky_connect(db_path):
        return FailingConn(real_connect(db_path))

    monkeypatch.setattr(db_mod, "connect", flaky_connect)
    with TestClient(client.app, raise_server_exceptions=False) as raw:
        r = raw.post(
            f"/documents/{doc_id}/publications/1/withdrawal",
            json={"reason": "x"},
        )
        assert r.status_code == 500
    monkeypatch.undo()

    _, db = _db(client)
    n = db.execute(
        "SELECT COUNT(*) c FROM publication_withdrawals WHERE doc_id=?", (doc_id,)
    ).fetchone()["c"]
    assert n == 0
    pub = db.execute(
        "SELECT COUNT(*) c FROM publications WHERE doc_id=? AND version=1", (doc_id,)
    ).fetchone()["c"]
    assert pub == 1  # 发布记录未受影响
    db.close()

    assert client.get(f"/publications/{doc_id}/1").status_code == 200
    assert _withdraw(client, doc_id, 1).status_code == 201
    assert client.get(f"/publications/{doc_id}/1").status_code == 410
