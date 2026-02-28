#!/usr/bin/env python3
"""Diet database initialization and management using SQLite."""

import sqlite3
import os

DB_PATH = os.path.expanduser("~/.local/share/diet-db/diet.db")


def get_connection():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS foods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            fat REAL NOT NULL DEFAULT 0,
            carb REAL NOT NULL DEFAULT 0,
            prot REAL NOT NULL DEFAULT 0,
            fiber REAL NOT NULL DEFAULT 0,
            gram REAL NOT NULL DEFAULT 100,
            cal REAL NOT NULL DEFAULT 0,
            iron REAL NOT NULL DEFAULT 0,
            sugar REAL NOT NULL DEFAULT 0,
            sodium REAL NOT NULL DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS meal_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            meal_type TEXT NOT NULL,
            food_name TEXT NOT NULL,
            grams REAL NOT NULL,
            cal REAL NOT NULL,
            fat REAL NOT NULL,
            carb REAL NOT NULL,
            prot REAL NOT NULL,
            fiber REAL NOT NULL,
            sugar REAL NOT NULL,
            iron REAL NOT NULL,
            sodium REAL NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS preferences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            learned_from TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def insert_food(conn, name, fat, carb, prot, fiber, gram, cal, iron, sugar, sodium):
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO foods (name, fat, carb, prot, fiber, gram, cal, iron, sugar, sodium)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (name, fat, carb, prot, fiber, gram, cal, iron, sugar, sodium))
    conn.commit()


def list_foods(conn):
    c = conn.cursor()
    c.execute("SELECT * FROM foods ORDER BY name")
    return c.fetchall()


def seed_data(conn):
    """Seed with initial food data. All values per 100g unless noted."""
    foods = [
        # name, fat, carb, prot, fiber, gram, cal, iron, sugar, sodium
        ("green bell pepper", 0.2, 4.6, 0.9, 1.7, 100, 20, 0.3, 2.4, 0.003),
        ("red bell pepper", 0.3, 6.0, 1.0, 2.1, 100, 31, 0.4, 4.2, 0.004),
        ("zucchini", 0.3, 3.1, 1.2, 1.0, 100, 17, 0.4, 2.5, 0.008),
    ]
    for food in foods:
        insert_food(conn, *food)


if __name__ == "__main__":
    conn = init_db()
    seed_data(conn)
    print("Diet database initialized.")
    print("\nFoods in database:")
    print(f"{'Name':<20} {'Fat':>6} {'Carb':>6} {'Prot':>6} {'Fiber':>6} {'Gram':>6} {'Cal':>6} {'Iron':>6} {'Sugar':>6} {'Sodium':>8}")
    print("-" * 92)
    for row in list_foods(conn):
        _id, name, fat, carb, prot, fiber, gram, cal, iron, sugar, sodium = row
        print(f"{name:<20} {fat:>6.1f} {carb:>6.1f} {prot:>6.1f} {fiber:>6.1f} {gram:>6.0f} {cal:>6.0f} {iron:>6.1f} {sugar:>6.1f} {sodium:>8.3f}")
    conn.close()
