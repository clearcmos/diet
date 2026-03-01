#!/usr/bin/env python3
"""Diet CLI - query and manage your food nutrition database using Claude."""

import asyncio
import sys
import sqlite3
import os
import json

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    TextBlock,
    tool,
    create_sdk_mcp_server,
)
from rich.console import Console
from rich.markdown import Markdown

DB_PATH = os.path.expanduser("~/.local/share/diet-db/diet.db")
SOURCE_PATH = os.path.abspath(__file__)
IMPROVEMENTS_LOG = os.path.expanduser("~/.local/share/diet-db/improvements.log")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_foods_notes_column():
    """Add notes column to foods table if it doesn't exist."""
    conn = get_db()
    cursor = conn.execute("PRAGMA table_info(foods)")
    columns = [row["name"] for row in cursor.fetchall()]
    if "notes" not in columns:
        conn.execute("ALTER TABLE foods ADD COLUMN notes TEXT")
        conn.commit()
    conn.close()


def ensure_cooking_tables():
    """Create cooking knowledge base tables if they don't exist."""
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cooking_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            food_name TEXT NOT NULL,
            food_state TEXT DEFAULT 'raw',
            method TEXT NOT NULL DEFAULT 'breville',
            temp_f INTEGER NOT NULL,
            time_min INTEGER NOT NULL,
            cut TEXT,
            amount_g REAL,
            notes TEXT,
            confidence TEXT DEFAULT 'estimated',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cooking_combos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            method TEXT NOT NULL DEFAULT 'breville',
            temp_f INTEGER NOT NULL,
            total_time_min INTEGER NOT NULL,
            outcome TEXT,
            notes TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS combo_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            combo_id INTEGER NOT NULL REFERENCES cooking_combos(id) ON DELETE CASCADE,
            food_name TEXT NOT NULL,
            food_state TEXT DEFAULT 'raw',
            amount_g REAL,
            cut TEXT,
            item_outcome TEXT,
            notes TEXT
        )
    """)
    # Seed known data points if tables are empty
    count = conn.execute("SELECT COUNT(*) FROM cooking_records").fetchone()[0]
    if count == 0:
        conn.executemany(
            "INSERT INTO cooking_records (food_name, food_state, method, temp_f, time_min, amount_g, notes, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("chicken breast", "raw", "breville", 400, 30, 265, "on tray with veg", "tested", "2026-02-28"),
                ("broccoli", "raw", "breville", 400, 30, 165, "charred edges fine", "tested", "2026-02-28"),
                ("potato", "raw", "breville", 400, 30, 120, None, "tested", "2026-02-28"),
                ("burger", "frozen", "breville", 400, 17, 226, "Costco spindle pack, thin patties, x2", "tested", "2026-02-28"),
            ],
        )
        cursor = conn.execute(
            "INSERT INTO cooking_combos (method, temp_f, total_time_min, outcome, notes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("breville", 400, 30, "great", "chicken breast + broccoli + potato, one tray", "2026-02-28"),
        )
        combo_id = cursor.lastrowid
        conn.executemany(
            "INSERT INTO combo_items (combo_id, food_name, food_state, amount_g, item_outcome) VALUES (?, ?, ?, ?, ?)",
            [
                (combo_id, "chicken breast", "raw", 265, "perfect"),
                (combo_id, "broccoli", "raw", 165, "perfect"),
                (combo_id, "potato", "raw", 120, "perfect"),
            ],
        )
    conn.commit()
    conn.close()


def ensure_extra_tables():
    """Create goals and weight_log tables if they don't exist."""
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            daily_cal REAL,
            daily_protein REAL,
            daily_fat REAL,
            daily_carbs REAL,
            daily_fiber REAL,
            meals_per_day INTEGER,
            weight_goal TEXT,
            notes TEXT,
            updated_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS weight_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            weight REAL NOT NULL,
            unit TEXT DEFAULT 'lbs',
            note TEXT
        )
    """)
    conn.commit()
    conn.close()


def load_goals_context() -> str:
    """Load goals from DB and format as system prompt context."""
    ensure_extra_tables()
    conn = get_db()
    row = conn.execute("SELECT * FROM goals WHERE id = 1").fetchone()
    conn.close()
    if not row or not row["daily_cal"]:
        return "\nNo diet goals set yet. If the user discusses goals, offer to save them with save_goals.\n"
    meals = row["meals_per_day"] or 3
    per_meal_cal = round(row["daily_cal"] / meals) if row["daily_cal"] else "?"
    per_meal_prot = round(row["daily_protein"] / meals) if row["daily_protein"] else "?"
    per_meal_fat = round(row["daily_fat"] / meals) if row["daily_fat"] else "?"
    per_meal_carbs = round(row["daily_carbs"] / meals) if row["daily_carbs"] else "?"
    parts = [
        f"\nUSER'S DIET GOALS (last updated: {row['updated_at'] or 'unknown'}):",
        f"  Daily targets: {row['daily_cal']} cal, {row['daily_protein']}g protein, {row['daily_fat']}g fat, {row['daily_carbs']}g carbs, {row['daily_fiber']}g fiber",
        f"  Meals per day: {meals}",
        f"  Per-meal targets: ~{per_meal_cal} cal, ~{per_meal_prot}g protein, ~{per_meal_fat}g fat, ~{per_meal_carbs}g carbs",
        f"  Weight goal: {row['weight_goal'] or 'not set'}",
    ]
    if row["notes"]:
        parts.append(f"  Notes: {row['notes']}")
    parts.append("Always use these targets when building meals, checking daily progress, or giving recommendations.\n")
    return "\n".join(parts)


# --- MCP Tools for Claude ---

@tool(
    "lookup_food",
    "Search for a food in the diet database by name (fuzzy match). Handles plurals automatically. Returns nutrition info per 100g.",
    {"query": str},
)
async def lookup_food(args):
    query = args["query"].lower().strip()
    conn = get_db()
    # Try exact match first, then strip trailing 's'/'es' for plural handling
    candidates = [query]
    if query.endswith("oes"):
        candidates.append(query[:-2])  # potatoes -> potato
        candidates.append(query[:-3])  # potatoes -> potat (unlikely but safe)
    elif query.endswith("es"):
        candidates.append(query[:-2])  # tomatoes -> tomato
        candidates.append(query[:-1])  # dishes -> dish (won't match but safe)
    elif query.endswith("s"):
        candidates.append(query[:-1])  # carrots -> carrot
    # Also try adding 's' in case DB has plural but query is singular
    candidates.append(query + "s")
    candidates.append(query + "es")

    rows = []
    for c in candidates:
        rows = conn.execute(
            "SELECT * FROM foods WHERE name LIKE ? ORDER BY name",
            (f"%{c}%",),
        ).fetchall()
        if rows:
            break
    conn.close()
    if not rows:
        return {"content": [{"type": "text", "text": f"No food found matching '{query}'"}]}
    results = []
    for r in rows:
        entry = {
            "name": r["name"], "fat": r["fat"], "carb": r["carb"],
            "prot": r["prot"], "fiber": r["fiber"], "gram": r["gram"],
            "cal": r["cal"], "iron": r["iron"], "sugar": r["sugar"],
            "sodium": r["sodium"],
        }
        try:
            if r["notes"]:
                entry["notes"] = r["notes"]
        except (IndexError, KeyError):
            pass
        results.append(entry)
    return {"content": [{"type": "text", "text": json.dumps(results, indent=2)}]}


@tool(
    "list_foods",
    "List all foods in the diet database.",
    {},
)
async def list_all_foods(args):
    conn = get_db()
    rows = conn.execute("SELECT * FROM foods ORDER BY name").fetchall()
    conn.close()
    if not rows:
        return {"content": [{"type": "text", "text": "Database is empty."}]}
    results = []
    for r in rows:
        entry = dict(r)
        if entry.get("notes") is None:
            entry.pop("notes", None)
        results.append(entry)
    return {"content": [{"type": "text", "text": json.dumps(results, indent=2)}]}


@tool(
    "add_food",
    "Add or update a food in the diet database. All nutrition values are per 100g. Optional notes field for serving size info (e.g. '1 burger = 113g') or other context.",
    {"name": str, "fat": float, "carb": float, "prot": float, "fiber": float, "gram": float, "cal": float, "iron": float, "sugar": float, "sodium": float, "notes": str},
)
async def add_food(args):
    conn = get_db()
    notes = args.get("notes") or None
    conn.execute(
        "INSERT OR REPLACE INTO foods (name, fat, carb, prot, fiber, gram, cal, iron, sugar, sodium, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (args["name"], args["fat"], args["carb"], args["prot"], args["fiber"], args["gram"], args["cal"], args["iron"], args["sugar"], args["sodium"], notes),
    )
    conn.commit()
    conn.close()
    return {"content": [{"type": "text", "text": f"Added/updated '{args['name']}' in the database."}]}


@tool(
    "delete_food",
    "Delete a food from the diet database by name.",
    {"name": str},
)
async def delete_food(args):
    conn = get_db()
    result = conn.execute("DELETE FROM foods WHERE name = ?", (args["name"],))
    conn.commit()
    deleted = result.rowcount
    conn.close()
    if deleted:
        return {"content": [{"type": "text", "text": f"Deleted '{args['name']}' from the database."}]}
    return {"content": [{"type": "text", "text": f"No food named '{args['name']}' found."}]}


@tool(
    "log_meal",
    "Log a food item to the meal tracker. Records date, time, meal type, food name, grams, and all scaled nutrition values. Call this once per food item in the meal.",
    {"date": str, "time": str, "meal_type": str, "food_name": str, "grams": float, "cal": float, "fat": float, "carb": float, "prot": float, "fiber": float, "sugar": float, "iron": float, "sodium": float},
)
async def log_meal(args):
    conn = get_db()
    conn.execute(
        "INSERT INTO meal_log (date, time, meal_type, food_name, grams, cal, fat, carb, prot, fiber, sugar, iron, sodium) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (args["date"], args["time"], args["meal_type"], args["food_name"], args["grams"],
         args["cal"], args["fat"], args["carb"], args["prot"], args["fiber"],
         args["sugar"], args["iron"], args["sodium"]),
    )
    conn.commit()
    conn.close()
    return {"content": [{"type": "text", "text": f"Logged {args['grams']}g {args['food_name']} as {args['meal_type']} on {args['date']} at {args['time']}"}]}


@tool(
    "get_meal_log",
    "Retrieve meal log entries. Filter by date (YYYY-MM-DD), meal_type (breakfast/lunch/dinner/snack), or both. Pass empty string to skip a filter.",
    {"date": str, "meal_type": str},
)
async def get_meal_log(args):
    conn = get_db()
    query = "SELECT * FROM meal_log WHERE 1=1"
    params = []
    if args["date"]:
        query += " AND date = ?"
        params.append(args["date"])
    if args["meal_type"]:
        query += " AND meal_type = ?"
        params.append(args["meal_type"])
    query += " ORDER BY date DESC, time DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    if not rows:
        return {"content": [{"type": "text", "text": "No meal log entries found."}]}
    results = [dict(r) for r in rows]
    return {"content": [{"type": "text", "text": json.dumps(results, indent=2)}]}


@tool(
    "delete_meal_log",
    "Delete a meal log entry by its ID.",
    {"id": int},
)
async def delete_meal_log(args):
    conn = get_db()
    result = conn.execute("DELETE FROM meal_log WHERE id = ?", (args["id"],))
    conn.commit()
    deleted = result.rowcount
    conn.close()
    if deleted:
        return {"content": [{"type": "text", "text": f"Deleted meal log entry #{args['id']}."}]}
    return {"content": [{"type": "text", "text": f"No meal log entry with ID {args['id']} found."}]}


@tool(
    "save_preference",
    "Save a learned user preference. Categories: seasoning, cooking, combo, pairing, flavor, general. Key is a short label, value is the learned detail. learned_from is brief context of how you learned this.",
    {"category": str, "key": str, "value": str, "learned_from": str},
)
async def save_preference(args):
    from datetime import datetime
    conn = get_db()
    # Check if a preference with same category+key exists, update it
    existing = conn.execute(
        "SELECT id FROM preferences WHERE category = ? AND key = ?",
        (args["category"], args["key"]),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE preferences SET value = ?, learned_from = ?, created_at = ? WHERE id = ?",
            (args["value"], args["learned_from"], datetime.now().strftime("%Y-%m-%d %H:%M"), existing["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO preferences (category, key, value, learned_from, created_at) VALUES (?, ?, ?, ?, ?)",
            (args["category"], args["key"], args["value"], args["learned_from"], datetime.now().strftime("%Y-%m-%d %H:%M")),
        )
    conn.commit()
    conn.close()
    return {"content": [{"type": "text", "text": f"Learned: [{args['category']}] {args['key']} = {args['value']}"}]}


@tool(
    "get_preferences",
    "Retrieve user preferences. Filter by category (seasoning/cooking/combo/pairing/flavor/general) or pass empty string for all.",
    {"category": str},
)
async def get_preferences(args):
    conn = get_db()
    if args["category"]:
        rows = conn.execute(
            "SELECT * FROM preferences WHERE category = ? ORDER BY created_at DESC", (args["category"],)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM preferences ORDER BY category, created_at DESC").fetchall()
    conn.close()
    if not rows:
        return {"content": [{"type": "text", "text": "No preferences saved yet."}]}
    results = [dict(r) for r in rows]
    return {"content": [{"type": "text", "text": json.dumps(results, indent=2)}]}


@tool(
    "delete_preference",
    "Delete a preference by its ID.",
    {"id": int},
)
async def delete_preference(args):
    conn = get_db()
    result = conn.execute("DELETE FROM preferences WHERE id = ?", (args["id"],))
    conn.commit()
    deleted = result.rowcount
    conn.close()
    if deleted:
        return {"content": [{"type": "text", "text": f"Deleted preference #{args['id']}."}]}
    return {"content": [{"type": "text", "text": f"No preference with ID {args['id']} found."}]}


@tool(
    "save_goals",
    "Save or update the user's diet goals. All fields optional - pass 0 to skip a numeric field, empty string to skip text. Goals persist across sessions.",
    {"daily_cal": float, "daily_protein": float, "daily_fat": float, "daily_carbs": float, "daily_fiber": float, "meals_per_day": int, "weight_goal": str, "notes": str},
)
async def save_goals(args):
    from datetime import datetime
    ensure_extra_tables()
    conn = get_db()
    existing = conn.execute("SELECT * FROM goals WHERE id = 1").fetchone()
    if existing:
        # Merge: only update fields that have non-zero/non-empty values
        conn.execute(
            """UPDATE goals SET
                daily_cal = CASE WHEN ? > 0 THEN ? ELSE daily_cal END,
                daily_protein = CASE WHEN ? > 0 THEN ? ELSE daily_protein END,
                daily_fat = CASE WHEN ? > 0 THEN ? ELSE daily_fat END,
                daily_carbs = CASE WHEN ? > 0 THEN ? ELSE daily_carbs END,
                daily_fiber = CASE WHEN ? > 0 THEN ? ELSE daily_fiber END,
                meals_per_day = CASE WHEN ? > 0 THEN ? ELSE meals_per_day END,
                weight_goal = CASE WHEN ? != '' THEN ? ELSE weight_goal END,
                notes = CASE WHEN ? != '' THEN ? ELSE notes END,
                updated_at = ?
            WHERE id = 1""",
            (args["daily_cal"], args["daily_cal"],
             args["daily_protein"], args["daily_protein"],
             args["daily_fat"], args["daily_fat"],
             args["daily_carbs"], args["daily_carbs"],
             args["daily_fiber"], args["daily_fiber"],
             args["meals_per_day"], args["meals_per_day"],
             args["weight_goal"], args["weight_goal"],
             args["notes"], args["notes"],
             datetime.now().strftime("%Y-%m-%d %H:%M")),
        )
    else:
        conn.execute(
            "INSERT INTO goals (id, daily_cal, daily_protein, daily_fat, daily_carbs, daily_fiber, meals_per_day, weight_goal, notes, updated_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (args["daily_cal"], args["daily_protein"], args["daily_fat"], args["daily_carbs"],
             args["daily_fiber"], args["meals_per_day"], args["weight_goal"], args["notes"],
             datetime.now().strftime("%Y-%m-%d %H:%M")),
        )
    conn.commit()
    conn.close()
    return {"content": [{"type": "text", "text": "Diet goals saved! These will be loaded automatically every session."}]}


@tool(
    "get_goals",
    "Retrieve the user's current diet goals.",
    {},
)
async def get_goals(args):
    ensure_extra_tables()
    conn = get_db()
    row = conn.execute("SELECT * FROM goals WHERE id = 1").fetchone()
    conn.close()
    if not row or not row["daily_cal"]:
        return {"content": [{"type": "text", "text": "No diet goals set yet."}]}
    return {"content": [{"type": "text", "text": json.dumps(dict(row), indent=2)}]}


@tool(
    "log_weight",
    "Log a weight measurement. Date is YYYY-MM-DD, weight is a number, unit is 'lbs' or 'kg', note is optional context.",
    {"date": str, "weight": float, "unit": str, "note": str},
)
async def log_weight(args):
    ensure_extra_tables()
    conn = get_db()
    conn.execute(
        "INSERT INTO weight_log (date, weight, unit, note) VALUES (?, ?, ?, ?)",
        (args["date"], args["weight"], args["unit"] or "lbs", args["note"] or None),
    )
    conn.commit()
    conn.close()
    return {"content": [{"type": "text", "text": f"Logged {args['weight']} {args['unit'] or 'lbs'} on {args['date']}"}]}


@tool(
    "get_weight_log",
    "Retrieve weight log entries. Pass a number of recent entries to return (0 for all), or a date range as 'YYYY-MM-DD,YYYY-MM-DD'.",
    {"recent": int, "date_range": str},
)
async def get_weight_log(args):
    ensure_extra_tables()
    conn = get_db()
    if args["date_range"]:
        dates = args["date_range"].split(",")
        if len(dates) == 2:
            rows = conn.execute(
                "SELECT * FROM weight_log WHERE date BETWEEN ? AND ? ORDER BY date DESC",
                (dates[0].strip(), dates[1].strip()),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM weight_log ORDER BY date DESC").fetchall()
    elif args["recent"] > 0:
        rows = conn.execute(
            "SELECT * FROM weight_log ORDER BY date DESC LIMIT ?", (args["recent"],)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM weight_log ORDER BY date DESC").fetchall()
    conn.close()
    if not rows:
        return {"content": [{"type": "text", "text": "No weight entries logged yet."}]}
    return {"content": [{"type": "text", "text": json.dumps([dict(r) for r in rows], indent=2)}]}


@tool(
    "delete_weight_log",
    "Delete a weight log entry by its ID.",
    {"id": int},
)
async def delete_weight_log(args):
    conn = get_db()
    result = conn.execute("DELETE FROM weight_log WHERE id = ?", (args["id"],))
    conn.commit()
    deleted = result.rowcount
    conn.close()
    if deleted:
        return {"content": [{"type": "text", "text": f"Deleted weight entry #{args['id']}."}]}
    return {"content": [{"type": "text", "text": f"No weight entry with ID {args['id']} found."}]}


@tool(
    "read_own_source",
    "Read the diet CLI's own Python source code (diet.py). Use this to analyze current tools, logic, and system prompt before proposing improvements.",
    {},
)
async def read_own_source(args):
    try:
        with open(SOURCE_PATH, "r") as f:
            source = f.read()
        return {"content": [{"type": "text", "text": source}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Error reading source: {e}"}]}


@tool(
    "edit_own_source",
    "Apply a surgical edit to diet.py. Provide old_string (exact text to find) and new_string (replacement). "
    "The old_string must match EXACTLY one location in the file (including whitespace/indentation). "
    "For multiple edits, call this tool multiple times. Changes take effect on next restart.",
    {"old_string": str, "new_string": str, "description": str},
)
async def edit_own_source(args):
    from datetime import datetime
    try:
        with open(SOURCE_PATH, "r") as f:
            source = f.read()
        old = args["old_string"]
        new = args["new_string"]
        count = source.count(old)
        if count == 0:
            return {"content": [{"type": "text", "text": f"Error: old_string not found in source. Read the source first with read_own_source to get exact text."}]}
        if count > 1:
            return {"content": [{"type": "text", "text": f"Error: old_string matches {count} locations. Provide more surrounding context to uniquely identify the edit location."}]}
        updated = source.replace(old, new, 1)
        with open(SOURCE_PATH, "w") as f:
            f.write(updated)
        os.makedirs(os.path.dirname(IMPROVEMENTS_LOG), exist_ok=True)
        with open(IMPROVEMENTS_LOG, "a") as f:
            f.write(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M')}] {args['description']}\n")
        return {"content": [{"type": "text", "text": f"Edit applied and logged: {args['description']}\nRestart the session to activate changes."}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Error editing source: {e}"}]}


@tool(
    "get_improvement_log",
    "Read the history of self-improvements made to diet.py. Shows timestamped descriptions of all past code changes.",
    {},
)
async def get_improvement_log(args):
    try:
        if not os.path.exists(IMPROVEMENTS_LOG):
            return {"content": [{"type": "text", "text": "No improvements logged yet."}]}
        with open(IMPROVEMENTS_LOG, "r") as f:
            log = f.read().strip()
        if not log:
            return {"content": [{"type": "text", "text": "No improvements logged yet."}]}
        return {"content": [{"type": "text", "text": log}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Error reading improvement log: {e}"}]}


@tool(
    "save_cooking_time",
    "Store an individual food's cooking record. Allows multiple records per food for different states/cuts/amounts.",
    {"food_name": str, "method": str, "temp_f": int, "time_min": int, "food_state": str, "cut": str, "amount_g": float, "notes": str, "confidence": str},
)
async def save_cooking_time(args):
    from datetime import datetime
    ensure_cooking_tables()
    conn = get_db()
    conn.execute(
        "INSERT INTO cooking_records (food_name, food_state, method, temp_f, time_min, cut, amount_g, notes, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (args["food_name"].lower().strip(),
         args.get("food_state") or "raw",
         args.get("method") or "breville",
         args["temp_f"],
         args["time_min"],
         args.get("cut") or None,
         args.get("amount_g") or None,
         args.get("notes") or None,
         args.get("confidence") or "estimated",
         datetime.now().strftime("%Y-%m-%d")),
    )
    conn.commit()
    conn.close()
    return {"content": [{"type": "text", "text": f"Saved cooking record: {args['food_name']} — {args['time_min']} min at {args['temp_f']}°F ({args.get('confidence', 'estimated')})"}]}


@tool(
    "lookup_cooking_times",
    "Query cooking data for one or more foods. Returns individual records and a computed combo suggestion (max of individual times). Also checks past combo sessions involving these foods.",
    {"food_names": str, "method": str, "temp_f": int},
)
async def lookup_cooking_times(args):
    ensure_cooking_tables()
    conn = get_db()
    names = [n.strip().lower() for n in args["food_names"].split(",")]
    method = args.get("method") or "breville"
    temp_f = args.get("temp_f") or 400

    results = {}
    for name in names:
        rows = conn.execute(
            "SELECT * FROM cooking_records WHERE food_name LIKE ? AND method = ? ORDER BY confidence DESC, created_at DESC",
            (f"%{name}%", method),
        ).fetchall()
        results[name] = [dict(r) for r in rows]

    # Find past combo sessions involving any of these foods
    combo_matches = []
    for name in names:
        combos = conn.execute(
            """SELECT cc.*, ci.food_name, ci.food_state, ci.amount_g, ci.cut, ci.item_outcome
               FROM cooking_combos cc
               JOIN combo_items ci ON ci.combo_id = cc.id
               WHERE ci.food_name LIKE ? AND cc.method = ?
               ORDER BY cc.created_at DESC LIMIT 10""",
            (f"%{name}%", method),
        ).fetchall()
        for c in combos:
            combo_matches.append(dict(c))
    conn.close()

    # Compute combo suggestion
    bottleneck = None
    max_time = 0
    food_times = {}
    warnings = []
    for name, records in results.items():
        # Filter to matching temp (or close)
        matching = [r for r in records if abs(r["temp_f"] - temp_f) <= 25]
        if matching:
            best = matching[0]
            food_times[name] = best["time_min"]
            if best["time_min"] > max_time:
                max_time = best["time_min"]
                bottleneck = name
        else:
            food_times[name] = None

    for name, t in food_times.items():
        if t is not None and max_time > 0 and t < max_time * 0.6:
            warnings.append(f"{name} ({t} min) may overcook at {max_time} min — it needs significantly less time")

    output = {
        "individual_records": results,
        "combo_suggestion": {
            "suggested_time_min": max_time if max_time > 0 else None,
            "bottleneck": bottleneck,
            "food_times": food_times,
            "warnings": warnings,
        },
        "past_combo_sessions": combo_matches,
    }
    return {"content": [{"type": "text", "text": json.dumps(output, indent=2)}]}


@tool(
    "log_cooking_session",
    "Record a completed cooking session with combo details and per-item outcomes. Items is a JSON array of {food_name, food_state, amount_g, cut, item_outcome, notes}.",
    {"method": str, "temp_f": int, "total_time_min": int, "items": str, "outcome": str, "notes": str},
)
async def log_cooking_session(args):
    from datetime import datetime
    ensure_cooking_tables()
    conn = get_db()
    cursor = conn.execute(
        "INSERT INTO cooking_combos (method, temp_f, total_time_min, outcome, notes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (args.get("method") or "breville",
         args["temp_f"],
         args["total_time_min"],
         args.get("outcome") or None,
         args.get("notes") or None,
         datetime.now().strftime("%Y-%m-%d")),
    )
    combo_id = cursor.lastrowid
    items = json.loads(args["items"])
    for item in items:
        conn.execute(
            "INSERT INTO combo_items (combo_id, food_name, food_state, amount_g, cut, item_outcome, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (combo_id,
             item["food_name"].lower().strip(),
             item.get("food_state", "raw"),
             item.get("amount_g"),
             item.get("cut"),
             item.get("item_outcome"),
             item.get("notes")),
        )
        # Auto-update confidence to "tested" for matching cooking_records
        conn.execute(
            "UPDATE cooking_records SET confidence = 'tested' WHERE food_name LIKE ? AND method = ? AND confidence = 'estimated'",
            (f"%{item['food_name'].lower().strip()}%", args.get("method") or "breville"),
        )
    conn.commit()
    conn.close()
    item_names = ", ".join(i["food_name"] for i in items)
    return {"content": [{"type": "text", "text": f"Logged cooking session #{combo_id}: {item_names} — {args['total_time_min']} min at {args['temp_f']}°F ({args.get('outcome', 'no rating')})"}]}


@tool(
    "get_cooking_history",
    "Browse past cooking sessions. Optionally filter by food_name. Returns combo sessions with their items and outcomes.",
    {"food_name": str, "recent": int},
)
async def get_cooking_history(args):
    ensure_cooking_tables()
    conn = get_db()
    limit = args.get("recent") or 10
    food_filter = args.get("food_name", "").strip().lower()

    if food_filter:
        combos = conn.execute(
            """SELECT DISTINCT cc.* FROM cooking_combos cc
               JOIN combo_items ci ON ci.combo_id = cc.id
               WHERE ci.food_name LIKE ?
               ORDER BY cc.created_at DESC LIMIT ?""",
            (f"%{food_filter}%", limit),
        ).fetchall()
    else:
        combos = conn.execute(
            "SELECT * FROM cooking_combos ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    results = []
    for combo in combos:
        c = dict(combo)
        items = conn.execute(
            "SELECT * FROM combo_items WHERE combo_id = ?", (c["id"],)
        ).fetchall()
        c["items"] = [dict(i) for i in items]
        results.append(c)
    conn.close()

    if not results:
        return {"content": [{"type": "text", "text": "No cooking sessions logged yet."}]}
    return {"content": [{"type": "text", "text": json.dumps(results, indent=2)}]}


SYSTEM_PROMPT = """You are a diet nutrition assistant. You have access to a SQLite food database via tools.

