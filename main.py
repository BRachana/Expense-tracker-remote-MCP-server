import sqlite3
import os
import sys
import json
import csv
import logging
import traceback
import calendar
from pathlib import Path
from datetime import date, timedelta
from fastmcp import FastMCP

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

# Store database in the same folder as main.py
SCRIPT_DIR = Path(__file__).parent
DB_PATH = SCRIPT_DIR / "expenses.db"
CATEGORIES_FILE = SCRIPT_DIR / "categories.json"
EXPORTS_DIR = SCRIPT_DIR / "exports"
RECENT_EXPENSES_LIMIT = 10

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
# SHARED HELPERS
# ============================================================================

def get_connection():
    """Get a database connection with row factory and WAL mode."""
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _validate_category(category: str, subcategory: str = None) -> tuple[bool, dict]:
    """Validate category and subcategory against loaded categories.
    Returns (is_valid, error_dict_if_invalid)"""
    valid_categories = {cat["name"] for cat in CATEGORIES}

    if category not in valid_categories:
        return False, {
            "success": False,
            "message": f"Invalid category: {category}",
            "available_categories": list(valid_categories)
        }

    if subcategory:
        cat_obj = next((cat for cat in CATEGORIES if cat["name"] == category), None)
        if cat_obj and subcategory not in cat_obj["subcategories"]:
            return False, {
                "success": False,
                "message": f"Invalid subcategory: {subcategory} for category: {category}",
                "available_subcategories": cat_obj["subcategories"]
            }

    return True, {}


def _suggest_category(description: str) -> tuple[str, str] | None:
    """Fuzzy-suggest a category from description. Returns (category, subcategory) or None."""
    desc_lower = description.lower()

    # Sort by keyword length descending (longest first)
    sorted_keywords = sorted(KEYWORD_MAPS.keys(), key=len, reverse=True)

    for keyword in sorted_keywords:
        if keyword in desc_lower:
            return KEYWORD_MAPS[keyword]

    return None


def _build_date_category_where(category: str = None, start_date: str = None, end_date: str = None) -> tuple[str, list]:
    """Build WHERE clause and params for date/category filtering.
    Returns (where_clause_str, params_list)"""
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
    """Get first and last day of a month (YYYY-MM-DD).
    If month is None, returns bounds for current month.
    month format: 'YYYY-MM' or None"""
    if month:
        year, mon = map(int, month.split('-'))
    else:
        today = date.today()
        year, mon = today.year, today.month

    first_day = date(year, mon, 1)
    last_day_num = calendar.monthrange(year, mon)[1]
    last_day = date(year, mon, last_day_num)

    return first_day.isoformat(), last_day.isoformat()


