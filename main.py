import os
import sys
import json
import csv
import logging
import traceback
import calendar
import asyncio
from pathlib import Path
from datetime import date, timedelta
from fastmcp import FastMCP
import aiosqlite

# Setup proper logging
logging.basicConfig(
    level=logging.DEBUG,
    format='[%(asctime)s] %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stderr),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


mcp = FastMCP(name="Expense Tracker")

# Directory structure
SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
CONFIG_DIR = SCRIPT_DIR / "config"
EXPORTS_DIR = SCRIPT_DIR / "exports"
LOGS_DIR = SCRIPT_DIR / "logs"

# Ensure all directories exist
for dir_path in [DATA_DIR, CONFIG_DIR, EXPORTS_DIR, LOGS_DIR]:
    dir_path.mkdir(exist_ok=True)

# Database path with environment variable override
DB_PATH_ENV = os.getenv("EXPENSE_TRACKER_DB_PATH")
if DB_PATH_ENV:
    DB_PATH = DB_PATH_ENV
    logger.info(f"[PATHS] Using DB_PATH from environment: {DB_PATH}")
else:
    DB_PATH = str(DATA_DIR / "expenses.db")
    logger.info(f"[PATHS] Using default DB_PATH: {DB_PATH}")

# Categories file path with environment variable override
CATEGORIES_FILE_ENV = os.getenv("EXPENSE_TRACKER_CONFIG_PATH")
if CATEGORIES_FILE_ENV:
    CATEGORIES_FILE = CATEGORIES_FILE_ENV
    logger.info(f"[PATHS] Using CATEGORIES_FILE from environment: {CATEGORIES_FILE}")
else:
    CATEGORIES_FILE = str(CONFIG_DIR / "categories.json")
    logger.info(f"[PATHS] Using default CATEGORIES_FILE: {CATEGORIES_FILE}")

RECENT_EXPENSES_LIMIT = 10

logger.info(f"[PATHS] Script dir: {SCRIPT_DIR}")
logger.info(f"[PATHS] Data dir: {DATA_DIR} (writable: {os.access(DATA_DIR, os.W_OK)})")
logger.info(f"[PATHS] Config dir: {CONFIG_DIR} (writable: {os.access(CONFIG_DIR, os.W_OK)})")
logger.info(f"[PATHS] Exports dir: {EXPORTS_DIR} (writable: {os.access(EXPORTS_DIR, os.W_OK)})")
logger.info(f"[PATHS] Logs dir: {LOGS_DIR} (writable: {os.access(LOGS_DIR, os.W_OK)})")

# Load categories
def load_categories():
    """Load categories from JSON file."""
    try:
        with open(CATEGORIES_FILE, 'r') as f:
            data = json.load(f)
            return data.get("categories", [])
    except Exception as e:
        logger.error(f"Error loading categories: {e}")
        return []

CATEGORIES = load_categories()

# Build keyword maps for fuzzy suggestion
def build_keyword_maps():
    """Build subcategory keywords and aliases for fuzzy category matching."""
    subcategory_keywords = {}
    for cat in CATEGORIES:
        for sub in cat["subcategories"]:
            subcategory_keywords[sub.lower()] = (cat["name"], sub)

    alias_keywords = {
        "uber": ("Transport", "Cab"),
        "ola": ("Transport", "Cab"),
        "lyft": ("Transport", "Cab"),
        "swiggy": ("Food & Dining", "Delivery"),
        "zomato": ("Food & Dining", "Delivery"),
        "doordash": ("Food & Dining", "Delivery"),
        "netflix": ("Entertainment", "Streaming Services"),
        "spotify": ("Entertainment", "Streaming Services"),
        "prime": ("Entertainment", "Streaming Services"),
        "electricity": ("Utilities", "Electricity"),
        "power bill": ("Utilities", "Electricity"),
        "wifi": ("Utilities", "Internet"),
        "broadband": ("Utilities", "Internet"),
        "gym": ("Health & Medical", "Gym"),
        "rent": ("Rent & Housing", "Rent"),
    }

    return {**subcategory_keywords, **alias_keywords}

KEYWORD_MAPS = build_keyword_maps()


# ============================================================================
# SHARED HELPERS (ASYNC)
# ============================================================================

async def get_connection():
    """Get an async database connection."""
    try:
        db_dir = str(Path(DB_PATH).parent)

        # Detailed logging for debugging
        logger.info(f"[DB CONNECTION] Attempting to connect to: {DB_PATH}")
        logger.info(f"[DB CONNECTION] Database directory: {db_dir}")
        logger.info(f"[DB CONNECTION] Directory exists: {os.path.exists(db_dir)}")
        logger.info(f"[DB CONNECTION] Directory writable: {os.access(db_dir, os.W_OK) if os.path.exists(db_dir) else 'N/A'}")

        # Ensure directory exists
        os.makedirs(db_dir, exist_ok=True)
        logger.info(f"[DB CONNECTION] Directory ensured to exist: {os.path.exists(db_dir)}")

        # Try to connect
        logger.info(f"[DB CONNECTION] Calling aiosqlite.connect()...")
        conn = await aiosqlite.connect(DB_PATH)

        await conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = aiosqlite.Row

        logger.info(f"[DB CONNECTION] Successfully connected!")
        return conn
    except FileNotFoundError as e:
        logger.error(f"[DB CONNECTION] FileNotFoundError: {e}")
        logger.error(f"[DB CONNECTION] DB_PATH={DB_PATH}")
        logger.error(f"[DB CONNECTION] DB_PATH parent={Path(DB_PATH).parent}")
        logger.error(f"[DB CONNECTION] __file__={__file__}")
        logger.error(f"[DB CONNECTION] cwd={os.getcwd()}")
        raise
    except Exception as e:
        logger.error(f"[DB CONNECTION] Exception: {type(e).__name__}: {e}")
        logger.error(f"[DB CONNECTION] DB_PATH={DB_PATH}")
        logger.error(f"[DB CONNECTION] cwd={os.getcwd()}")
        raise


def _validate_category(category: str, subcategory: str = None) -> tuple[bool, dict, str | None]:
    """Validate category and subcategory against loaded categories.
    Returns (is_valid, error_dict_if_invalid, normalized_subcategory)"""
    valid_categories = {cat["name"] for cat in CATEGORIES}

    if category not in valid_categories:
        return False, {
            "success": False,
            "message": f"Invalid category: {category}",
            "available_categories": list(valid_categories)
        }, None

    normalized_sub = subcategory
    if subcategory:
        cat_obj = next((cat for cat in CATEGORIES if cat["name"] == category), None)
        if cat_obj:
            # Find matching subcategory (case-insensitive)
            matching_sub = next(
                (sub for sub in cat_obj["subcategories"] if sub.lower() == subcategory.lower()),
                None
            )
            if matching_sub:
                normalized_sub = matching_sub
            else:
                return False, {
                    "success": False,
                    "message": f"Invalid subcategory: {subcategory} for category: {category}",
                    "available_subcategories": cat_obj["subcategories"]
                }, None

    return True, {}, normalized_sub


def _suggest_category(description: str) -> tuple[str, str] | None:
    """Fuzzy-suggest a category from description. Returns (category, subcategory) or None."""
    desc_lower = description.lower()
    sorted_keywords = sorted(KEYWORD_MAPS.keys(), key=len, reverse=True)

    for keyword in sorted_keywords:
        if keyword in desc_lower:
            return KEYWORD_MAPS[keyword]

    return None


def _build_date_category_where(category: str = None, start_date: str = None, end_date: str = None) -> tuple[str, list]:
    """Build WHERE clause and params for date/category filtering."""
    where_clause = "WHERE 1=1"
    params = []

    if category:
        where_clause += " AND category = ?"
        params.append(category)

    if start_date:
        where_clause += " AND expense_date >= ?"
        params.append(start_date)

    if end_date:
        where_clause += " AND expense_date <= ?"
        params.append(end_date)

    return where_clause, params


def get_month_bounds(month: str = None) -> tuple[str, str]:
    """Get first and last day of a month (YYYY-MM-DD)."""
    if month:
        year, mon = map(int, month.split('-'))
    else:
        today = date.today()
        year, mon = today.year, today.month

    first_day = date(year, mon, 1)
    last_day_num = calendar.monthrange(year, mon)[1]
    last_day = date(year, mon, last_day_num)

    return first_day.isoformat(), last_day.isoformat()


async def init_db():
    """Initialize the SQLite database with all tables (non-destructive)."""
    try:
        logger.info(f"[INIT_DB] Starting database initialization...")
        logger.info(f"[INIT_DB] DB_PATH: {DB_PATH}")
        logger.info(f"[INIT_DB] __file__: {__file__}")
        logger.info(f"[INIT_DB] cwd: {os.getcwd()}")

        db_dir = str(Path(DB_PATH).parent)
        logger.info(f"[INIT_DB] Database directory: {db_dir}")
        logger.info(f"[INIT_DB] Directory exists before mkdir: {os.path.exists(db_dir)}")

        os.makedirs(db_dir, exist_ok=True)
        logger.info(f"[INIT_DB] Directory exists after mkdir: {os.path.exists(db_dir)}")
        logger.info(f"[INIT_DB] Directory writable: {os.access(db_dir, os.W_OK)}")

        conn = await aiosqlite.connect(DB_PATH)

        # Create expenses table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                description TEXT NOT NULL,
                amount REAL NOT NULL,
                category TEXT NOT NULL,
                subcategory TEXT,
                expense_date TEXT DEFAULT CURRENT_DATE,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT
            )
        """)

        # Create budgets table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS budgets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL UNIQUE,
                monthly_limit REAL NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Create recurring_expenses table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS recurring_expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                description TEXT NOT NULL,
                amount REAL NOT NULL,
                category TEXT NOT NULL,
                subcategory TEXT,
                day_of_month INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                last_generated_date TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Additive migration: add updated_at column to expenses if missing
        cursor = await conn.execute("PRAGMA table_info(expenses)")
        rows = await cursor.fetchall()
        existing_cols = {row[1] for row in rows}
        if "updated_at" not in existing_cols:
            await conn.execute("ALTER TABLE expenses ADD COLUMN updated_at TEXT")
            logger.info("Added updated_at column to expenses table")

        await conn.commit()
        await conn.close()
        logger.info(f"Database ready at: {DB_PATH}")
    except Exception as e:
        logger.error(f"Error initializing database: {e}")
        raise


# ============================================================================
# CRUD TOOLS (ASYNC)
# ============================================================================

@mcp.tool
async def add_expense(description: str, amount: float, category: str, subcategory: str = None, expense_date: str = None) -> dict:
    """Add a new expense to the tracker."""
    try:
        logger.info(f"add_expense called: desc={description}, amount={amount}, category={category}")

        is_valid, error_dict, normalized_sub = _validate_category(category, subcategory)
        if not is_valid:
            suggestion = _suggest_category(description)
            if suggestion:
                sug_cat, sug_sub = suggestion
                error_dict["message"] = f"Invalid category: {category}. Did you mean '{sug_cat}' / '{sug_sub}'?"
                error_dict["suggested_category"] = sug_cat
                error_dict["suggested_subcategory"] = sug_sub
            return error_dict

        conn = await get_connection()
        await conn.execute(
            "INSERT INTO expenses (description, amount, category, subcategory, expense_date) VALUES (?, ?, ?, ?, ?)",
            (description, amount, category, normalized_sub, expense_date)
        )
        await conn.commit()

        cursor = await conn.execute("SELECT last_insert_rowid() as id")
        row = await cursor.fetchone()
        expense_id = row[0]
        await conn.close()

        logger.info(f"Expense added successfully with ID: {expense_id}")

        return {
            "success": True,
            "message": "Expense added successfully",
            "expense_id": expense_id,
            "description": description,
            "amount": amount,
            "category": category,
            "subcategory": normalized_sub,
            "expense_date": expense_date or "today"
        }
    except Exception as e:
        logger.error(f"Error adding expense: {str(e)}")
        logger.error(traceback.format_exc())
        return {
            "success": False,
            "message": f"Error: {str(e)}",
            "error": str(e)
        }


@mcp.tool
async def get_expense(expense_id: int) -> dict:
    """Get a single expense by ID."""
    try:
        logger.info(f"get_expense called with id={expense_id}")

        conn = await get_connection()
        cursor = await conn.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,))
        row = await cursor.fetchone()
        await conn.close()

        if not row:
            return {
                "success": False,
                "message": f"Expense {expense_id} not found"
            }

        return {
            "success": True,
            "expense": dict(row) if hasattr(row, 'keys') else {
                'id': row[0], 'description': row[1], 'amount': row[2],
                'category': row[3], 'subcategory': row[4], 'expense_date': row[5],
                'created_at': row[6], 'updated_at': row[7]
            }
        }
    except Exception as e:
        logger.error(f"Error getting expense: {str(e)}")
        return {
            "success": False,
            "message": f"Error: {str(e)}",
            "error": str(e)
        }


@mcp.tool
async def update_expense(expense_id: int, description: str = None, amount: float = None, category: str = None, subcategory: str = None, expense_date: str = None) -> dict:
    """Update an existing expense."""
    try:
        logger.info(f"update_expense called with id={expense_id}")

        conn = await get_connection()

        cursor = await conn.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,))
        existing = await cursor.fetchone()
        if not existing:
            await conn.close()
            return {
                "success": False,
                "message": f"Expense {expense_id} not found"
            }

        normalized_sub = subcategory
        if category:
            is_valid, error_dict, normalized_sub = _validate_category(category, subcategory)
            if not is_valid:
                await conn.close()
                return error_dict
            if not subcategory:
                normalized_sub = None
        else:
            normalized_sub = subcategory if subcategory is not None else existing[4]

        updates = []
        params = []
        if description is not None:
            updates.append("description = ?")
            params.append(description)
        if amount is not None:
            updates.append("amount = ?")
            params.append(amount)
        if category is not None:
            updates.append("category = ?")
            params.append(category)
        if normalized_sub is not None or (category is not None and subcategory is None):
            updates.append("subcategory = ?")
            params.append(normalized_sub)
        if expense_date is not None:
            updates.append("expense_date = ?")
            params.append(expense_date)

        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append(expense_id)

        if len(updates) > 1:
            query = f"UPDATE expenses SET {', '.join(updates[:-1])} WHERE id = ?"
            await conn.execute(query, params)
            await conn.commit()

        cursor = await conn.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,))
        updated = await cursor.fetchone()
        await conn.close()

        logger.info(f"Expense {expense_id} updated successfully")

        return {
            "success": True,
            "message": "Expense updated",
            "expense": dict(updated) if hasattr(updated, 'keys') else {
                'id': updated[0], 'description': updated[1], 'amount': updated[2],
                'category': updated[3], 'subcategory': updated[4], 'expense_date': updated[5],
                'created_at': updated[6], 'updated_at': updated[7]
            }
        }
    except Exception as e:
        logger.error(f"Error updating expense: {str(e)}")
        return {
            "success": False,
            "message": f"Error: {str(e)}",
            "error": str(e)
        }


@mcp.tool
async def delete_expense(expense_id: int) -> dict:
    """Delete an expense."""
    try:
        logger.info(f"delete_expense called with id={expense_id}")

        conn = await get_connection()
        await conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
        await conn.commit()
        await conn.close()

        logger.info(f"Expense {expense_id} deleted successfully")

        return {
            "success": True,
            "message": f"Expense {expense_id} deleted",
            "expense_id": expense_id
        }
    except Exception as e:
        logger.error(f"Error deleting expense: {str(e)}")
        return {
            "success": False,
            "message": f"Error: {str(e)}",
            "error": str(e)
        }


# ============================================================================
# LIST & SUMMARIZE TOOLS (ASYNC)
# ============================================================================

@mcp.tool
async def list_expenses(category: str = None) -> list[dict]:
    """List all expenses or filter by category."""
    try:
        logger.info(f"list_expenses called with category={category}")

        conn = await get_connection()

        if category:
            query = "SELECT id, description, amount, category, subcategory, expense_date, created_at, updated_at FROM expenses WHERE category = ? ORDER BY expense_date DESC"
            cursor = await conn.execute(query, (category,))
        else:
            query = "SELECT id, description, amount, category, subcategory, expense_date, created_at, updated_at FROM expenses ORDER BY expense_date DESC"
            cursor = await conn.execute(query)

        rows = await cursor.fetchall()
        await conn.close()

        expenses = []
        for row in rows:
            if hasattr(row, 'keys'):
                expenses.append(dict(row))
            else:
                expenses.append({
                    'id': row[0], 'description': row[1], 'amount': row[2],
                    'category': row[3], 'subcategory': row[4], 'expense_date': row[5],
                    'created_at': row[6], 'updated_at': row[7]
                })

        logger.info(f"Retrieved {len(expenses)} expenses")
        return expenses
    except Exception as e:
        logger.error(f"Error listing expenses: {str(e)}")
        return []


@mcp.tool
async def summarize_expenses(category: str = None, start_date: str = None, end_date: str = None) -> dict:
    """Summarize expenses by category with optional date range and category filter."""
    try:
        logger.info(f"summarize_expenses called with category={category}, start_date={start_date}, end_date={end_date}")

        conn = await get_connection()

        where_clause, params = _build_date_category_where(category, start_date, end_date)

        if category:
            query = f"SELECT description, amount, expense_date, category, subcategory FROM expenses {where_clause} ORDER BY expense_date DESC"
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
        else:
            query = f"SELECT category, SUM(amount) as total, COUNT(*) as count FROM expenses {where_clause} GROUP BY category ORDER BY total DESC"
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()

        await conn.close()

        summary = {
            "filters": {
                "category": category or "All",
                "start_date": start_date or "N/A",
                "end_date": end_date or "N/A"
            }
        }

        if category:
            total_amount = 0
            expenses_list = []
            for row in rows:
                amount = row[1] if not hasattr(row, 'keys') else row['amount']
                total_amount += amount
                expenses_list.append({
                    "description": row[0] if not hasattr(row, 'keys') else row['description'],
                    "amount": amount,
                    "subcategory": row[4] if not hasattr(row, 'keys') else row['subcategory'],
                    "date": row[2] if not hasattr(row, 'keys') else row['expense_date']
                })

            summary["category"] = category
            summary["expenses"] = expenses_list
            summary["total"] = total_amount
            summary["count"] = len(expenses_list)
            summary["average"] = round(total_amount / len(expenses_list), 2) if len(expenses_list) > 0 else 0
        else:
            summary["categories"] = {}
            total_amount = 0
            total_count = 0
            for row in rows:
                cat = row[0] if not hasattr(row, 'keys') else row['category']
                total = row[1] if not hasattr(row, 'keys') else row['total']
                count = row[2] if not hasattr(row, 'keys') else row['count']
                summary["categories"][cat] = {
                    "total": total,
                    "count": count,
                    "average": round(total / count, 2) if count > 0 else 0
                }
                total_amount += total
                total_count += count

            summary["overall"] = {
                "total_amount": total_amount,
                "total_expenses": total_count
            }

        return summary
    except Exception as e:
        logger.error(f"Error summarizing expenses: {str(e)}")
        return {
            "success": False,
            "message": f"Error: {str(e)}",
            "error": str(e)
        }


# ============================================================================
# BUDGET TOOLS (ASYNC)
# ============================================================================

@mcp.tool
async def set_budget(category: str, monthly_limit: float) -> dict:
    """Set or update a budget for a category."""
    try:
        logger.info(f"set_budget called: category={category}, limit={monthly_limit}")

        valid_categories = {"Overall"} | {cat["name"] for cat in CATEGORIES}
        if category not in valid_categories:
            return {
                "success": False,
                "message": f"Invalid category: {category}",
                "available_categories": list(valid_categories)
            }

        conn = await get_connection()
        await conn.execute(
            """INSERT INTO budgets (category, monthly_limit, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(category) DO UPDATE SET
                   monthly_limit = excluded.monthly_limit,
                   updated_at = CURRENT_TIMESTAMP""",
            (category, monthly_limit)
        )
        await conn.commit()
        await conn.close()

        logger.info(f"Budget set for {category}: ₹{monthly_limit}")

        return {
            "success": True,
            "message": f"Budget set for {category}",
            "category": category,
            "monthly_limit": monthly_limit
        }
    except Exception as e:
        logger.error(f"Error setting budget: {str(e)}")
        return {
            "success": False,
            "message": f"Error: {str(e)}",
            "error": str(e)
        }


@mcp.tool
async def check_budget_status(category: str = None, month: str = None) -> dict:
    """Check spending against budget for a category/month."""
    try:
        logger.info(f"check_budget_status called: category={category}, month={month}")

        category = category or "Overall"
        start_date, end_date = get_month_bounds(month)

        conn = await get_connection()

        cursor = await conn.execute("SELECT monthly_limit FROM budgets WHERE category = ?", (category,))
        budget_row = await cursor.fetchone()

        if not budget_row:
            await conn.close()
            return {
                "success": False,
                "message": f"No budget set for {category}"
            }

        limit = budget_row[0] if isinstance(budget_row, tuple) else budget_row['monthly_limit']

        where_clause, params = _build_date_category_where(
            category if category != "Overall" else None,
            start_date,
            end_date
        )
        query = f"SELECT COALESCE(SUM(amount), 0) as total FROM expenses {where_clause}"
        cursor = await conn.execute(query, params)
        spent_row = await cursor.fetchone()
        spent = spent_row[0] if isinstance(spent_row, tuple) else spent_row['total']
        await conn.close()

        remaining = limit - spent
        percent_used = round(spent / limit * 100, 1) if limit > 0 else 0

        if spent > limit:
            status = "over_budget"
            status_msg = f"Over budget by ₹{round(abs(remaining), 2)}"
        elif percent_used >= 90:
            status = "near_limit"
            status_msg = f"Nearing limit: {percent_used}% used"
        else:
            status = "ok"
            status_msg = f"Within budget: {percent_used}% used"

        return {
            "success": True,
            "category": category,
            "month": month or "current",
            "budget": limit,
            "spent": round(spent, 2),
            "remaining": round(remaining, 2),
            "percent_used": percent_used,
            "status": status,
            "message": status_msg
        }
    except Exception as e:
        logger.error(f"Error checking budget: {str(e)}")
        return {
            "success": False,
            "message": f"Error: {str(e)}",
            "error": str(e)
        }


# ============================================================================
# RECURRING EXPENSE TOOLS (ASYNC)
# ============================================================================

@mcp.tool
async def add_recurring_expense(description: str, amount: float, category: str, subcategory: str = None, day_of_month: int = 1) -> dict:
    """Add a recurring monthly expense."""
    try:
        logger.info(f"add_recurring_expense: desc={description}, amount={amount}, category={category}, day={day_of_month}")

        if not (1 <= day_of_month <= 28):
            return {
                "success": False,
                "message": "day_of_month must be between 1 and 28"
            }

        is_valid, error_dict, normalized_sub = _validate_category(category, subcategory)
        if not is_valid:
            return error_dict

        conn = await get_connection()
        await conn.execute(
            """INSERT INTO recurring_expenses
               (description, amount, category, subcategory, day_of_month, active, last_generated_date)
               VALUES (?, ?, ?, ?, ?, 1, NULL)""",
            (description, amount, category, normalized_sub, day_of_month)
        )
        await conn.commit()

        cursor = await conn.execute("SELECT last_insert_rowid() as id")
        row = await cursor.fetchone()
        recurring_id = row[0]
        await conn.close()

        logger.info(f"Recurring expense added with ID: {recurring_id}")

        return {
            "success": True,
            "message": "Recurring expense added",
            "recurring_id": recurring_id,
            "description": description,
            "amount": amount,
            "category": category,
            "day_of_month": day_of_month
        }
    except Exception as e:
        logger.error(f"Error adding recurring expense: {str(e)}")
        return {
            "success": False,
            "message": f"Error: {str(e)}",
            "error": str(e)
        }


@mcp.tool
async def list_recurring_expenses(active_only: bool = True) -> list[dict]:
    """List all recurring expenses."""
    try:
        logger.info(f"list_recurring_expenses called, active_only={active_only}")

        conn = await get_connection()

        if active_only:
            cursor = await conn.execute("SELECT * FROM recurring_expenses WHERE active = 1 ORDER BY day_of_month")
        else:
            cursor = await conn.execute("SELECT * FROM recurring_expenses ORDER BY day_of_month")

        rows = await cursor.fetchall()
        await conn.close()

        result = []
        for row in rows:
            if hasattr(row, 'keys'):
                result.append(dict(row))
            else:
                result.append({
                    'id': row[0], 'description': row[1], 'amount': row[2],
                    'category': row[3], 'subcategory': row[4], 'day_of_month': row[5],
                    'active': row[6], 'last_generated_date': row[7], 'created_at': row[8]
                })

        return result
    except Exception as e:
        logger.error(f"Error listing recurring expenses: {str(e)}")
        return []


@mcp.tool
async def generate_due_recurring_expenses() -> dict:
    """Generate expenses for recurring items that are due."""
    try:
        logger.info("generate_due_recurring_expenses called")

        today = date.today()
        conn = await get_connection()

        cursor = await conn.execute("SELECT * FROM recurring_expenses WHERE active = 1")
        recurring_rows = await cursor.fetchall()

        generated = []

        for rec in recurring_rows:
            recurring_id = rec[0] if isinstance(rec, tuple) else rec['id']
            last_generated = rec[7] if isinstance(rec, tuple) else rec['last_generated_date']

            if last_generated:
                last_date = date.fromisoformat(last_generated)
                current_month = date(last_date.year, last_date.month, 1) + timedelta(days=32)
                current_month = date(current_month.year, current_month.month, 1)
            else:
                current_month = today.replace(day=1)

            day_of_month = rec[5] if isinstance(rec, tuple) else rec['day_of_month']
            due_date = current_month.replace(day=min(day_of_month, 28))

            while due_date <= today:
                await conn.execute(
                    """INSERT INTO expenses
                       (description, amount, category, subcategory, expense_date)
                       VALUES (?, ?, ?, ?, ?)""",
                    (rec[1], rec[2], rec[3], rec[4], due_date.isoformat()) if isinstance(rec, tuple)
                    else (rec['description'], rec['amount'], rec['category'], rec['subcategory'], due_date.isoformat())
                )
                await conn.commit()

                cursor = await conn.execute("SELECT last_insert_rowid() as id")
                id_row = await cursor.fetchone()
                expense_id = id_row[0]

                generated.append({
                    "recurring_id": recurring_id,
                    "expense_id": expense_id,
                    "expense_date": due_date.isoformat()
                })

                next_month = due_date + timedelta(days=32)
                next_month = next_month.replace(day=1)
                due_date = next_month.replace(day=min(day_of_month, 28))

            if generated:
                last_gen_date = generated[-1]["expense_date"]
                await conn.execute(
                    "UPDATE recurring_expenses SET last_generated_date = ? WHERE id = ?",
                    (last_gen_date, recurring_id)
                )
                await conn.commit()

        await conn.close()

        logger.info(f"Generated {len(generated)} recurring expenses")

        return {
            "success": True,
            "generated_count": len(generated),
            "generated": generated
        }
    except Exception as e:
        logger.error(f"Error generating recurring expenses: {str(e)}")
        return {
            "success": False,
            "message": f"Error: {str(e)}",
            "error": str(e)
        }


# ============================================================================
# EXPORT & REPORTING TOOLS (ASYNC)
# ============================================================================

@mcp.tool
async def export_expenses_csv(start_date: str = None, end_date: str = None, category: str = None) -> dict:
    """Export expenses to a CSV file."""
    try:
        logger.info(f"export_expenses_csv called with filters: start={start_date}, end={end_date}, category={category}")

        EXPORTS_DIR.mkdir(exist_ok=True)

        conn = await get_connection()

        where_clause, params = _build_date_category_where(category, start_date, end_date)
        query = f"""SELECT id, description, amount, category, subcategory, expense_date, created_at
                    FROM expenses
                    {where_clause}
                    ORDER BY expense_date DESC"""
        cursor = await conn.execute(query, params)
        rows = await cursor.fetchall()
        await conn.close()

        now = date.today().isoformat().replace('-', '') + "_" + str(int(date.today().isoformat().split('-')[2]))
        filename = f"expenses_export_{now}.csv"
        filepath = EXPORTS_DIR / filename

        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['id', 'description', 'amount', 'category', 'subcategory', 'expense_date', 'created_at'])
            for row in rows:
                writer.writerow(row)

        logger.info(f"CSV exported to {filepath} ({len(rows)} rows)")

        return {
            "success": True,
            "file_path": str(filepath),
            "row_count": len(rows),
            "filters": {
                "category": category,
                "start_date": start_date,
                "end_date": end_date
            }
        }
    except Exception as e:
        logger.error(f"Error exporting CSV: {str(e)}")
        return {
            "success": False,
            "message": f"Error: {str(e)}",
            "error": str(e)
        }


@mcp.tool
async def monthly_report(months: int = 6) -> dict:
    """Generate a monthly expense report."""
    try:
        logger.info(f"monthly_report called with months={months}")

        end_date = date.today()
        start_date = end_date - timedelta(days=30 * months)

        conn = await get_connection()

        query = """SELECT strftime('%Y-%m', expense_date) as month,
                          SUM(amount) as total, COUNT(*) as count
                   FROM expenses
                   WHERE expense_date >= ? AND expense_date <= ?
                   GROUP BY month
                   ORDER BY month"""
        cursor = await conn.execute(query, (start_date.isoformat(), end_date.isoformat()))
        trend_rows = await cursor.fetchall()
        monthly_trend = []
        for row in trend_rows:
            monthly_trend.append({
                "month": row[0] if isinstance(row, tuple) else row['month'],
                "total": row[1] if isinstance(row, tuple) else row['total'],
                "count": row[2] if isinstance(row, tuple) else row['count']
            })

        query = """SELECT id, description, amount, category, subcategory, expense_date
                   FROM expenses
                   WHERE expense_date >= ? AND expense_date <= ?
                   ORDER BY amount DESC
                   LIMIT 5"""
        cursor = await conn.execute(query, (start_date.isoformat(), end_date.isoformat()))
        top_rows = await cursor.fetchall()
        top_expenses = []
        for row in top_rows:
            if hasattr(row, 'keys'):
                top_expenses.append(dict(row))
            else:
                top_expenses.append({
                    'id': row[0], 'description': row[1], 'amount': row[2],
                    'category': row[3], 'subcategory': row[4], 'expense_date': row[5]
                })

        await conn.close()

        total_spent = sum(t["total"] for t in monthly_trend)
        avg_monthly = round(total_spent / len(monthly_trend), 2) if monthly_trend else 0

        logger.info(f"Monthly report generated for {len(monthly_trend)} months")

        return {
            "success": True,
            "period": {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "months": len(monthly_trend)
            },
            "monthly_trend": monthly_trend,
            "top_expenses": top_expenses,
            "average_monthly_spend": avg_monthly
        }
    except Exception as e:
        logger.error(f"Error generating monthly report: {str(e)}")
        return {
            "success": False,
            "message": f"Error: {str(e)}",
            "error": str(e)
        }


# ============================================================================
# RESOURCES (ASYNC)
# ============================================================================

@mcp.resource("categories://list")
def resource_categories() -> str:
    """Resource: List of all available expense categories and subcategories."""
    try:
        with open(CATEGORIES_FILE, 'r') as f:
            return f.read()
    except Exception as e:
        logger.error(f"Error reading categories resource: {e}")
        return json.dumps({"error": str(e)})


@mcp.resource("expenses://recent")
async def resource_recent_expenses() -> str:
    """Resource: Recent expenses (last 10)."""
    try:
        conn = await get_connection()
        cursor = await conn.execute(
            "SELECT * FROM expenses ORDER BY expense_date DESC, id DESC LIMIT ?",
            (RECENT_EXPENSES_LIMIT,)
        )
        rows = await cursor.fetchall()
        await conn.close()

        expenses = []
        for row in rows:
            if hasattr(row, 'keys'):
                expenses.append(dict(row))
            else:
                expenses.append({
                    'id': row[0], 'description': row[1], 'amount': row[2],
                    'category': row[3], 'subcategory': row[4], 'expense_date': row[5],
                    'created_at': row[6], 'updated_at': row[7]
                })

        return json.dumps({"limit": RECENT_EXPENSES_LIMIT, "expenses": expenses})
    except Exception as e:
        logger.error(f"Error reading recent expenses resource: {e}")
        return json.dumps({"error": str(e)})


@mcp.resource("expenses://summary")
async def resource_summary() -> str:
    """Resource: Current month expense summary by category."""
    try:
        start_date, end_date = get_month_bounds(None)

        conn = await get_connection()

        query = f"""SELECT category, SUM(amount) as total, COUNT(*) as count
                    FROM expenses
                    WHERE expense_date BETWEEN ? AND ?
                    GROUP BY category
                    ORDER BY total DESC"""
        cursor = await conn.execute(query, (start_date, end_date))
        rows = await cursor.fetchall()
        await conn.close()

        summary = {
            "period": {
                "start_date": start_date,
                "end_date": end_date
            },
            "categories": {},
            "overall": {
                "total_amount": 0,
                "total_expenses": 0
            }
        }

        for row in rows:
            cat = row[0] if isinstance(row, tuple) else row['category']
            total = row[1] if isinstance(row, tuple) else row['total']
            count = row[2] if isinstance(row, tuple) else row['count']
            summary["categories"][cat] = {
                "total": total,
                "count": count,
                "average": round(total / count, 2) if count > 0 else 0
            }
            summary["overall"]["total_amount"] += total
            summary["overall"]["total_expenses"] += count

        return json.dumps(summary)
    except Exception as e:
        logger.error(f"Error reading summary resource: {e}")
        return json.dumps({"error": str(e)})


# ============================================================================
# PROMPTS
# ============================================================================

@mcp.prompt("weekly_expense_report", description="Get a summary of expenses from the past 7 days")
def prompt_weekly_report() -> str:
    """Prompt template for weekly expense report."""
    today = date.today()
    start = today - timedelta(days=7)

    return f"""Provide a weekly expense report for {start.isoformat()} to {today.isoformat()}.

Call summarize_expenses(start_date="{start.isoformat()}", end_date="{today.isoformat()}") to get category breakdowns.

Then produce a brief 3-4 line summary: total spend, top 3 categories by amount, any single expense >₹2000, and a brief comparison note."""


@mcp.prompt("categorize_expense", description="Categorize a raw expense description and add it to the tracker")
def prompt_categorize(description: str, amount: float = None) -> str:
    """Prompt template for categorizing an expense."""
    return f"""Categorize this expense and add it to the tracker:
Description: "{description}"
Amount: {amount if amount else "(unknown)"}

Read the categories://list resource to see available categories and subcategories.
Then call add_expense(description="{description}", amount={amount}, category="<chosen>", subcategory="<chosen>").

If add_expense returns suggested_category/suggested_subcategory in the response (fuzzy match), retry with those suggestions."""


if __name__ == "__main__":
    asyncio.run(init_db())
    mcp.run()