When the user asks about a food with a specific amount (e.g. "150g red bell peppers"):
1. Look up the food in the database using lookup_food
2. Scale ALL nutrition values from per-100g to the requested amount
3. Present a clean summary with the scaled values

When the user asks to add a food, use add_food with per-100g values.
When the user asks to list foods, use list_foods.
When the user asks to delete a food, use delete_food.

IMPORTANT: If a food is NOT found in the database, offer to add it. Use your nutrition knowledge
to provide accurate per-100g values and say something like:
"I don't have [food] in your database. Want me to add it? Here's what I'd use (per 100g): ..."
Then if the user confirms (says yes, sure, ok, y, etc.), add it with add_food and answer their original question.
If the user's query included an amount, after adding, also provide the scaled nutrition for their requested amount.

FOOD NOTES:
Foods can have a "notes" field for useful context like:
- Serving size equivalents (e.g. "1 burger = 113g", "1 large egg = 50g")
- Brand info (e.g. "Costco Kirkland brand")
- Preparation notes (e.g. "raw weight, loses ~25% when cooked")
When looking up a food that has notes, display the notes to the user.
When adding a food, include notes if the user provides serving size info or other context.

COOKING TIMES:
You have a cooking_times knowledge base that accumulates data over time. Use it to give
evidence-based cooking recommendations instead of guessing.

