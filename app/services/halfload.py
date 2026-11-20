"""半截补交：抽取标记与落库行必须同源，补交时钉死写库命运。

选定路线（归档 + 硬拦）：
1. 补交先把目标表里的半截残行整行留存进只读归档 `landing_archive`；
2. 再把这些残行清出目标表，落点不再被半截残行占住；
3. 重新抽取，只有能凑齐应有行数才重灌；凑不齐就硬拦，不往目标表写半截数据。

`landing_rows` 上的唯一索引 (target_table, slice_key, row_key) 作为写入瞬间的
兜底闸：若半截残行仍占着落点，重灌会直接撞索引，被拦下并回一句白话。

选边说明：选「归档后清出」这一边，半截残行不再占落点；若选「唯一索引拦下、
残行留在原地」那一边，残行会继续占着落点。两条路线不能混着走。
"""

import sqlite3

from app.db import get_db, get_db_immediate
from app.services.errors import ServiceError

SAMPLE_KEY = "H1"
TARGET_TABLE = "dw_orders_d"
EXPECTED_ROWS = 5
HALF_EXTRACTED = 2
ATTEMPT_SEED = "A1"
RESUBMIT_KEY = "RESUBMIT-EXTRACT-LOAD"


def _row_keys(n: int) -> list[str]:
    return [f"r{i}" for i in range(1, n + 1)]


def _put(conn, target: str, key: str, row_key: str, attempt: str, status: str) -> None:
    conn.execute(
        "INSERT INTO landing_rows(target_table, slice_key, row_key, payload, status, attempt) "
        "VALUES (?,?,?,?,?,?)",
        (target, key, row_key, f"{key}:{row_key}", status, attempt),
    )


def _next_attempt(conn, key: str) -> str:
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM extract_marks WHERE slice_key=?", (key,)
    ).fetchone()["c"]
    return f"A{n + 1}"


def _latest_attempt(conn, key: str) -> str:
    row = conn.execute(
        "SELECT attempt FROM extract_marks WHERE slice_key=? ORDER BY id DESC LIMIT 1",
        (key,),
    ).fetchone()
    return row["attempt"] if row else ""


def _verdict(marks: list, rows: list) -> tuple[bool, str]:
    """抽取标记与落库行的同源核对。双标（两标互相打架）即停。"""
    attempts = [m["attempt"] for m in marks]
    dup = [a for a in set(attempts) if attempts.count(a) > 1]
    if dup:
        return False, f"同一切片同一批次出现重复抽取标记（{dup[0]}），双标即停，未改动任何落库行"
    if not marks:
        return False, "缺抽取标记，无法与落库行同源核对，双标即停，未改动任何落库行"
    latest = marks[-1]
    half = [r for r in rows if r["status"] == "半截"]
    landed = [r for r in rows if r["status"] == "已落库"]
    if latest["extract_ok"] == 0 and latest["load_ok"] == 1:
        return False, "抽取未成却标记已落库，双标即停，未改动任何落库行"
    if latest["load_ok"] == 1 and half:
        return False, (
            f"落库标记齐全但目标表仍有 {len(half)} 行半截残行，双标即停，未改动任何落库行"
        )
    if latest["load_ok"] == 1 and not landed:
        return False, "落库标记齐全但目标表没有落库行，双标即停，未改动任何落库行"
    return True, ""