def init_db():
    """Initialize the SQLite database with all tables (non-destructive)."""
    try:
        logger.info(f"Initializing database at: {DB_PATH}")

        conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
        cursor = conn.cursor()

        # Create expenses table
        cursor.execute("""
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
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS budgets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL UNIQUE,
                monthly_limit REAL NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Create recurring_expenses table
        cursor.execute("""
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
        cursor.execute("PRAGMA table_info(expenses)")
        existing_cols = {row[1] for row in cursor.fetchall()}
        if "updated_at" not in existing_cols:
            cursor.execute("ALTER TABLE expenses ADD COLUMN updated_at TEXT")
            logger.info("Added updated_at column to expenses table")

        conn.commit()
        conn.close()
        logger.info(f"Database ready at: {DB_PATH}")
    except Exception as e:
        logger.error(f"Error initializing database: {e}")
        raise


# ============================================================================
# CRUD TOOLS
# ============================================================================

@mcp.tool
def add_expense(description: str, amount: float, category: str, subcategory: str = None, expense_date: str = None) -> dict:
    """Add a new expense to the tracker.

    Args:
        description: Description of the expense
        amount: Amount spent
        category: Category of the expense (from available categories)
        subcategory: Subcategory within the category (optional)
        expense_date: Date of the expense in YYYY-MM-DD format (default: today)

    Returns:
        Success message with the expense ID
    """
    try:
        logger.info(f"add_expense called: desc={description}, amount={amount}, category={category}, subcategory={subcategory}")

        # Validate category
        is_valid, error_dict = _validate_category(category, subcategory)
        if not is_valid:
            # Try to suggest a category
            suggestion = _suggest_category(description)
            if suggestion:
                sug_cat, sug_sub = suggestion
                error_dict["message"] = f"Invalid category: {category}. Did you mean '{sug_cat}' / '{sug_sub}'?"
                error_dict["suggested_category"] = sug_cat
                error_dict["suggested_subcategory"] = sug_sub
            return error_dict

        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute(
            "INSERT INTO expenses (description, amount, category, subcategory, expense_date) VALUES (?, ?, ?, ?, ?)",
            (description, amount, category, subcategory, expense_date)
        )
        conn.commit()
        expense_id = cursor.lastrowid
        conn.close()

        logger.info(f"Expense added successfully with ID: {expense_id}")

        return {
            "success": True,
            "message": "Expense added successfully",
            "expense_id": expense_id,
            "description": description,
            "amount": amount,
            "category": category,
            "subcategory": subcategory,
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
def get_expense(expense_id: int) -> dict:
    """Get a single expense by ID.

    Args:
        expense_id: ID of the expense

    Returns:
        Expense details or not-found message
    """
    try:
        logger.info(f"get_expense called with id={expense_id}")

        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,))
        row = cursor.fetchone()
        conn.close()

        if not row:
            return {
                "success": False,
                "message": f"Expense {expense_id} not found"
            }

        return {
            "success": True,
            "expense": dict(row)
        }
    except Exception as e:
        logger.error(f"Error getting expense: {str(e)}")
        logger.error(traceback.format_exc())
        return {
            "success": False,
            "message": f"Error: {str(e)}",
            "error": str(e)
        }


@mcp.tool
def update_expense(expense_id: int, description: str = None, amount: float = None, category: str = None, subcategory: str = None, expense_date: str = None) -> dict:
    """Update an existing expense.

    Args:
        expense_id: ID of the expense to update
        description: New description (optional)
        amount: New amount (optional)
        category: New category (optional)
        subcategory: New subcategory (optional)
        expense_date: New date (optional)

    Returns:
        Updated expense or error
    """
    try:
        logger.info(f"update_expense called with id={expense_id}")

        conn = get_connection()
        cursor = conn.cursor()

        # Check if expense exists
        cursor.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,))
        existing = cursor.fetchone()
        if not existing:
            conn.close()
            return {
                "success": False,
                "message": f"Expense {expense_id} not found"
            }

        # Validate category if provided
        if category:
            is_valid, error_dict = _validate_category(category, subcategory)
            if not is_valid:
                conn.close()
                return error_dict
            # If category changes but subcategory not explicitly given, clear subcategory
            if not subcategory:
                subcategory = None
        else:
            # Category not changing, so keep existing subcategory unless explicitly cleared
            subcategory = subcategory if subcategory is not None else existing["subcategory"]

        # Build dynamic SET clause
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
        if subcategory is not None:
            updates.append("subcategory = ?")
            params.append(subcategory)
        if expense_date is not None:
            updates.append("expense_date = ?")
            params.append(expense_date)

        # Always update updated_at
        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append(expense_id)

        if updates:
            query = f"UPDATE expenses SET {', '.join(updates)} WHERE id = ?"
            cursor.execute(query, params)
            conn.commit()

        # Fetch updated row
        cursor.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,))
        updated = dict(cursor.fetchone())
        conn.close()

        logger.info(f"Expense {expense_id} updated successfully")

        return {
            "success": True,
            "message": "Expense updated",
            "expense": updated
        }
    except Exception as e:
        logger.error(f"Error updating expense: {str(e)}")
        logger.error(traceback.format_exc())
        return {
            "success": False,
            "message": f"Error: {str(e)}",
            "error": str(e)
        }


