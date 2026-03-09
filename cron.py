"""
Vercel serverless function — triggered by cron job daily.
GET /api/cron  →  runs the Bluesky bot
"""
from http.server import BaseHTTPRequestHandler
import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from bluesky_bot import run_bluesky_bot


class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        try:
            asyncio.run(run_bluesky_bot())
            body   = json.dumps({"status": "ok", "message": "Bluesky bot ran successfully!"})
            status = 200
        except Exception as e:
            body   = json.dumps({"status": "error", "message": str(e)})
            status = 500

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())