def seed_half(
    extracted_rows: int | None = None, db_path: str | None = None
) -> dict:
    """挂一个半截样本：抽取标记写“齐全”、落库标记写“半截”，两标不同源，覆盖假绿。

    `extracted_rows` 可指定本次抽取实际行数（默认 2）。小于应有行数即为半截，
    补交时会被硬拦；只有抽齐了才允许重灌。
    """
    extracted = HALF_EXTRACTED if extracted_rows is None else int(extracted_rows)
    if extracted < 0:
        raise ServiceError("extracted_rows 不能为负")
    if extracted > EXPECTED_ROWS:
        extracted = EXPECTED_ROWS
    with get_db(db_path) as conn:
        conn.execute("DELETE FROM extract_marks")
        conn.execute("DELETE FROM load_samples")
        conn.execute("DELETE FROM landing_rows")
        conn.execute("DELETE FROM resubmit_ledger")
        conn.execute(
            "INSERT INTO load_samples(slice_key, target_table, expected_rows, extracted_rows, state, reason) "
            "VALUES (?,?,?,?, '半截', ?)",
            (
                SAMPLE_KEY,
                TARGET_TABLE,
                EXPECTED_ROWS,
                extracted,
                f"抽取标记写齐全，落库只到 {extracted}/{EXPECTED_ROWS} 行，"
                f"缺口 {EXPECTED_ROWS - extracted} 行",
            ),
        )
        for row_key in _row_keys(extracted):
            _put(conn, TARGET_TABLE, SAMPLE_KEY, row_key, ATTEMPT_SEED, "半截")
        conn.execute(
            "INSERT INTO extract_marks(slice_key, extract_ok, load_ok, attempt) VALUES (?,?,?,?)",
            (SAMPLE_KEY, 1, 0, ATTEMPT_SEED),
        )
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('extract_flag','齐全')")
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('load_flag','半截')")
        return {
            "ok": True,
            "chip": "假齐",
            "fake_green": True,
            "message": (
                f"已挂半截样本：抽取 {extracted}/{EXPECTED_ROWS} 行，"
                f"目标表残留 {extracted} 行半截"
            ),
        }


def resubmit(db_path: str | None = None) -> dict:
    """值班点补交：一次收拢所有同败样本，写库命运在事务内钉死。

    - 左右值班并发点补交：BEGIN IMMEDIATE 排队 + 台账唯一索引，不双写补交行；
    - 半截残行先进只读归档，再清出目标表；
    - 抽取不齐就硬拦，不写半截数据，抽取标记与落库行同成同败；
    - 失败回白话。
    """
    with get_db_immediate(db_path) as conn:
        samples = conn.execute(
            "SELECT * FROM load_samples WHERE state != '已收口' ORDER BY slice_key"
        ).fetchall()
        if not samples:
            return {
                "ok": True,
                "closed": True,
                "outcome": "无待补交",
                "chip": "",
                "message": "没有待补交的半截样本。",
                "results": [],
            }

        results = []
        for sample in samples:
            key = sample["slice_key"]

            prior = conn.execute(
                "SELECT * FROM resubmit_ledger WHERE slice_key=?", (key,)
            ).fetchone()
            current_attempt = _latest_attempt(conn, key)
            if prior and prior["attempt"] == current_attempt:
                # 并发/重复点击：写锁把第二路排在后面，它只读第一路的结果，
                # 不再写第二笔补交行。要重试须重新挂样本（会重置台账）。
                results.append(
                    {
                        "slice_key": key,
                        "outcome": prior["outcome"],
                        "message": prior["message"],
                        "duplicate": True,
                    }
                )
                continue

            marks = conn.execute(
                "SELECT * FROM extract_marks WHERE slice_key=? ORDER BY id", (key,)
            ).fetchall()
            rows = conn.execute(
                "SELECT * FROM landing_rows WHERE target_table=? AND slice_key=?",
                (sample["target_table"], key),
            ).fetchall()

            ok, why = _verdict(marks, rows)
            if not ok:
                conn.execute(
                    "INSERT INTO resubmit_ledger(slice_key, attempt, outcome, message) "
                    "VALUES (?,?, '双标即停', ?) "
                    "ON CONFLICT(slice_key) DO UPDATE SET "
                    "attempt=excluded.attempt, outcome=excluded.outcome, "
                    "message=excluded.message",
                    (key, current_attempt, why),
                )
                results.append({"slice_key": key, "outcome": "双标即停", "message": why})
                continue

            half = [r for r in rows if r["status"] == "半截"]
            # 1) 半截残行整行留存进只读归档（归档表有只读触发器）
            for r in half:
                conn.execute(
                    "INSERT OR IGNORE INTO landing_archive"
                    "(target_table, slice_key, row_key, payload, attempt) VALUES (?,?,?,?,?)",
                    (r["target_table"], r["slice_key"], r["row_key"], r["payload"], r["attempt"]),
                )
            # 2) 清出目标表：落点不再被半截残行占住
            conn.execute(
                "DELETE FROM landing_rows WHERE target_table=? AND slice_key=?",
                (sample["target_table"], key),
            )

            attempt = _next_attempt(conn, key)
            can_load = sample["extracted_rows"] >= sample["expected_rows"]

            if can_load:
                # 3a) 抽取齐了才重灌；唯一索引作为写入瞬间兜底
                conn.execute("SAVEPOINT reload")
                try:
                    for row_key in _row_keys(sample["expected_rows"]):
                        _put(conn, sample["target_table"], key, row_key, attempt, "已落库")
                    conn.execute("RELEASE reload")
                    outcome = "已收口"
                    extract_ok = 1
                    load_ok = 1
                    state = "已收口"
                    message = (
                        f"补交完成：{sample['expected_rows']} 行全部落库，"
                        f"半截残行 {len(half)} 行已归档清出目标表。"
                    )
                except sqlite3.IntegrityError:
                    conn.execute("ROLLBACK TO reload")
                    conn.execute("RELEASE reload")
                    outcome = "同败"
                    extract_ok = 0
                    load_ok = 0
                    state = "半截"
                    message = "补交未落库：唯一索引拦下目标表落点冲突，本次未写入任何行。"
            else:
                # 3b) 抽取不齐：硬拦，目标表一行都不写
                gap = sample["expected_rows"] - sample["extracted_rows"]
                outcome = "同败"
                extract_ok = 0
                load_ok = 0
                state = "半截"
                message = (
                    f"补交未通过：只抽到 {sample['extracted_rows']}/"
                    f"{sample['expected_rows']} 行，缺口 {gap} 行；"
                    f"已把 {len(half)} 行半截残行归档并清出目标表，"
                    f"没往目标表写半截数据。"
                )

            # 4) 抽取标记与落库行同一事务落笔：同成同败
            conn.execute(
                "INSERT INTO extract_marks(slice_key, extract_ok, load_ok, attempt) "
                "VALUES (?,?,?,?)",
                (key, extract_ok, load_ok, attempt),
            )
            conn.execute(
                "UPDATE load_samples SET state=?, reason=? WHERE slice_key=?",
                (state, message, key),
            )
            conn.execute(
                "INSERT INTO resubmit_ledger(slice_key, attempt, outcome, message) "
                "VALUES (?,?,?,?) ON CONFLICT(slice_key) DO UPDATE SET "
                "attempt=excluded.attempt, outcome=excluded.outcome, "
                "message=excluded.message",
                (key, attempt, outcome, message),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES ('extract_flag',?)",
                ("齐全" if extract_ok else "缺口",),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES ('load_flag',?)",
                ("已落库" if load_ok else "半截",),
            )
            results.append(
                {
                    "slice_key": key,
                    "outcome": outcome,
                    "message": message,
                    "attempt": attempt,
                    "extract_ok": extract_ok,
                    "load_ok": load_ok,
                }
            )

        first = results[0]
        closed = first["outcome"] == "已收口"
        return {
            "ok": closed,
            "closed": closed,
            "outcome": first["outcome"],
            "chip": "" if closed else "假齐",
            "message": first["message"],
            "results": results,
        }