@mcp.tool
def delete_expense(expense_id: int) -> dict:
    """Delete an expense.

    Args:
        expense_id: ID of the expense to delete

    Returns:
        Confirmation message
    """
    try:
        logger.info(f"delete_expense called with id={expense_id}")

        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
        conn.commit()
        rowcount = cursor.rowcount
        conn.close()

        if rowcount == 0:
            return {
                "success": False,
                "message": f"Expense {expense_id} not found"
            }

        logger.info(f"Expense {expense_id} deleted successfully")

        return {
            "success": True,
            "message": f"Expense {expense_id} deleted",
            "expense_id": expense_id
        }
    except Exception as e:
        logger.error(f"Error deleting expense: {str(e)}")
        logger.error(traceback.format_exc())
        return {
            "success": False,
            "message": f"Error: {str(e)}",
            "error": str(e)
        }


# ============================================================================
# LIST & SUMMARIZE TOOLS
# ============================================================================

@mcp.tool
def list_expenses(category: str = None) -> list[dict]:
    """List all expenses or filter by category.

    Args:
        category: Optional category to filter expenses

    Returns:
        List of expenses
    """
    try:
        logger.info(f"list_expenses called with category={category}")

        conn = get_connection()
        cursor = conn.cursor()

        if category:
            cursor.execute(
                "SELECT id, description, amount, category, subcategory, expense_date, created_at, updated_at FROM expenses WHERE category = ? ORDER BY expense_date DESC",
                (category,)
            )
        else:
            cursor.execute(
                "SELECT id, description, amount, category, subcategory, expense_date, created_at, updated_at FROM expenses ORDER BY expense_date DESC"
            )

        rows = cursor.fetchall()
        conn.close()

        expenses = [dict(row) for row in rows]
        logger.info(f"Retrieved {len(expenses)} expenses")
        return expenses

    except Exception as e:
        logger.error(f"Error listing expenses: {str(e)}")
        logger.error(traceback.format_exc())
        return []


@mcp.tool
def summarize_expenses(category: str = None, start_date: str = None, end_date: str = None) -> dict:
    """Summarize expenses by category with optional date range and category filter.

    Args:
        category: Optional specific category to summarize (e.g., 'Transport', 'Groceries')
        start_date: Start date in YYYY-MM-DD format (optional)
        end_date: End date in YYYY-MM-DD format (optional)

    Returns:
        Dictionary with category totals and overall summary
    """
    try:
        logger.info(f"summarize_expenses called with category={category}, start_date={start_date}, end_date={end_date}")

        conn = get_connection()
        cursor = conn.cursor()

        where_clause, params = _build_date_category_where(category, start_date, end_date)

        if category:
            query = f"""
                SELECT description, amount, expense_date, category, subcategory
                FROM expenses
                {where_clause}
                ORDER BY expense_date DESC
            """
            cursor.execute(query, params)
            rows = cursor.fetchall()
        else:
            query = f"""
                SELECT category, SUM(amount) as total, COUNT(*) as count
                FROM expenses
                {where_clause}
                GROUP BY category
                ORDER BY total DESC
            """
            cursor.execute(query, params)
            rows = cursor.fetchall()

        conn.close()

        # Build summary
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
                total_amount += row["amount"]
                expenses_list.append({
                    "description": row["description"],
                    "amount": row["amount"],
                    "subcategory": row["subcategory"],
                    "date": row["expense_date"]
                })

            summary["category"] = category
            summary["expenses"] = expenses_list
            summary["total"] = total_amount
            summary["count"] = len(expenses_list)
            summary["average"] = round(total_amount / len(expenses_list), 2) if len(expenses_list) > 0 else 0
            logger.info(f"Summary for {category}: {len(expenses_list)} expenses, ₹{total_amount}")
        else:
            summary["categories"] = {}
            total_amount = 0
            total_count = 0
            for row in rows:
                summary["categories"][row["category"]] = {
                    "total": row["total"],
                    "count": row["count"],
                    "average": round(row["total"] / row["count"], 2) if row["count"] > 0 else 0
                }
                total_amount += row["total"]
                total_count += row["count"]

            summary["overall"] = {
                "total_amount": total_amount,
                "total_expenses": total_count
            }
            logger.info(f"Summary generated: {total_count} expenses, ₹{total_amount}")

        return summary

    except Exception as e:
        logger.error(f"Error summarizing expenses: {str(e)}")
        logger.error(traceback.format_exc())
        return {
            "success": False,
            "message": f"Error: {str(e)}",
            "error": str(e)
        }


