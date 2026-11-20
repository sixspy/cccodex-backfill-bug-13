"""半截补交验收：假齐清单 / 半截详情 / 同败卡片 / 回灌附页。

钉死题面要求：
- 不许只重跑抽取：补交必须清出半截残行（归档后清出目标表）并重灌，
  不得留下半截行占落点；
- 抽取标记与落库行必须同源、同成同败，双标即停；
- 并发点补交不得双写补交行；
- 补交后附页不得假绿，且库内可核。
"""

import os
import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient

from app.db import get_db, get_db_immediate, init_db
from app.main import create_app


def _db_path() -> str:
    return os.environ["DATABASE_PATH"]


async def _fresh_client(tmp_path) -> AsyncClient:
    db = tmp_path / "halfload.db"
    os.environ["DATABASE_PATH"] = str(db)
    init_db(str(db))
    app = create_app()
    transport = ASGITransport(app=app)
    ac = AsyncClient(transport=transport, base_url="http://127.0.0.1")
    await ac.post("/api/login", json={"username": "admin", "password": "123456"})
    return ac


async def _panels(ac: AsyncClient) -> dict:
    r = await ac.get("/api/halfload/panels")
    assert r.status_code == 200
    return r.json()


def _count(sql: str, params: tuple = ()) -> int:
    with get_db(_db_path()) as conn:
        return conn.execute(sql, params).fetchone()[0]


@pytest.mark.asyncio
async def test_seed_shows_fake_green_everywhere(tmp_path):
    """挂半截后：清单亮假齐芯片、详情半截、卡片同败未收口、附页假绿。"""
    ac = await _fresh_client(tmp_path)
    try:
        seed = (await ac.post("/api/halfload/seed", json={})).json()
        assert seed["chip"] == "假齐"

        p = await _panels(ac)
        assert p["list"][0]["chip"] == "假齐"
        assert p["list"][0]["reason"], "假齐清单上要能看出假齐因"
        assert p["detail"]["half_rows_in_target"] == 2
        assert p["detail"]["archived_rows"] == 0
        assert p["detail"]["same_source"] is False
        assert p["card"]["fate"] == "同败未收口"
        assert p["card"]["resubmit_key"]
        assert p["attach"]["fake_green"] is True
        assert p["attach"]["coverage"] == "假绿"
    finally:
        await ac.aclose()


@pytest.mark.asyncio
async def test_resubmit_does_not_only_rerun_extract(tmp_path):
    """抽取仍未齐：补交必须硬拦、不写半截行，但半截残行要清出目标表并归档。"""
    ac = await _fresh_client(tmp_path)
    try:
        await ac.post("/api/halfload/seed", json={})
        out = (await ac.post("/api/halfload/resubmit", json={})).json()

        assert out["closed"] is False, "抽取不齐不能回绿"
        assert "补交未通过" in out["message"], "失败要回白话"
        assert out["results"][0]["extract_ok"] == 0
        assert out["results"][0]["load_ok"] == 0
        # 抽取标记与落库行同成同败
        assert out["results"][0]["extract_ok"] == out["results"][0]["load_ok"]

        # 半截残行不得继续占落点
        assert _count("SELECT COUNT(*) FROM landing_rows WHERE status='半截'") == 0
        assert _count("SELECT COUNT(*) FROM landing_rows") == 0
        # 但半截行已进只读归档，库内可核
        assert _count("SELECT COUNT(*) FROM landing_archive") == 2

        p = await _panels(ac)
        assert p["detail"]["half_rows_in_target"] == 0
        assert p["detail"]["archived_rows"] == 2
        assert p["detail"]["extract_ok"] == 0 and p["detail"]["load_ok"] == 0
        assert p["detail"]["same_source"] is True
        # 补交后两标同败、残行清出：附页不再假绿
        assert p["attach"]["fake_green"] is False
        assert p["attach"]["coverage"] == "同败未收口"
        assert p["attach"]["half_rows"] == 0
        # 但假齐芯片保持：案件还没收口，不能翻成“已对账”
        assert p["list"][0]["chip"] == "假齐"
        assert p["list"][0]["fake_green"] is False
        assert p["card"]["fate"] == "同败未收口"
    finally:
        await ac.aclose()