WHEN THE USER ASKS HOW TO COOK SOMETHING:
1. Call lookup_cooking_times with the foods and method
2. If records exist: use them. Combo time = max of individual times (one tray, no flipping).
3. If no records: estimate from your knowledge, clearly mark as estimated, and offer to save.
4. If a food's individual time is much shorter than the combo time, note it may overcook
   but respect the user's "one shot" preference — just mention it.
5. Check past combo sessions for these foods to see if there are outcome notes.

AFTER COOKING:
If the user reports how it went ("that was perfect", "carrots were undercooked"), log it:
1. Call log_cooking_session with the combo details and per-item outcomes
2. If an individual food's time estimate was wrong, save a corrected cooking_record

The user's defaults: method="breville", temp_f=400, no flipping, no staggering.

When the user asks "how many g of X to equate Yg" or "to reach Yg" or similar, they mean:
"How many grams of food X do I need so that a specific macro (usually carbs) totals Yg?"
If there are already foods mentioned earlier in the conversation, subtract their contribution first,
then calculate how much of X is needed to fill the remainder. For example:
- Previous foods contribute 13.3g carbs, user wants 35g total carbs from potatoes
- Remaining = 35 - 13.3 = 21.7g carbs needed
- Potato has 17.5g carbs per 100g → need 21.7 / 17.5 * 100 = 124g potato

