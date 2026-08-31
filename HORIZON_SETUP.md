# Expense Tracker - Horizon Deployment Guide

## Prerequisites

- Prefect Cloud account (sign up at https://app.prefect.cloud)
- CLI: `pip install prefect`
- Your repo: https://github.com/BRachana/Expense-tracker-remote-MCP-server

## Step 1: Authenticate with Prefect Cloud

```bash
prefect cloud login
# Follow the prompts to enter your API key
```

## Step 2: Create a Work Pool in Horizon

Go to https://app.prefect.cloud and:
1. Navigate to **Work Pools**
2. Click **+ Create Work Pool**
3. Select **Docker** or **VM** (depending on your Horizon setup)
4. Name it `expense-tracker-pool`
5. Save

## Step 3: Create Deployment Configuration

Create a `prefect.yaml` file in your project root:

```yaml
# prefect.yaml
deployments:
  - name: expense-tracker-mcp
    description: "Expense Tracker MCP Server"
    entrypoint: main.py:mcp.run
    parameters:
      transport: "http"
      host: "0.0.0.0"
      port: 8000
    work_pool:
      name: expense-tracker-pool
    work_queue:
      name: default
    schedule:
      interval: 3600  # Health check interval
```

## Step 4: Deploy to Horizon

```bash
# Push to Horizon
prefect deploy --name expense-tracker-mcp

# Start the work pool worker (run once, keeps server running)
prefect work-pool get expense-tracker-pool
prefect worker start expense-tracker-pool
```

## Step 5: Get Your Server URL

Once deployed, go to:
- https://app.prefect.cloud → Deployments → expense-tracker-mcp
- Look for the **public URL** or **deployment URL**

It will be something like: `https://expense-tracker-mcp-xxxxx.horizon.prefect.io`

## Step 6: Configure Claude Desktop

Edit `~/.claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "Expense Tracker (Remote)": {
      "command": "curl",
      "args": [
        "-N",
        "-X",
        "POST",
        "https://your-horizon-url:8000",
        "-H",
        "Content-Type: application/json",
        "-d",
        "@-"
      ],
      "type": "stdio"
    }
  }
}
```

Replace `https://your-horizon-url:8000` with your actual Horizon server URL.

## Step 7: Restart Claude Desktop

Close and reopen Claude Desktop. Your remote MCP server should now be connected!

## Testing

In Claude, you should be able to:
- Call `list_expenses()` → returns expenses from remote database
- Call `add_expense(...)` → creates expense on remote server
- Call any of the 14 tools
- Read the 3 resources
- Use the 2 prompts

## Troubleshooting

**Connection refused:**
- Make sure your Horizon worker is running (`prefect worker start expense-tracker-pool`)
- Check the URL is correct

**Database file not found:**
- The database is created in `/app/expenses.db` inside the container
- It persists across deployments (Horizon keeps volumes)

**Logs:**
```bash
# View deployment logs
prefect deployment logs expense-tracker-mcp

# Stream logs in real-time
prefect deployment logs expense-tracker-mcp -f
```

---

**Questions?** Check Prefect docs: https://docs.prefect.io/latest/