@pytest.mark.asyncio
async def test_resubmit_with_full_extract_closes_and_clears_half(tmp_path):
    """抽齐后补交：全部落库、半截清出目标表、标记与落库行同成同败。"""
    ac = await _fresh_client(tmp_path)
    try:
        await ac.post("/api/halfload/seed", json={"extracted_rows": 5})
        out = (await ac.post("/api/halfload/resubmit", json={})).json()

        assert out["closed"] is True
        assert out["results"][0]["extract_ok"] == 1
        assert out["results"][0]["load_ok"] == 1

        assert _count("SELECT COUNT(*) FROM landing_rows WHERE status='半截'") == 0
        assert _count("SELECT COUNT(*) FROM landing_rows WHERE status='已落库'") == 5
        assert _count("SELECT COUNT(*) FROM landing_archive") == 5

        p = await _panels(ac)
        assert p["detail"]["loaded_rows"] == 5
        assert p["detail"]["half_rows_in_target"] == 0
        assert p["detail"]["same_source"] is True
        # 假齐芯片保持：收口也不翻芯片，收口另由 status 表达
        assert p["list"][0]["chip"] == "假齐"
        assert p["list"][0]["status"] == "已收口"
        assert p["list"][0]["fake_green"] is False
        assert p["attach"]["fake_green"] is False
        assert p["attach"]["coverage"] == "已对账"
        assert p["card"]["fate"] == "同败已收口"
    finally:
        await ac.aclose()


@pytest.mark.asyncio
async def test_resubmit_archive_is_readonly(tmp_path):
    """归档只读：补交后不许改/删归档里的半截行。"""
    ac = await _fresh_client(tmp_path)
    try:
        await ac.post("/api/halfload/seed", json={})
        await ac.post("/api/halfload/resubmit", json={})
        assert _count("SELECT COUNT(*) FROM landing_archive") == 2

        with pytest.raises(sqlite3.DatabaseError):
            with get_db(_db_path()) as conn:
                conn.execute("UPDATE landing_archive SET payload='x'")
        with pytest.raises(sqlite3.DatabaseError):
            with get_db(_db_path()) as conn:
                conn.execute("DELETE FROM landing_archive")
        assert _count("SELECT COUNT(*) FROM landing_archive") == 2
    finally:
        await ac.aclose()


@pytest.mark.asyncio
async def test_resubmit_is_idempotent_after_close(tmp_path):
    """补交后再点一次：不再写第二笔补交行，也不改动已收口结果。"""
    ac = await _fresh_client(tmp_path)
    try:
        await ac.post("/api/halfload/seed", json={"extracted_rows": 5})
        await ac.post("/api/halfload/resubmit", json={})
        assert _count("SELECT COUNT(*) FROM resubmit_ledger") == 1

        again = (await ac.post("/api/halfload/resubmit", json={})).json()
        assert again["results"] == [] or all(
            r.get("duplicate") for r in again["results"]
        )
        assert _count("SELECT COUNT(*) FROM resubmit_ledger") == 1
        assert _count("SELECT COUNT(*) FROM landing_rows") == 5
        assert _count("SELECT COUNT(*) FROM extract_marks") == 2
    finally:
        await ac.aclose()


