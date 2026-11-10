"""Creates a demo e-commerce + HR dataset in PostgreSQL.

    python -m app.seed            # from ./backend
    python -m app.seed --reset    # drop and recreate demo tables
"""
from __future__ import annotations

import argparse
import datetime as dt
import random

from sqlalchemy import text

from .db import engine, init_app_tables

DDL = """
CREATE TABLE departments (
    department_id SERIAL PRIMARY KEY,
    name          VARCHAR(60) NOT NULL UNIQUE,
    location      VARCHAR(60) NOT NULL
);
CREATE TABLE employees (
    employee_id   SERIAL PRIMARY KEY,
    full_name     VARCHAR(80) NOT NULL,
    department_id INTEGER NOT NULL REFERENCES departments(department_id),
    manager_id    INTEGER REFERENCES employees(employee_id),
    job_title     VARCHAR(60) NOT NULL,
    salary        NUMERIC(10,2) NOT NULL,
    hire_date     DATE NOT NULL
);
CREATE TABLE customers (
    customer_id SERIAL PRIMARY KEY,
    full_name   VARCHAR(80) NOT NULL,
    email       VARCHAR(120) NOT NULL UNIQUE,
    city        VARCHAR(60) NOT NULL,
    country     VARCHAR(40) NOT NULL,
    signup_date DATE NOT NULL
);
CREATE TABLE products (
    product_id SERIAL PRIMARY KEY,
    name       VARCHAR(80) NOT NULL,
    category   VARCHAR(40) NOT NULL,
    unit_price NUMERIC(10,2) NOT NULL,
    stock      INTEGER NOT NULL
);
CREATE TABLE orders (
    order_id    SERIAL PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
    order_date  DATE NOT NULL,
    status      VARCHAR(20) NOT NULL
);
CREATE TABLE order_items (
    order_item_id SERIAL PRIMARY KEY,
    order_id      INTEGER NOT NULL REFERENCES orders(order_id),
    product_id    INTEGER NOT NULL REFERENCES products(product_id),
    quantity      INTEGER NOT NULL,
    unit_price    NUMERIC(10,2) NOT NULL
);
CREATE INDEX ON orders(customer_id);
CREATE INDEX ON order_items(order_id);
CREATE INDEX ON order_items(product_id);
"""

FIRST = ["Aarav", "Diya", "Rohan", "Ananya", "Vihaan", "Isha", "Kabir", "Meera", "Arjun", "Saanvi",
         "Aditya", "Riya", "Karan", "Priya", "Dev", "Neha", "Rahul", "Tara", "Imran", "Zoya",
         "Sameer", "Nisha", "Yash", "Pooja", "Farhan", "Kavya", "Manav", "Sara", "Ishaan", "Lata"]
LAST = ["Sharma", "Verma", "Patel", "Khan", "Gupta", "Mehta", "Iyer", "Nair", "Reddy", "Singh",
        "Das", "Joshi", "Kapoor", "Malhotra", "Chopra", "Ansari", "Bose", "Rao", "Saxena", "Jain"]
CITIES = [("Bhopal", "India"), ("Mumbai", "India"), ("Delhi", "India"), ("Bengaluru", "India"),
          ("Pune", "India"), ("Hyderabad", "India"), ("Dubai", "UAE"), ("Singapore", "Singapore"),
          ("London", "UK"), ("Toronto", "Canada")]
DEPTS = [("Engineering", "Bengaluru"), ("Data Science", "Pune"), ("Sales", "Mumbai"),
         ("Marketing", "Delhi"), ("HR", "Bhopal"), ("Finance", "Mumbai")]
TITLES = {"Engineering": ["Software Engineer", "Senior Engineer", "QA Engineer"],
          "Data Science": ["Data Analyst", "ML Engineer", "Data Scientist"],
          "Sales": ["Sales Executive", "Account Manager"],
          "Marketing": ["Marketing Associate", "Content Strategist"],
          "HR": ["HR Generalist", "Recruiter"], "Finance": ["Accountant", "Financial Analyst"]}