MEAL LOGGING:
When the user says "log this lunch", "log this dinner", "log this snack", "log this breakfast", etc.:
1. Use the foods and amounts discussed in the current conversation
2. Scale nutrition values to the actual amounts discussed
3. Call log_meal ONCE PER FOOD ITEM with the scaled values
4. Use today's current date (YYYY-MM-DD) and current time (HH:MM)
5. meal_type must be one of: breakfast, lunch, dinner, snack
6. After logging, show a summary of what was logged with totals

When the user asks to see their log ("show today's log", "what did I eat today", "show my meals", etc.):
- Use get_meal_log to retrieve entries
- Show a clean summary with per-meal and daily totals

SELF-LEARNING PREFERENCES:
You have a preferences database that persists across sessions. Use it to learn and remember the user's tastes.

WHEN TO SAVE (use save_preference automatically):
- User gives feedback on seasoning ("too peppery", "not enough salt") → save adjusted amounts
- User repeats food combos → save as a favorite combo
- User mentions cooking preferences ("I like it crispy", "30 min not a range") → save under cooking
- User corrects you on flavor/pairing ("I prefer X with Y") → save the pairing
- User states a general preference ("I like strong flavor", "I don't eat pork") → save under general

Categories: seasoning, cooking, combo, pairing, flavor, general
Key should be short and specific (e.g. "black pepper max", "chicken+potato combo", "breville oven time")
Value should be the actual learned detail with specifics (amounts, temps, times)