def test_concurrent_resubmit_does_not_double_write(tmp_path):
    """左右值班并发点补交：写锁 + 台账唯一索引，只落一笔补交行。"""
    import threading

    db = tmp_path / "concurrent.db"
    os.environ["DATABASE_PATH"] = str(db)
    init_db(str(db))

    from app.services.halfload import resubmit, seed_half

    seed_half(db_path=str(db))

    errors: list[Exception] = []
    results: list[dict] = []
    barrier = threading.Barrier(2)

    def worker():
        try:
            barrier.wait(timeout=5)
            results.append(resubmit(db_path=str(db)))
        except Exception as exc:  # pragma: no cover - surfaced by assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, errors
    assert len(results) == 2
    with get_db(str(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM resubmit_ledger").fetchone()[0] == 1
        # 半截行只归档一次，不重复追加
        assert conn.execute("SELECT COUNT(*) FROM landing_archive").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM landing_rows").fetchone()[0] == 0


def test_double_mark_stops_without_touching_target(tmp_path):
    """抽取标记与落库行互相打架（双标）时即停，不改动目标表。"""
    db = tmp_path / "doublemark.db"
    os.environ["DATABASE_PATH"] = str(db)
    init_db(str(db))

    from app.services.halfload import resubmit, seed_half

    seed_half(db_path=str(db))
    # 人为造双标：同一批次出现两条抽取标记
    with get_db(str(db)) as conn:
        conn.execute(
            "INSERT INTO extract_marks(slice_key, extract_ok, load_ok, attempt) "
            "VALUES ('H1',1,0,'A1')"
        )

    out = resubmit(db_path=str(db))
    assert out["results"][0]["outcome"] == "双标即停"
    assert "补交" not in out["results"][0]["message"][:2]
    with get_db(str(db)) as conn:
        # 半截残行原地未动，未归档、未清出
        assert conn.execute("SELECT COUNT(*) FROM landing_rows").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM landing_archive").fetchone()[0] == 0


def test_get_db_immediate_rolls_back_on_error(tmp_path):
    """写事务异常时整笔回滚，不留半截写。"""
    db = tmp_path / "tx.db"
    os.environ["DATABASE_PATH"] = str(db)
    init_db(str(db))

    with pytest.raises(RuntimeError):
        with get_db_immediate(str(db)) as conn:
            conn.execute("INSERT INTO meta(key,value) VALUES ('tmp','1')")
            raise RuntimeError("boom")

    with get_db(str(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM meta WHERE key='tmp'").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_panels_stay_consistent_after_resubmit_rerun(tmp_path):
    """补交后反复刷新附页与切片再跑，不得回假绿，库内可核。"""
    ac = await _fresh_client(tmp_path)
    try:
        await ac.post("/api/halfload/seed", json={"extracted_rows": 5})
        first = (await ac.post("/api/halfload/resubmit", json={})).json()
        assert first["closed"] is True

        # 附页/详情/明显反复读取，不改库
        for _ in range(3):
            p = await _panels(ac)
            assert p["attach"]["fake_green"] is False
            assert p["attach"]["half_rows"] == 0
            assert p["detail"]["same_source"] is True
            assert p["detail"]["loaded_rows"] == 5

        # 切片与覆盖对账再跑一遍
        windows = (await ac.get("/api/windows")).json()["items"]
        wid = windows[0]["id"]
        planned = (await ac.post(f"/api/windows/{wid}/plan")).json()
        for sl in planned["slices"]:
            r = await ac.post(f"/api/slices/{sl['id']}/replay")
            assert r.status_code == 200
        report = (await ac.post("/api/coverage-reports", json={"window_id": wid})).json()["report"]
        assert report["missing"] == 0

        # 再点补交：无待补交，不得凭空造绿也不得改已收口
        again = (await ac.post("/api/halfload/resubmit", json={})).json()
        assert again["outcome"] == "无待补交"
        assert _count("SELECT COUNT(*) FROM landing_rows WHERE status='半截'") == 0
        assert _count("SELECT COUNT(*) FROM resubmit_ledger") == 1
    finally:
        await ac.aclose()


def test_unique_index_blocks_half_row_occupying_landing(tmp_path):
    """唯一索引在写入瞬间拦半截：落点被半截残行占住时，重灌会撞索引。"""
    db = tmp_path / "uniq.db"
    os.environ["DATABASE_PATH"] = str(db)
    init_db(str(db))

    with get_db(str(db)) as conn:
        conn.execute(
            "INSERT INTO landing_rows(target_table, slice_key, row_key, payload, status, attempt) "
            "VALUES ('dw_orders_d','H1','r1','H1:r1','半截','A1')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        with get_db(str(db)) as conn:
            conn.execute(
                "INSERT INTO landing_rows(target_table, slice_key, row_key, payload, status, attempt) "
                "VALUES ('dw_orders_d','H1','r1','H1:r1','已落库','A2')"
            )
    # 清出后才能重灌
    with get_db(str(db)) as conn:
        conn.execute("DELETE FROM landing_rows WHERE slice_key='H1' AND row_key='r1'")
        conn.execute(
            "INSERT INTO landing_rows(target_table, slice_key, row_key, payload, status, attempt) "
            "VALUES ('dw_orders_d','H1','r1','H1:r1','已落库','A2')"
        )
        assert conn.execute("SELECT status FROM landing_rows WHERE row_key='r1'").fetchone()[0] == "已落库"