PRODUCTS = [
    ("Wireless Mouse", "Electronics", 799), ("Mechanical Keyboard", "Electronics", 3499),
    ("USB-C Hub", "Electronics", 1899), ("Noise-Cancelling Headphones", "Electronics", 7999),
    ("27-inch Monitor", "Electronics", 15999), ("Webcam HD", "Electronics", 2499),
    ("Running Shoes", "Sports", 3999), ("Yoga Mat", "Sports", 899), ("Dumbbell Set", "Sports", 2999),
    ("Cricket Bat", "Sports", 2499), ("Water Bottle", "Sports", 399),
    ("Cotton T-Shirt", "Clothing", 599), ("Denim Jacket", "Clothing", 2799),
    ("Hoodie", "Clothing", 1499), ("Formal Shirt", "Clothing", 1299),
    ("Python Crash Course", "Books", 699), ("Deep Learning Handbook", "Books", 1599),
    ("SQL Cookbook", "Books", 899), ("Atomic Habits", "Books", 499),
    ("Desk Lamp", "Home", 1199), ("Office Chair", "Home", 8999), ("Standing Desk", "Home", 18999),
    ("Coffee Maker", "Home", 3299), ("Air Purifier", "Home", 9999), ("Notebook Set", "Home", 299),
]
STATUSES = ["delivered"] * 6 + ["shipped", "processing", "cancelled", "returned"]


def seed(reset: bool = False) -> None:
    rnd = random.Random(42)
    today = dt.date.today()
    init_app_tables()

    with engine.begin() as conn:
        exists = conn.execute(text("SELECT to_regclass('public.orders')")).scalar()
        if exists and not reset:
            print("Demo tables already exist. Use --reset to recreate them.")
            return
        conn.execute(text(
            "DROP TABLE IF EXISTS order_items, orders, products, customers, employees, departments CASCADE"
        ))
        conn.execute(text(DDL))

        for name, loc in DEPTS:
            conn.execute(text("INSERT INTO departments(name, location) VALUES (:n,:l)"),
                         {"n": name, "l": loc})
        dept_ids = {r[1]: r[0] for r in conn.execute(text("SELECT department_id, name FROM departments"))}

        # employees: first employee in each dept is the manager
        managers: dict[int, int] = {}
        for dname, did in dept_ids.items():
            for i in range(7):
                name = f"{rnd.choice(FIRST)} {rnd.choice(LAST)}"
                is_mgr = i == 0
                salary = rnd.randint(90, 150) * 1000 if is_mgr else rnd.randint(35, 130) * 1000
                title = f"Head of {dname}" if is_mgr else rnd.choice(TITLES[dname])
                hired = today - dt.timedelta(days=rnd.randint(60, 365 * 9))
                r = conn.execute(text(
                    "INSERT INTO employees(full_name, department_id, manager_id, job_title, salary, hire_date) "
                    "VALUES (:n,:d,:m,:t,:s,:h) RETURNING employee_id"),
                    {"n": name, "d": did, "m": None if is_mgr else managers[did], "t": title,
                     "s": salary, "h": hired})
                if is_mgr:
                    managers[did] = r.scalar_one()

        for i in range(80):
            city, country = rnd.choice(CITIES)
            name = f"{rnd.choice(FIRST)} {rnd.choice(LAST)}"
            conn.execute(text(
                "INSERT INTO customers(full_name, email, city, country, signup_date) VALUES (:n,:e,:c,:co,:s)"),
                {"n": name, "e": f"{name.lower().replace(' ', '.')}{i}@example.com", "c": city,
                 "co": country, "s": today - dt.timedelta(days=rnd.randint(30, 900))})

        for name, cat, price in PRODUCTS:
            conn.execute(text(
                "INSERT INTO products(name, category, unit_price, stock) VALUES (:n,:c,:p,:s)"),
                {"n": name, "c": cat, "p": price, "s": rnd.randint(0, 300)})

        # leave the last 3 products never-ordered (nice demo question)
        orderable = list(range(1, len(PRODUCTS) - 2))
        prices = {r[0]: r[1] for r in conn.execute(text("SELECT product_id, unit_price FROM products"))}
        for _ in range(600):
            cust = rnd.randint(1, 80)
            odate = today - dt.timedelta(days=rnd.randint(0, 540))
            oid = conn.execute(text(
                "INSERT INTO orders(customer_id, order_date, status) VALUES (:c,:d,:s) RETURNING order_id"),
                {"c": cust, "d": odate, "s": rnd.choice(STATUSES)}).scalar_one()
            for pid in rnd.sample(orderable, rnd.randint(1, 4)):
                conn.execute(text(
                    "INSERT INTO order_items(order_id, product_id, quantity, unit_price) VALUES (:o,:p,:q,:u)"),
                    {"o": oid, "p": pid, "q": rnd.randint(1, 4), "u": prices[pid]})

        # grant read access to the optional dedicated role
        has_role = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname='nlp_readonly'")).scalar()
        if has_role:
            conn.execute(text("GRANT USAGE ON SCHEMA public TO nlp_readonly"))
            conn.execute(text("GRANT SELECT ON ALL TABLES IN SCHEMA public TO nlp_readonly"))
            print("Granted SELECT to role nlp_readonly")

    print("Seeded demo data: 6 departments, 42 employees, 80 customers, 25 products, 600 orders.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    seed(ap.parse_args().reset)