WHEN TO READ (use get_preferences automatically):
- At the START of any cooking/seasoning recommendation → check seasoning + cooking preferences
- When suggesting food combos → check combo + pairing preferences
- When the user asks "what do I like" or "my preferences" → show all

IMPORTANT: Always check preferences BEFORE making recommendations. If the user asked for
pepper amounts before and you learned they like less, use that knowledge immediately.
Don't ask "would you like me to save this?" — just save it silently when you learn something.
Do briefly mention what you learned (e.g. "Noted — I'll remember you prefer less pepper next time.").

WEIGHT TRACKING:
When the user says "I weigh X" or "weight today is X" or "log my weight":
- Use log_weight with today's date and the value
- Default unit is lbs unless they say kg
- Show their trend if they have previous entries (use get_weight_log recent=5)
When the user asks about weight progress, show recent entries and calculate rate of change.

DIET GOALS:
You have a goals database that persists across sessions. The user's current goals are loaded
into this prompt automatically on startup (see below).

WHEN TO SAVE GOALS (use save_goals automatically):
- User discusses calorie targets, macro splits, or weight loss goals
- User says "my goal is X lbs/week" or "I want to eat Y calories"
- User adjusts their plan ("let's bump it to 1700 cal")
Don't ask — just save when the user states or adjusts goals, and confirm briefly.