def panels(db_path: str | None = None) -> dict:
    """四处面板同一数据源：假齐清单 / 半截详情 / 同败卡片 / 回灌附页。"""
    with get_db(db_path) as conn:
        marks = [dict(r) for r in conn.execute("SELECT * FROM extract_marks ORDER BY id").fetchall()]
        samples = [
            dict(r) for r in conn.execute("SELECT * FROM load_samples ORDER BY slice_key").fetchall()
        ]
        rows = [dict(r) for r in conn.execute("SELECT * FROM landing_rows").fetchall()]
        archived = conn.execute("SELECT COUNT(*) AS c FROM landing_archive").fetchone()["c"]
        ledgers = {
            r["slice_key"]: dict(r)
            for r in conn.execute("SELECT * FROM resubmit_ledger").fetchall()
        }

        latest: dict[str, dict] = {}
        for m in marks:
            latest[m["slice_key"]] = m

        def _fake_green_for(key: str) -> bool:
            """真正的假绿：抽取标记齐全、落库标记半截，两标不同源。"""
            lm = latest.get(key)
            return bool(lm and lm["extract_ok"] == 1 and lm["load_ok"] == 0)

        list_rows = []
        for s in samples:
            key = s["slice_key"]
            lm = latest.get(key)
            # 假齐芯片是“案件标记”，补交后也保持不翻：
            # 收口与否另由 status 表达，页上才能分出假齐因与已对账。
            closed = s["state"] == "已收口"
            status = "已收口" if closed else ("同败" if key in ledgers else "待补交")
            list_rows.append(
                {
                    "slice_key": key,
                    "attempt": lm["attempt"] if lm else "",
                    "extract_ok": lm["extract_ok"] if lm else 0,
                    "load_ok": lm["load_ok"] if lm else 0,
                    "state": s["state"],
                    "chip": "假齐",
                    "status": status,
                    # 假齐因：当下是否仍满足“抽取齐全 + 落库半截”
                    "fake_green": _fake_green_for(key),
                    "reason": s["reason"],
                }
            )

        fake_green = any(_fake_green_for(s["slice_key"]) for s in samples)
        half_rows = sum(1 for r in rows if r["status"] == "半截")
        unclosed = sum(1 for s in samples if s["state"] != "已收口")

        detail = {
            "slice_key": "",
            "extract": "",
            "load": "",
            "extract_ok": 0,
            "load_ok": 0,
            "attempt": "",
            "expected_rows": 0,
            "extracted_rows": 0,
            "loaded_rows": 0,
            "half_rows_in_target": half_rows,
            "archived_rows": archived,
            "same_source": True,
            "reason": "",
        }
        if samples:
            s = samples[0]
            key = s["slice_key"]
            lm = latest.get(key)
            landed = [
                r
                for r in rows
                if r["target_table"] == s["target_table"]
                and r["slice_key"] == key
                and r["status"] == "已落库"
            ]
            half = [
                r
                for r in rows
                if r["target_table"] == s["target_table"]
                and r["slice_key"] == key
                and r["status"] == "半截"
            ]
            # 同源：标记必须与目标表实况一致，标记自身也必须同成同败
            mark_extract = 1 if s["extracted_rows"] >= s["expected_rows"] else 0
            mark_load = 1 if len(landed) >= s["expected_rows"] else 0
            grounded = (
                lm is not None
                and lm["extract_ok"] == mark_extract
                and lm["load_ok"] == mark_load
            )
            same_source = bool(
                grounded
                and lm["extract_ok"] == lm["load_ok"]
                and all(r["attempt"] == lm["attempt"] for r in landed)
            )
            detail = {
                "slice_key": key,
                "extract": ("齐全" if lm["extract_ok"] else "缺口") if lm else "缺标记",
                "load": ("已落库" if lm["load_ok"] else "半截") if lm else "缺标记",
                "extract_ok": lm["extract_ok"] if lm else 0,
                "load_ok": lm["load_ok"] if lm else 0,
                "attempt": lm["attempt"] if lm else "",
                "expected_rows": s["expected_rows"],
                "extracted_rows": s["extracted_rows"],
                "loaded_rows": len(landed),
                "half_rows_in_target": len(half),
                "archived_rows": archived,
                "same_source": same_source,
                "reason": s["reason"],
            }

        led = ledgers.get(samples[0]["slice_key"]) if samples else None
        closed_ledger = bool(led and led["outcome"] == "已收口")
        card = {
            "resubmit_key": RESUBMIT_KEY,
            "fate": "同败已收口" if closed_ledger else "同败未收口",
            "outcome": led["outcome"] if led else "",
            "message": led["message"] if led else "补交键可用，等值班点收录",
            "pending": unclosed,
        }

        attach = {
            # 附页的假绿只认“抽取齐全 + 落库半截”这一真实条件：
            # 补交后两标同败、残行清出，附页就不该再亮假绿；
            # 但清单上的假齐芯片仍亮（案件未收口）。
            "fake_green": fake_green,
            "half_rows": half_rows,
            "archived_rows": archived,
            "unclosed": unclosed,
            "coverage": "假绿" if fake_green else ("同败未收口" if unclosed else "已对账"),
            "reason": (
                samples[0]["reason"]
                if samples and fake_green
                else (
                    "抽取与落库同成同败，假绿已戳破，但案件未收口"
                    if unclosed
                    else ("无样本" if not samples else "抽取与落库同成同败")
                )
            ),
        }

        return {"list": list_rows, "detail": detail, "card": card, "attach": attach}