# ============================================================================
# BUDGET TOOLS
# ============================================================================

@mcp.tool
def set_budget(category: str, monthly_limit: float) -> dict:
    """Set or update a budget for a category.

    Args:
        category: Category name or "Overall" for total budget
        monthly_limit: Monthly budget limit in rupees

    Returns:
        Confirmation with budget details
    """
    try:
        logger.info(f"set_budget called: category={category}, limit={monthly_limit}")

        valid_categories = {"Overall"} | {cat["name"] for cat in CATEGORIES}
        if category not in valid_categories:
            return {
                "success": False,
                "message": f"Invalid category: {category}",
                "available_categories": list(valid_categories)
            }

        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """INSERT INTO budgets (category, monthly_limit, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(category) DO UPDATE SET
                   monthly_limit = excluded.monthly_limit,
                   updated_at = CURRENT_TIMESTAMP""",
            (category, monthly_limit)
        )
        conn.commit()
        conn.close()

        logger.info(f"Budget set for {category}: ₹{monthly_limit}")

        return {
            "success": True,
            "message": f"Budget set for {category}",
            "category": category,
            "monthly_limit": monthly_limit
        }
    except Exception as e:
        logger.error(f"Error setting budget: {str(e)}")
        logger.error(traceback.format_exc())
        return {
            "success": False,
            "message": f"Error: {str(e)}",
            "error": str(e)
        }


@mcp.tool
def check_budget_status(category: str = None, month: str = None) -> dict:
    """Check spending against budget for a category/month.

    Args:
        category: Category to check (optional, defaults to "Overall")
        month: Month in YYYY-MM format (optional, defaults to current month)

    Returns:
        Budget status with spending info
    """
    try:
        logger.info(f"check_budget_status called: category={category}, month={month}")

        category = category or "Overall"
        start_date, end_date = get_month_bounds(month)

        conn = get_connection()
        cursor = conn.cursor()

        # Get budget
        cursor.execute("SELECT monthly_limit FROM budgets WHERE category = ?", (category,))
        budget_row = cursor.fetchone()

        if not budget_row:
            conn.close()
            return {
                "success": False,
                "message": f"No budget set for {category}"
            }

        limit = budget_row["monthly_limit"]

        # Calculate spending
        where_clause, params = _build_date_category_where(
            category if category != "Overall" else None,
            start_date,
            end_date
        )
        query = f"SELECT COALESCE(SUM(amount), 0) as total FROM expenses {where_clause}"
        cursor.execute(query, params)
        spent = cursor.fetchone()["total"]
        conn.close()

        # Calculate status
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

        logger.info(f"Budget status for {category} ({month or 'current month'}): {status}")

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
        logger.error(traceback.format_exc())
        return {
            "success": False,
            "message": f"Error: {str(e)}",
            "error": str(e)
        }


# ============================================================================
# RECURRING EXPENSE TOOLS
# ============================================================================

@mcp.tool
def add_recurring_expense(description: str, amount: float, category: str, subcategory: str = None, day_of_month: int = 1) -> dict:
    """Add a recurring monthly expense.

    Args:
        description: Description of the expense
        amount: Monthly amount
        category: Category of the expense
        subcategory: Subcategory (optional)
        day_of_month: Day of month to recur (1-28, default 1)

    Returns:
        Confirmation with recurring expense ID
    """
    try:
        logger.info(f"add_recurring_expense: desc={description}, amount={amount}, category={category}, day={day_of_month}")

        # Validate
        if not (1 <= day_of_month <= 28):
            return {
                "success": False,
                "message": "day_of_month must be between 1 and 28"
            }

        is_valid, error_dict = _validate_category(category, subcategory)
        if not is_valid:
            return error_dict

        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """INSERT INTO recurring_expenses
               (description, amount, category, subcategory, day_of_month, active, last_generated_date)
               VALUES (?, ?, ?, ?, ?, 1, NULL)""",
            (description, amount, category, subcategory, day_of_month)
        )
        conn.commit()
        recurring_id = cursor.lastrowid
        conn.close()

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
        logger.error(traceback.format_exc())
        return {
            "success": False,
            "message": f"Error: {str(e)}",
            "error": str(e)
        }


