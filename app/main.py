from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
import sqlite3
from datetime import datetime, date
import os
import logging

app = FastAPI()

BASE_DIR = Path(__file__).resolve().parent.parent

# Use explicit env override, or /tmp when running on Vercel (serverless), otherwise keep default local path
if os.getenv("GYMLOG_DB"):
    DB_PATH = Path(os.getenv("GYMLOG_DB"))
elif os.getenv("VERCEL"):
    DB_PATH = Path("/tmp/gymlog.db")
else:
    DB_PATH = BASE_DIR / "data" / "gymlog.db"

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

# Used only to seed the DB the first time
WORKOUTS = {
    "Push": ["Bench Press", "Incline DB Press", "Overhead Press", "Tricep Pushdown"],
    "Pull": ["Lat Pulldown", "Row", "Face Pull", "Bicep Curl"],
    "Legs": ["Squat", "RDL", "Leg Press", "Leg Curl", "Calf Raise"],
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def db_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Allow use from serverless environments / threads
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db():
    with db_conn() as conn:
        # Templates
        conn.execute("""
        CREATE TABLE IF NOT EXISTS workout_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS exercises (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS template_exercises (
            template_id INTEGER NOT NULL,
            exercise_id INTEGER NOT NULL,
            order_index INTEGER NOT NULL,
            PRIMARY KEY (template_id, exercise_id),
            FOREIGN KEY (template_id) REFERENCES workout_templates(id) ON DELETE CASCADE,
            FOREIGN KEY (exercise_id) REFERENCES exercises(id) ON DELETE CASCADE
        )
        """)

        # Sessions
        conn.execute("""
        CREATE TABLE IF NOT EXISTS workout_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day TEXT NOT NULL,
            template_id INTEGER NOT NULL,
            workout_name TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            FOREIGN KEY (template_id) REFERENCES workout_templates(id)
        )
        """)

        # Logged sets
        conn.execute("""
        CREATE TABLE IF NOT EXISTS set_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day TEXT NOT NULL,
            workout TEXT NOT NULL,
            exercise TEXT NOT NULL,
            weight REAL NOT NULL,
            reps INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            session_id INTEGER
        )
        """)

        # If set_entries existed before we added session_id, add it safely
        try:
            conn.execute("ALTER TABLE set_entries ADD COLUMN session_id INTEGER")
        except sqlite3.OperationalError:
            pass  # already exists or table created with column

        conn.commit()


def seed_templates_if_empty():
    with db_conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM workout_templates").fetchone()["c"]
        if count > 0:
            return

        for template_name, ex_list in WORKOUTS.items():
            conn.execute("INSERT INTO workout_templates(name) VALUES (?)", (template_name,))
            template_id = conn.execute(
                "SELECT id FROM workout_templates WHERE name=?",
                (template_name,)
            ).fetchone()["id"]

            for idx, ex_name in enumerate(ex_list):
                conn.execute("INSERT OR IGNORE INTO exercises(name) VALUES (?)", (ex_name,))
                ex_id = conn.execute(
                    "SELECT id FROM exercises WHERE name=?",
                    (ex_name,)
                ).fetchone()["id"]

                conn.execute("""
                    INSERT OR IGNORE INTO template_exercises(template_id, exercise_id, order_index)
                    VALUES (?, ?, ?)
                """, (template_id, ex_id, idx))

        conn.commit()


def get_templates():
    with db_conn() as conn:
        rows = conn.execute("SELECT id, name FROM workout_templates ORDER BY name").fetchall()
        return [dict(r) for r in rows]


def get_template_by_id(tid: int):
    with db_conn() as conn:
        row = conn.execute("SELECT id, name FROM workout_templates WHERE id=?", (tid,)).fetchone()
        return dict(row) if row else None


def get_exercises_for_template(tid: int):
    with db_conn() as conn:
        rows = conn.execute("""
            SELECT e.id, e.name, te.order_index
            FROM template_exercises te
            JOIN exercises e ON e.id = te.exercise_id
            WHERE te.template_id = ?
            ORDER BY te.order_index
        """, (tid,)).fetchall()
        return [dict(r) for r in rows]


def fetch_last_for_exercises(exercise_names: list[str]):
    if not exercise_names:
        return {}

    placeholders = ",".join(["?"] * len(exercise_names))
    with db_conn() as conn:
        rows = conn.execute(f"""
            SELECT se.exercise, se.weight, se.reps, se.day
            FROM set_entries se
            JOIN (
                SELECT exercise, MAX(id) AS max_id
                FROM set_entries
                WHERE exercise IN ({placeholders})
                GROUP BY exercise
            ) last ON last.max_id = se.id
        """, exercise_names).fetchall()

        return {r["exercise"]: dict(r) for r in rows}


def fetch_pr_for_exercises(exercise_names: list[str]):
    """
    PR rule:
      - highest weight wins
      - if weight ties, highest reps wins
      - if still tied, latest entry (max id) wins
    """
    if not exercise_names:
        return {}

    placeholders = ",".join(["?"] * len(exercise_names))
    with db_conn() as conn:
        rows = conn.execute(f"""
            SELECT se.exercise, se.weight, se.reps, se.day
            FROM set_entries se
            JOIN (
                SELECT exercise, MAX(weight) AS max_weight
                FROM set_entries
                WHERE exercise IN ({placeholders})
                GROUP BY exercise
            ) mw
              ON mw.exercise = se.exercise AND mw.max_weight = se.weight
            JOIN (
                SELECT exercise, weight, MAX(reps) AS max_reps
                FROM set_entries
                WHERE exercise IN ({placeholders})
                GROUP BY exercise, weight
            ) mr
              ON mr.exercise = se.exercise
             AND mr.weight = se.weight
             AND mr.max_reps = se.reps
            JOIN (
                SELECT exercise, weight, reps, MAX(id) AS max_id
                FROM set_entries
                WHERE exercise IN ({placeholders})
                GROUP BY exercise, weight, reps
            ) tie
              ON tie.exercise = se.exercise
             AND tie.weight = se.weight
             AND tie.reps = se.reps
             AND tie.max_id = se.id
        """, exercise_names + exercise_names + exercise_names).fetchall()

        return {r["exercise"]: dict(r) for r in rows}


def get_active_session_id(template_id: int, day: str):
    with db_conn() as conn:
        row = conn.execute("""
            SELECT id FROM workout_sessions
            WHERE template_id=? AND day=? AND ended_at IS NULL
            ORDER BY id DESC
            LIMIT 1
        """, (template_id, day)).fetchone()
        return row["id"] if row else None


def ensure_active_session(template_id: int, workout_name: str, day: str):
    existing = get_active_session_id(template_id, day)
    if existing:
        return existing

    now = datetime.now().isoformat(timespec="seconds")
    with db_conn() as conn:
        conn.execute("""
            INSERT INTO workout_sessions (day, template_id, workout_name, started_at, ended_at)
            VALUES (?, ?, ?, ?, NULL)
        """, (day, template_id, workout_name, now))
        conn.commit()
        return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def close_active_session(template_id: int, day: str):
    sid = get_active_session_id(template_id, day)
    if not sid:
        return None

    now = datetime.now().isoformat(timespec="seconds")
    with db_conn() as conn:
        conn.execute("UPDATE workout_sessions SET ended_at=? WHERE id=?", (now, sid))
        conn.commit()
    return sid


def fetch_sets_for_session(session_id: int, limit: int = 200):
    if not session_id:
        return []
    with db_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM set_entries
            WHERE session_id=?
            ORDER BY id DESC
            LIMIT ?
        """, (session_id, limit)).fetchall()
        return [dict(r) for r in rows]


def get_db_info():
    exists = DB_PATH.exists()
    size = DB_PATH.stat().st_size if exists and DB_PATH.is_file() else None
    counts = {}
    try:
        with db_conn() as conn:
            for t in ("workout_templates", "exercises", "template_exercises", "workout_sessions", "set_entries"):
                try:
                    counts[t] = conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
                except Exception:
                    counts[t] = None
    except Exception:
        logger.exception("Failed to open DB for info")
    return {"db_path": str(DB_PATH), "exists": exists, "size": size, "counts": counts}


# Init + seed on startup (safer for serverless cold starts)
@app.on_event("startup")
def startup():
    init_db()
    seed_templates_if_empty()
    logger.info("DB path: %s exists=%s", DB_PATH, DB_PATH.exists())
    logger.info("DB counts: %s", get_db_info().get("counts"))


@app.get("/", response_class=HTMLResponse)
def home(request: Request, t: int | None = None, edit: int = 0):
    templates_list = get_templates()

    selected_tid = t
    if not selected_tid and templates_list:
        selected_tid = templates_list[0]["id"]

    selected_template = get_template_by_id(selected_tid) if selected_tid else None
    exercises = get_exercises_for_template(selected_tid) if selected_tid else []

    exercise_names = [ex["name"] for ex in exercises]
    last = fetch_last_for_exercises(exercise_names)
    pr = fetch_pr_for_exercises(exercise_names)

    today = date.today().isoformat()

    active_session_id = get_active_session_id(selected_tid, today) if selected_tid else None
    session_sets = fetch_sets_for_session(active_session_id, 200)

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "templates": templates_list,
            "selected_template": selected_template,
            "exercises": exercises,
            "last": last,
            "pr": pr,
            "today": today,
            "edit": (edit == 1),

            # session data
            "active_session_id": active_session_id,
            "session_sets": session_sets,
        },
    )


@app.get("/admin/db_info")
def admin_db_info():
    # Quick endpoint to verify which DB file is used and basic table counts (check Vercel logs / response)
    return get_db_info()


@app.post("/log")
def log_set(
    template_id: int = Form(...),
    workout: str = Form(...),
    exercise: str = Form(...),
    weight: float = Form(...),
    reps: int = Form(...),
):
    now = datetime.now().isoformat(timespec="seconds")
    day = date.today().isoformat()

    # basic guardrails (adjust to taste)
    if weight < 0 or weight > 2000 or reps < 1 or reps > 200:
        return RedirectResponse(url=f"/?t={template_id}", status_code=303)

    try:
        session_id = ensure_active_session(template_id, workout, day)

        with db_conn() as conn:
            conn.execute("""
                INSERT INTO set_entries (day, workout, exercise, weight, reps, created_at, session_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (day, workout, exercise, weight, reps, now, session_id))
            conn.commit()
    except Exception:
        logger.exception("Failed to save set entry")
        raise HTTPException(status_code=500, detail="Failed to save set; check server logs")

    return RedirectResponse(url=f"/?t={template_id}", status_code=303)


@app.post("/template/add_exercise")
def add_exercise(template_id: int = Form(...), exercise_name: str = Form(...)):
    name = exercise_name.strip()
    if not name:
        return RedirectResponse(url=f"/?t={template_id}&edit=1", status_code=303)

    with db_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO exercises(name) VALUES (?)", (name,))
        ex_id = conn.execute("SELECT id FROM exercises WHERE name=?", (name,)).fetchone()["id"]

        max_row = conn.execute(
            "SELECT COALESCE(MAX(order_index), -1) AS m FROM template_exercises WHERE template_id=?",
            (template_id,)
        ).fetchone()
        next_idx = max_row["m"] + 1

        conn.execute("""
            INSERT OR IGNORE INTO template_exercises(template_id, exercise_id, order_index)
            VALUES (?, ?, ?)
        """, (template_id, ex_id, next_idx))

        conn.commit()

    return RedirectResponse(url=f"/?t={template_id}&edit=1", status_code=303)


@app.post("/template/remove_exercise")
def remove_exercise(template_id: int = Form(...), exercise_id: int = Form(...)):
    with db_conn() as conn:
        conn.execute("""
            DELETE FROM template_exercises
            WHERE template_id=? AND exercise_id=?
        """, (template_id, exercise_id))
        conn.commit()

    return RedirectResponse(url=f"/?t={template_id}&edit=1", status_code=303)


@app.post("/session/done")
def done_session(template_id: int = Form(...)):
    day = date.today().isoformat()
    close_active_session(template_id, day)
    return RedirectResponse(url=f"/?t={template_id}", status_code=303)
