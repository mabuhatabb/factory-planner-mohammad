import sqlite3
from datetime import datetime
import pandas as pd

DB_FILE = "planner.db"


def connect():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created TEXT,
        name TEXT,
        max_overtime INTEGER,
        overtime_cost INTEGER,
        late_units INTEGER,
        units_not_made INTEGER,
        overtime_minutes INTEGER)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS plan_lines (
        run_id INTEGER,
        day INTEGER,
        product TEXT,
        machine TEXT,
        qty INTEGER)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER,
        created TEXT,
        decision TEXT,
        reason TEXT)""")
    return conn


def save_run(name, max_overtime, overtime_cost,
             late_units, units_not_made, overtime_minutes, plan_df):
    conn = connect()
    cur = conn.execute(
        """INSERT INTO runs (created, name, max_overtime, overtime_cost,
                             late_units, units_not_made, overtime_minutes)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (datetime.now().strftime("%Y-%m-%d %H:%M"), name,
         int(max_overtime), int(overtime_cost),
         int(late_units), int(units_not_made), int(overtime_minutes)))
    run_id = cur.lastrowid
    rows = [(run_id, int(r.day), str(r.product), str(r.machine), int(r.qty))
            for r in plan_df.itertuples()]
    conn.executemany("INSERT INTO plan_lines VALUES (?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return run_id


def list_runs():
    """All saved plans, newest first, with the latest planner decision."""
    conn = connect()
    df = pd.read_sql_query(
        """SELECT r.id, r.created, r.name, r.max_overtime, r.overtime_cost,
                  r.late_units, r.units_not_made, r.overtime_minutes,
                  COALESCE((SELECT f.decision FROM feedback f
                            WHERE f.run_id = r.id
                            ORDER BY f.id DESC LIMIT 1), 'Not reviewed') AS decision
           FROM runs r ORDER BY r.id DESC""", conn)
    conn.close()
    return df


def load_lines(run_id):
    conn = connect()
    df = pd.read_sql_query(
        "SELECT day, product, machine, qty FROM plan_lines WHERE run_id = ?",
        conn, params=(int(run_id),))
    conn.close()
    return df


def save_feedback(run_id, decision, reason):
    conn = connect()
    conn.execute(
        "INSERT INTO feedback (run_id, created, decision, reason) VALUES (?, ?, ?, ?)",
        (int(run_id), datetime.now().strftime("%Y-%m-%d %H:%M"), decision, reason))
    conn.commit()
    conn.close()


def list_feedback():
    conn = connect()
    df = pd.read_sql_query(
        """SELECT f.created, f.run_id AS plan, r.name, f.decision, f.reason
           FROM feedback f LEFT JOIN runs r ON r.id = f.run_id
           ORDER BY f.id DESC""", conn)
    conn.close()
    return df