@mcp.tool
def list_recurring_expenses(active_only: bool = True) -> list[dict]:
    """List all recurring expenses.

    Args:
        active_only: Show only active recurring expenses (default True)

    Returns:
        List of recurring expenses
    """
    try:
        logger.info(f"list_recurring_expenses called, active_only={active_only}")

        conn = get_connection()
        cursor = conn.cursor()

        if active_only:
            cursor.execute("SELECT * FROM recurring_expenses WHERE active = 1 ORDER BY day_of_month")
        else:
            cursor.execute("SELECT * FROM recurring_expenses ORDER BY day_of_month")

        rows = cursor.fetchall()
        conn.close()

        return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"Error listing recurring expenses: {str(e)}")
        logger.error(traceback.format_exc())
        return []


@mcp.tool
def generate_due_recurring_expenses() -> dict:
    """Generate expenses for recurring items that are due.

    Processes all active recurring expenses and creates new expense rows
    for any that are due since their last_generated_date.

    Returns:
        Count and list of generated expenses
    """
    try:
        logger.info("generate_due_recurring_expenses called")

        today = date.today()
        conn = get_connection()
        cursor = conn.cursor()

        # Get all active recurring expenses
        cursor.execute("SELECT * FROM recurring_expenses WHERE active = 1")
        recurring_rows = cursor.fetchall()

        generated = []

        for rec in recurring_rows:
            recurring_id = rec["id"]
            last_generated = rec["last_generated_date"]

            # Determine start month for generation
            if last_generated:
                last_date = date.fromisoformat(last_generated)
                current_month = date(last_date.year, last_date.month, 1) + timedelta(days=32)
                current_month = date(current_month.year, current_month.month, 1)
            else:
                # Never generated; use creation month
                current_month = today.replace(day=1)

            # Generate for each month up to today
            due_date = current_month.replace(day=min(rec["day_of_month"], 28))

            while due_date <= today:
                # Insert expense
                cursor.execute(
                    """INSERT INTO expenses
                       (description, amount, category, subcategory, expense_date)
                       VALUES (?, ?, ?, ?, ?)""",
                    (rec["description"], rec["amount"], rec["category"], rec["subcategory"], due_date.isoformat())
                )
                expense_id = cursor.lastrowid

                generated.append({
                    "recurring_id": recurring_id,
                    "expense_id": expense_id,
                    "expense_date": due_date.isoformat()
                })

                # Advance to next month
                next_month = due_date + timedelta(days=32)
                next_month = next_month.replace(day=1)
                due_date = next_month.replace(day=min(rec["day_of_month"], 28))

            # Update last_generated_date
            if generated:
                last_gen_date = generated[-1]["expense_date"]
                cursor.execute(
                    "UPDATE recurring_expenses SET last_generated_date = ? WHERE id = ?",
                    (last_gen_date, recurring_id)
                )

        conn.commit()
        conn.close()

        logger.info(f"Generated {len(generated)} recurring expenses")

        return {
            "success": True,
            "generated_count": len(generated),
            "generated": generated
        }
    except Exception as e:
        logger.error(f"Error generating recurring expenses: {str(e)}")
        logger.error(traceback.format_exc())
        return {
            "success": False,
            "message": f"Error: {str(e)}",
            "error": str(e)
        }


# ============================================================================
# EXPORT & REPORTING TOOLS
# ============================================================================