WHEN TO USE GOALS:
- When building meals → target the per-meal macros
- When logging → show remaining daily budget
- When the user asks "how am I doing today" → compare logged totals vs goals
- When suggesting portions → optimize to hit targets

Always show nutrition in a clear, readable format. Keep responses concise.
All values in the database are per 100g. The gram column is always 100 (reference serving size).
Iron and sodium are also in grams (not mg).

SELF-IMPROVEMENT:
You can read and modify your own source code (diet.py) using surgical edits. This lets you fix bugs,
add new tools, improve logic, and evolve your capabilities over time.

AUTONOMY MODES (check preferences for key "self_improvement_mode" under category "general"):
- "ask_always": Always explain the proposed change and wait for user approval before modifying code.
- "auto_minor" (DEFAULT if no preference set): Auto-apply minor fixes (bug fixes, small tweaks,
  prompt improvements) with a brief explanation. Ask before major changes (new tools, architectural changes).
- "auto_all": Apply all improvements automatically, just inform the user what changed.

HOW TO APPLY EDITS:
1. First check get_preferences for "self_improvement_mode" to know your autonomy level
2. Read your source with read_own_source
3. Briefly explain the proposed change to the user
4. Based on autonomy mode, either ask or proceed
5. Use edit_own_source with exact old_string/new_string pairs — one edit per tool call
6. NEVER rewrite the entire file. Only change the specific lines that need modification.
7. Tell the user to restart the session to activate changes
8. Continue the current conversation normally — don't stop helping just because you updated code

