# Expense Tracker MCP

A powerful FastMCP server for tracking expenses with full CRUD, budgets, recurring expenses, and reporting capabilities.

## Quick Start

**Local Development:**
```bash
# Install dependencies
uv sync

# Run server
uv run python main.py

# Test with inspector
uv run fastmcp inspector main.py:mcp
```

**Horizon Deployment:**
See [HORIZON_SETUP.md](HORIZON_SETUP.md) for step-by-step instructions.

## Features

**Core:**
- ✅ Add, update, delete, and list expenses
- ✅ Categorized expenses with subcategories
- ✅ Case-insensitive category matching with fuzzy suggestions

**Advanced:**
- ✅ Monthly budgets with spending alerts
- ✅ Recurring expenses (auto-generate monthly)
- ✅ CSV export with filtering
- ✅ Monthly trend reports

**API:**
- 🛠️ **14 Tools** - Full expense management
- 📖 **3 Resources** - Recent, summary, categories
- 💬 **2 Prompts** - Weekly report, categorize expense

## Directory Structure

```
data/          → Database (expenses.db)
config/        → Configuration (categories.json)
exports/       → Generated CSV files
logs/          → Application logs
```

See [DIRECTORY_STRUCTURE.md](DIRECTORY_STRUCTURE.md) for details.

## Tools

| Tool | Purpose |
|------|---------|
| `add_expense` | Add new expense |
| `get_expense` | Get expense by ID |
| `update_expense` | Update expense |
| `delete_expense` | Delete expense |
| `list_expenses` | List all expenses |
| `summarize_expenses` | Summary by category |
| `set_budget` | Set category budget |
| `check_budget_status` | Check budget usage |
| `add_recurring_expense` | Add recurring item |
| `list_recurring_expenses` | List recurring items |
| `generate_due_recurring_expenses` | Generate due items |
| `export_expenses_csv` | Export to CSV |
| `monthly_report` | Monthly analytics |

## Environment Variables

```bash
EXPENSE_TRACKER_DB_PATH=/path/to/expenses.db          # Database location
EXPENSE_TRACKER_CONFIG_PATH=/path/to/categories.json  # Config location
```

## Technology Stack

- **Python 3.12+**
- **FastMCP 3.4.7+** - MCP server framework
- **aiosqlite** - Async SQLite
- **asyncio** - Async I/O

## Deployment

**Local:** `uv run python main.py`

**Docker:** 
```bash
docker build -t expense-tracker .
docker run -v expense_data:/data expense-tracker
```

**Horizon:** See [HORIZON_SETUP.md](HORIZON_SETUP.md)

## License

MIT