@mcp.tool
def export_expenses_csv(start_date: str = None, end_date: str = None, category: str = None) -> dict:
    """Export expenses to a CSV file.

    Args:
        start_date: Start date in YYYY-MM-DD format (optional)
        end_date: End date in YYYY-MM-DD format (optional)
        category: Filter by category (optional)

    Returns:
        File path and row count
    """
    try:
        logger.info(f"export_expenses_csv called with filters: start={start_date}, end={end_date}, category={category}")

        EXPORTS_DIR.mkdir(exist_ok=True)

        conn = get_connection()
        cursor = conn.cursor()

        where_clause, params = _build_date_category_where(category, start_date, end_date)
        query = f"""SELECT id, description, amount, category, subcategory, expense_date, created_at
                    FROM expenses
                    {where_clause}
                    ORDER BY expense_date DESC"""
        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()

        # Create filename with timestamp
        now = date.today().isoformat().replace('-', '') + "_" + str(int(date.today().isoformat().split('-')[2]))
        filename = f"expenses_export_{now}.csv"
        filepath = EXPORTS_DIR / filename

        # Write CSV
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
        logger.error(traceback.format_exc())
        return {
            "success": False,
            "message": f"Error: {str(e)}",
            "error": str(e)
        }


@mcp.tool
def monthly_report(months: int = 6) -> dict:
    """Generate a monthly expense report.

    Args:
        months: Number of months to include (default 6)

    Returns:
        Monthly trend, top expenses, and summary
    """
    try:
        logger.info(f"monthly_report called with months={months}")

        # Calculate date range
        end_date = date.today()
        start_date = end_date - timedelta(days=30 * months)

        conn = get_connection()
        cursor = conn.cursor()

        # Monthly trend
        query = """SELECT strftime('%Y-%m', expense_date) as month,
                          SUM(amount) as total, COUNT(*) as count
                   FROM expenses
                   WHERE expense_date >= ? AND expense_date <= ?
                   GROUP BY month
                   ORDER BY month"""
        cursor.execute(query, (start_date.isoformat(), end_date.isoformat()))
        trend_rows = cursor.fetchall()
        monthly_trend = [{"month": row["month"], "total": row["total"], "count": row["count"]} for row in trend_rows]

        # Top expenses
        query = """SELECT id, description, amount, category, subcategory, expense_date
                   FROM expenses
                   WHERE expense_date >= ? AND expense_date <= ?
                   ORDER BY amount DESC
                   LIMIT 5"""
        cursor.execute(query, (start_date.isoformat(), end_date.isoformat()))
        top_rows = cursor.fetchall()
        top_expenses = [dict(row) for row in top_rows]

        # Average monthly spend
        total_spent = sum(row["total"] for row in trend_rows)
        avg_monthly = round(total_spent / len(monthly_trend), 2) if monthly_trend else 0

        conn.close()

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
        logger.error(traceback.format_exc())
        return {
            "success": False,
            "message": f"Error: {str(e)}",
            "error": str(e)
        }


# ============================================================================
# RESOURCES
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
def resource_recent_expenses() -> str:
    """Resource: Recent expenses (last 10)."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM expenses ORDER BY expense_date DESC, id DESC LIMIT ?",
            (RECENT_EXPENSES_LIMIT,)
        )
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()

        return json.dumps({"limit": RECENT_EXPENSES_LIMIT, "expenses": rows})
    except Exception as e:
        logger.error(f"Error reading recent expenses resource: {e}")
        return json.dumps({"error": str(e)})


@mcp.resource("expenses://summary")
def resource_summary() -> str:
    """Resource: Current month expense summary by category."""
    try:
        start_date, end_date = get_month_bounds(None)

        conn = get_connection()
        cursor = conn.cursor()

        query = f"""SELECT category, SUM(amount) as total, COUNT(*) as count
                    FROM expenses
                    WHERE expense_date BETWEEN ? AND ?
                    GROUP BY category
                    ORDER BY total DESC"""
        cursor.execute(query, (start_date, end_date))
        rows = cursor.fetchall()
        conn.close()

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
            summary["categories"][row["category"]] = {
                "total": row["total"],
                "count": row["count"],
                "average": round(row["total"] / row["count"], 2) if row["count"] > 0 else 0
            }
            summary["overall"]["total_amount"] += row["total"]
            summary["overall"]["total_expenses"] += row["count"]

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
    init_db()
    mcp.run()
