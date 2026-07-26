"""
Entry point.

    python run.py

Starts the Flask app (which serves both the REST API under /api/* and the
static frontend from /static). Defaults to http://127.0.0.1:5050.
"""
import os

from backend.app import app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    print(f"\n  Data Pipeline Orchestrator running at http://127.0.0.1:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=debug)
