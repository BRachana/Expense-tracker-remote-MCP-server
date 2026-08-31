"""
Horizon deployment configuration for Expense Tracker MCP Server.
Deploy to Horizon with: prefect deploy -n horizon_deployment -p horizon
"""

from prefect import serve
from prefect.deployments import DeploymentBase
import subprocess
import time


def run_mcp_server():
    """Run the FastMCP expense tracker server."""
    process = subprocess.Popen(["python", "main.py"])
    try:
        process.wait()
    except KeyboardInterrupt:
        process.terminate()
        process.wait()


if __name__ == "__main__":
    # This will be run by Horizon
    run_mcp_server()
