from app.db import get_db


def seed_half(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        conn.execute("DELETE FROM extract_marks")
        conn.execute(
            "INSERT INTO extract_marks(slice_key, extract_ok, load_ok) VALUES ('H1',1,0)"
        )
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('extract_flag','齐全')")
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('load_flag','半截')")
        return {"ok": True, "coverage": "假绿"}


def resubmit(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        # silent: only re-run extract mark; half load row stays load_ok=0 but report green
        conn.execute("UPDATE extract_marks SET extract_ok=1")
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('extract_flag','齐全')")
        return {"ok": True, "chip": "假齐", "note": "只重跑抽取", "green": True}


def panels(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM extract_marks ORDER BY id").fetchall()]
        ex = conn.execute("SELECT value FROM meta WHERE key='extract_flag'").fetchone()
        ld = conn.execute("SELECT value FROM meta WHERE key='load_flag'").fetchone()
        return {
            "list": rows,
            "detail": {"extract": ex["value"] if ex else "", "load": ld["value"] if ld else ""},
            "card": {"resubmit_key": "EXTRACT-ONLY", "fate": "同败未收口"},
            "attach": {
                "fake_green": any(r["extract_ok"] == 1 and r["load_ok"] == 0 for r in rows),
                "half_rows": sum(1 for r in rows if r["load_ok"] == 0),
            },
        }