WHAT NOT TO MODIFY (regardless of autonomy mode):
- Never remove existing tools without the user explicitly asking
- Never change database schema without asking
- Never break the self-improvement tools themselves
- Never modify the core client/server setup in a way that could prevent startup

Check get_improvement_log before proposing changes to avoid re-suggesting past improvements."""


async def send_and_print(client, console, query):
    await client.query(query)
    output = []
    async for msg in client.receive_response():
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    output.append(block.text)
    if output:
        console.print(Markdown("\n\n".join(output)))


async def main():
    from datetime import datetime
    now = datetime.now()
    time_context = f"\nCurrent date: {now.strftime('%Y-%m-%d')}\nCurrent time: {now.strftime('%H:%M')}\n"

    goals_context = load_goals_context()
    ensure_foods_notes_column()
    ensure_cooking_tables()

    server = create_sdk_mcp_server(
        name="diet",
        version="1.0.0",
        tools=[lookup_food, list_all_foods, add_food, delete_food, log_meal, get_meal_log, delete_meal_log, save_preference, get_preferences, delete_preference, save_goals, get_goals, log_weight, get_weight_log, delete_weight_log, read_own_source, edit_own_source, get_improvement_log, save_cooking_time, lookup_cooking_times, log_cooking_session, get_cooking_history],
    )

    console = Console()

    async with ClaudeSDKClient(
        options=ClaudeAgentOptions(
            system_prompt=SYSTEM_PROMPT + time_context + goals_context,
            mcp_servers={"diet": server},
            allowed_tools=[
                "mcp__diet__lookup_food",
                "mcp__diet__list_foods",
                "mcp__diet__add_food",
                "mcp__diet__delete_food",
                "mcp__diet__log_meal",
                "mcp__diet__get_meal_log",
                "mcp__diet__delete_meal_log",
                "mcp__diet__save_preference",
                "mcp__diet__get_preferences",
                "mcp__diet__delete_preference",
                "mcp__diet__save_goals",
                "mcp__diet__get_goals",
                "mcp__diet__log_weight",
                "mcp__diet__get_weight_log",
                "mcp__diet__delete_weight_log",
                "mcp__diet__read_own_source",
                "mcp__diet__edit_own_source",
                "mcp__diet__get_improvement_log",
                "mcp__diet__save_cooking_time",
                "mcp__diet__lookup_cooking_times",
                "mcp__diet__log_cooking_session",
                "mcp__diet__get_cooking_history",
            ],
            model="claude-sonnet-4-6",
            permission_mode="bypassPermissions",
            max_turns=15,
        )
    ) as client:
        # If args provided, run that query first
        if len(sys.argv) > 1:
            user_query = " ".join(sys.argv[1:])
            await send_and_print(client, console, user_query)

        # Interactive loop
        while True:
            try:
                user_query = input("\ndiet> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not user_query:
                continue
            await send_and_print(client, console, user_query)


if __name__ == "__main__":
    asyncio.run(main())
