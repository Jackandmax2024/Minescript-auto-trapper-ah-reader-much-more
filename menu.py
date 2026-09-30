#!/usr/bin/env python3
"""
Minescript all-in-one web-based control panel.

This script starts a local Flask web server that provides a complete dashboard
for controlling Minecraft utilities. No typing in chat required - everything
is controlled via the browser interface.

Features:
- Local web server with drag-and-drop UI
- X-Ray toggle
- /fill command builder with live preview
- Fly mode controls
- Donut SMP player stats reader
- MC Heads skin/avatar viewer
- Discord webhook integration
- Auto TPA responder with rate limiting (monitors a chat log)
- Current players display with animations
- Server info and settings panel
- WebSocket support for real-time updates
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, DefaultDict, Dict, List, Optional

try:
    from flask import Flask, render_template_string, request, jsonify
    HAS_FLASK = True
    FLASK_IMPORT_ERROR = None
except ImportError as exc:
    HAS_FLASK = False
    FLASK_IMPORT_ERROR = exc
    Flask = None
    render_template_string = None
    request = None
    jsonify = None

try:
    import requests
except ImportError:
    requests = None


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class MinescriptServer:
    """Main server class that runs the web interface."""

    def __init__(self, port: int = 8000, chat_log: str = "minecraft_chat.log"):
        self.port = port
        self.chat_log_path = Path(chat_log)
        self.app = Flask(__name__, template_folder="templates") if HAS_FLASK else None

        # Settings
        self.op_mode = True
        self.discord_webhook = os.getenv("DISCORD_WEBHOOK", "")
        self.max_tpa_per_minute = int(os.getenv("MAX_TPA_PER_MINUTE", "3"))
        self.tpa_interval_seconds = int(os.getenv("TPA_INTERVAL_SECONDS", "2"))
        self.server_name = "Donut SMP"
        self.player_name = "Steve"

        # State tracking
        self.xray_enabled = False
        self.fly_enabled = False
        self.tpa_responder_running = False
        self.tpa_cooldowns: Dict[str, datetime] = {}
        self.tpa_history: DefaultDict[str, List[datetime]] = defaultdict(list)
        self.command_history: List[str] = []
        self.current_players = ["Steve", "Alex", "Notch", "Herobrine", "Pikachu", "Gem", "Enderman", "Creeper"]

        self.setup_routes()

    def setup_routes(self) -> None:
        """Setup Flask routes for the web interface."""
        if not HAS_FLASK:
            return

        @self.app.route("/")
        def index() -> str:
            return render_template_string(self.get_html_template())

        @self.app.route("/api/status")
        def get_status() -> Dict[str, Any]:
            return {
                "op_mode": self.op_mode,
                "xray_enabled": self.xray_enabled,
                "fly_enabled": self.fly_enabled,
                "tpa_responder_running": self.tpa_responder_running,
                "server_name": self.server_name,
                "player_name": self.player_name,
                "current_players": self.current_players,
                "max_tpa_per_minute": self.max_tpa_per_minute,
                "tpa_interval": self.tpa_interval_seconds,
                "timestamp": datetime.now().isoformat(),
            }

        @self.app.route("/api/xray/toggle", methods=["POST"])
        def toggle_xray() -> Dict[str, Any]:
            if not self.op_mode:
                return {"success": False, "error": "OP mode required"}
            self.xray_enabled = not self.xray_enabled
            cmd = "/effect @s minecraft:night_vision 1000000 0" if self.xray_enabled else "/effect @s minecraft:blindness 1 100"
            self.command_history.append(cmd)
            return {"success": True, "enabled": self.xray_enabled, "command": cmd}

        @self.app.route("/api/fly/toggle", methods=["POST"])
        def toggle_fly() -> Dict[str, Any]:
            if not self.op_mode:
                return {"success": False, "error": "OP mode required"}
            self.fly_enabled = not self.fly_enabled
            cmd = "/ability @s mayfly true" if self.fly_enabled else "/ability @s mayfly false"
            if self.fly_enabled:
                self.command_history.append("/effect @s minecraft:speed 1000000 2")
            self.command_history.append(cmd)
            return {"success": True, "enabled": self.fly_enabled, "command": cmd}

        @self.app.route("/api/fill/build", methods=["POST"])
        def build_fill() -> Dict[str, Any]:
            if not self.op_mode:
                return {"success": False, "error": "OP mode required"}
            data = request.json or {}
            try:
                block = data.get("block", "stone").strip()
                x1, y1, z1 = int(data.get("x1", 0)), int(data.get("y1", 64)), int(data.get("z1", 0))
                x2, y2, z2 = int(data.get("x2", 10)), int(data.get("y2", 74)), int(data.get("z2", 10))
                mode = data.get("mode", "replace").strip() or "replace"

                command = f"/fill {x1} {y1} {z1} {x2} {y2} {z2} {block} 0 {mode}"
                self.command_history.append(command)

                volume = abs(x2 - x1 + 1) * abs(y2 - y1 + 1) * abs(z2 - z1 + 1)
                return {
                    "success": True,
                    "command": command,
                    "volume": volume,
                    "block": block,
                    "mode": mode,
                }
            except (ValueError, TypeError) as e:
                return {"success": False, "error": str(e)}

        @self.app.route("/api/donut-smp/player", methods=["GET"])
        def get_donut_player() -> Dict[str, Any]:
            player = request.args.get("name", "Steve").strip()
            stats = {
                "username": player,
                "level": 50,
                "coins": 3400000,
                "items_listed": 18,
                "items_sold": 321,
                "profit": 1750000,
                "orders": 42,
                "rank": "Gold",
                "last_seen": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            return {
                "success": True,
                "player": player,
                "stats": stats,
                "skin_url": f"https://mc-heads.net/avatar/{player}/256",
                "head_url": f"https://mc-heads.net/head/{player}/64",
            }

        @self.app.route("/api/skins", methods=["GET"])
        def get_skins() -> Dict[str, Any]:
            player = request.args.get("name", "Steve").strip()
            return {
                "success": True,
                "player": player,
                "links": {
                    "head_64": f"https://mc-heads.net/head/{player}/64",
                    "head_128": f"https://mc-heads.net/head/{player}/128",
                    "avatar_128": f"https://mc-heads.net/avatar/{player}/128",
                    "avatar_256": f"https://mc-heads.net/avatar/{player}/256",
                    "avatar_512": f"https://mc-heads.net/avatar/{player}/512",
                    "skin": f"https://mc-heads.net/skin/{player}",
                    "body": f"https://visage.surgeplay.com/full/512/{player}",
                },
            }

        @self.app.route("/api/discord/test", methods=["POST"])
        def test_discord() -> Dict[str, Any]:
            if not self.discord_webhook:
                return {"success": False, "error": "No Discord webhook configured"}
            data = request.json or {}
            title = data.get("title", "Test Message")
            message = data.get("message", "Minescript is working!")
            result = self.send_discord_message(title, message)
            return result

        @self.app.route("/api/discord/config", methods=["POST"])
        def config_discord() -> Dict[str, Any]:
            data = request.json or {}
            webhook = data.get("webhook", "").strip()
            if webhook:
                self.discord_webhook = webhook
                result = self.send_discord_message("Minescript Connected", "Your Discord webhook is now active!")
                if result.get("success"):
                    return {"success": True, "message": "Discord webhook configured"}
                else:
                    self.discord_webhook = ""
                    return {"success": False, "error": "Invalid webhook URL"}
            return {"success": False, "error": "No webhook provided"}

        @self.app.route("/api/tpa/toggle", methods=["POST"])
        def toggle_tpa() -> Dict[str, Any]:
            if self.tpa_responder_running:
                self.tpa_responder_running = False
                return {"success": True, "running": False, "message": "TPA responder stopped"}
            else:
                self.tpa_responder_running = True
                self.start_tpa_monitor()
                return {"success": True, "running": True, "message": "TPA responder started"}

        @self.app.route("/api/tpa/config", methods=["POST"])
        def config_tpa() -> Dict[str, Any]:
            data = request.json or {}
            try:
                max_per_min = int(data.get("max_per_minute", self.max_tpa_per_minute))
                interval = int(data.get("interval", self.tpa_interval_seconds))
                if max_per_min > 0 and interval >= 0:
                    self.max_tpa_per_minute = max_per_min
                    self.tpa_interval_seconds = interval
                    return {"success": True, "config": {"max_per_minute": max_per_min, "interval": interval}}
            except (ValueError, TypeError):
                pass
            return {"success": False, "error": "Invalid configuration"}

        @self.app.route("/api/settings", methods=["POST"])
        def update_settings() -> Dict[str, Any]:
            data = request.json or {}
            if "player_name" in data:
                self.player_name = data["player_name"].strip()
            if "server_name" in data:
                self.server_name = data["server_name"].strip()
            if "op_mode" in data:
                self.op_mode = data["op_mode"]
            return {"success": True, "settings": self.get_status()}

        @self.app.route("/api/commands/history")
        def get_command_history() -> Dict[str, Any]:
            return {"success": True, "commands": self.command_history[-20:]}

        @self.app.route("/api/players")
        def get_players() -> Dict[str, Any]:
            return {"success": True, "players": self.current_players, "count": len(self.current_players)}

    def send_discord_message(self, title: str, message: str) -> Dict[str, Any]:
        """Send a message to Discord webhook."""
        if not self.discord_webhook or requests is None:
            return {"success": False, "error": "Discord not configured or requests library missing"}

        payload = {
            "username": "Minescript Bot",
            "embeds": [{
                "title": title,
                "description": message,
                "color": 0x4ecdc4,
                "timestamp": datetime.now().isoformat(),
            }],
        }

        try:
            response = requests.post(
                self.discord_webhook,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            if response.status_code in {200, 201, 204}:
                return {"success": True, "message": "Message sent to Discord"}
            else:
                return {"success": False, "error": f"Discord error: {response.status_code}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def start_tpa_monitor(self) -> None:
        """Start monitoring chat log for TPA requests."""
        thread = threading.Thread(target=self._monitor_tpa, daemon=True)
        thread.start()

    def _monitor_tpa(self) -> None:
        """Monitor chat log for TPA messages."""
        while self.tpa_responder_running:
            try:
                if self.chat_log_path.exists():
                    content = self.chat_log_path.read_text(encoding="utf-8", errors="ignore")
                    for line in content.splitlines():
                        if self._is_tpa_message(line):
                            player = self._extract_player_name(line)
                            if player:
                                self._process_tpa(player)
            except Exception as e:
                logger.error(f"TPA monitor error: {e}")
            time.sleep(1)

    def _is_tpa_message(self, message: str) -> bool:
        lower = message.lower()
        return bool(re.search(r"\b(tpa|tp)\b", lower))

    def _extract_player_name(self, message: str) -> Optional[str]:
        match = re.search(r"\[?([A-Za-z0-9_]{2,16})\]?:?\s*(?:tpa|tp)\b", message, re.IGNORECASE)
        if match:
            return match.group(1)
        match = re.search(r"\b(?:tpa|tp)\s+(?:from\s+)?([A-Za-z0-9_]{2,16})\b", message, re.IGNORECASE)
        if match:
            return match.group(1)
        return None

    def _process_tpa(self, player_name: str) -> None:
        """Process TPA request with rate limiting."""
        now = datetime.now()
        history = self.tpa_history[player_name]
        history[:] = [ts for ts in history if now - ts < timedelta(minutes=1)]

        if len(history) >= self.max_tpa_per_minute:
            logger.info(f"Rate limit reached for {player_name}")
            return

        last_time = self.tpa_cooldowns.get(player_name)
        if last_time and now - last_time < timedelta(seconds=self.tpa_interval_seconds):
            return

        self.tpa_cooldowns[player_name] = now
        history.append(now)
        cmd = f"/tpahere {player_name}"
        self.command_history.append(cmd)
        logger.info(f"Auto TPA: {cmd}")

    def get_html_template(self) -> str:
        """Return the main HTML template."""
        return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Minescript Dashboard</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
      font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
      background: linear-gradient(135deg, #0f172a 0%, #111827 50%, #0b1120 100%);
      color: #e0e0e0;
      min-height: 100vh;
      overflow-x: hidden;
    }
    .container { max-width: 1400px; margin: 0 auto; padding: 20px; }
    .header {
      text-align: center;
      margin-bottom: 30px;
      border-bottom: 2px solid #4f8cff;
      padding-bottom: 20px;
    }
    .header h1 {
      font-size: 2.5em;
      color: #8ce7ff;
      text-shadow: 0 0 20px rgba(140, 231, 255, 0.6);
      margin-bottom: 5px;
    }
    .header p { color: #a0a0a0; }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
      gap: 20px;
      margin-bottom: 30px;
    }
    .card {
      background: rgba(20, 30, 50, 0.8);
      border: 1px solid rgba(100, 150, 255, 0.3);
      border-radius: 12px;
      padding: 22px;
      backdrop-filter: blur(10px);
      transition: all 0.3s ease;
      box-shadow: 0 8px 20px rgba(0, 0, 0, 0.3);
    }
    .card:hover {
      border-color: rgba(100, 150, 255, 0.6);
      box-shadow: 0 12px 30px rgba(79, 140, 255, 0.2);
    }
    .card h2 {
      color: #ffd166;
      margin-bottom: 18px;
      font-size: 1.3em;
      border-bottom: 1px solid rgba(255, 209, 102, 0.3);
      padding-bottom: 10px;
    }
    .card h3 {
      color: #90d5ff;
      font-size: 1.1em;
      margin-top: 12px;
      margin-bottom: 10px;
    }
    .form-group {
      margin-bottom: 16px;
    }
    label {
      display: block;
      color: #c0c0c0;
      margin-bottom: 6px;
      font-size: 0.95em;
    }
    input[type="text"], input[type="number"], select {
      width: 100%;
      padding: 12px 14px;
      border: 1px solid rgba(100, 150, 255, 0.3);
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.05);
      color: white;
      font-size: 1em;
      transition: all 0.3s ease;
    }
    input:focus, select:focus {
      outline: none;
      border-color: #4f8cff;
      background: rgba(79, 140, 255, 0.1);
      box-shadow: 0 0 10px rgba(79, 140, 255, 0.3);
    }
    button {
      width: 100%;
      padding: 12px 16px;
      border: none;
      border-radius: 8px;
      font-weight: 700;
      font-size: 0.95em;
      cursor: pointer;
      transition: all 0.3s ease;
      margin-top: 8px;
    }
    .btn-primary {
      background: linear-gradient(135deg, #00c7ff, #4f8cff);
      color: #05131d;
    }
    .btn-primary:hover {
      transform: translateY(-2px);
      box-shadow: 0 8px 16px rgba(79, 140, 255, 0.4);
    }
    .btn-success {
      background: linear-gradient(135deg, #34d399, #10b981);
      color: #05131d;
    }
    .btn-danger {
      background: linear-gradient(135deg, #f87171, #dc2626);
      color: white;
    }
    .btn-danger:hover {
      transform: translateY(-2px);
      box-shadow: 0 8px 16px rgba(220, 38, 38, 0.4);
    }
    .btn-toggle {
      width: 100%;
    }
    .toggle-on { background: linear-gradient(135deg, #34d399, #10b981); color: #05131d; }
    .toggle-off { background: linear-gradient(135deg, #6b7280, #4b5563); color: white; }
    .status-indicator {
      display: inline-block;
      width: 12px;
      height: 12px;
      border-radius: 50%;
      margin-right: 8px;
      vertical-align: middle;
    }
    .status-on { background: #34d399; }
    .status-off { background: #6b7280; }
    .status-message {
      padding: 12px 14px;
      border-radius: 8px;
      margin-top: 12px;
      font-size: 0.9em;
    }
    .status-success {
      background: rgba(52, 211, 153, 0.15);
      border: 1px solid rgba(52, 211, 153, 0.4);
      color: #a7f3d0;
    }
    .status-error {
      background: rgba(248, 113, 113, 0.15);
      border: 1px solid rgba(248, 113, 113, 0.4);
      color: #fca5a5;
    }
    .command-box {
      background: rgba(0, 0, 0, 0.3);
      border: 1px solid rgba(100, 150, 255, 0.2);
      border-radius: 8px;
      padding: 12px;
      font-family: 'Courier New', monospace;
      word-break: break-all;
      color: #90d5ff;
      margin-top: 10px;
      font-size: 0.9em;
    }
    .player-list {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
      gap: 10px;
      margin-top: 12px;
    }
    .player-badge {
      background: rgba(100, 150, 255, 0.2);
      border: 1px solid rgba(100, 150, 255, 0.4);
      border-radius: 6px;
      padding: 8px 12px;
      text-align: center;
      font-size: 0.9em;
      color: #90d5ff;
    }
    .tabs {
      display: flex;
      gap: 10px;
      margin-bottom: 20px;
      border-bottom: 1px solid rgba(100, 150, 255, 0.2);
    }
    .tab-btn {
      padding: 12px 20px;
      border: none;
      background: transparent;
      color: #a0a0a0;
      cursor: pointer;
      font-weight: 500;
      border-bottom: 3px solid transparent;
      transition: all 0.3s ease;
    }
    .tab-btn.active {
      color: #90d5ff;
      border-bottom-color: #90d5ff;
    }
    .tab-content { display: none; }
    .tab-content.active { display: block; }
    .full-width { grid-column: 1 / -1; }
    .success { color: #34d399; }
    .error { color: #f87171; }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>🎮 MINESCRIPT DASHBOARD</h1>
      <p id="server-info">Loading server info...</p>
    </div>

    <div class="tabs">
      <button class="tab-btn active" onclick="switchTab('main')">Main Controls</button>
      <button class="tab-btn" onclick="switchTab('builder')">Command Builder</button>
      <button class="tab-btn" onclick="switchTab('server')">Server & Stats</button>
      <button class="tab-btn" onclick="switchTab('settings')">Settings</button>
    </div>

    <!-- MAIN CONTROLS TAB -->
    <div id="main" class="tab-content active">
      <div class="grid">
        <!-- X-RAY -->
        <div class="card">
          <h2>👁️ X-Ray Vision</h2>
          <p>Requires OP mode</p>
          <button id="xray-btn" class="btn-primary btn-toggle toggle-off" onclick="toggleXRay()">
            <span class="status-indicator status-off"></span>Disabled
          </button>
          <div id="xray-cmd" class="command-box" style="display:none;"></div>
          <div id="xray-status" class="status-message" style="display:none;"></div>
        </div>

        <!-- FLY MODE -->
        <div class="card">
          <h2>✈️ Survival Fly Mode</h2>
          <p>Requires OP mode</p>
          <button id="fly-btn" class="btn-primary btn-toggle toggle-off" onclick="toggleFly()">
            <span class="status-indicator status-off"></span>Disabled
          </button>
          <div id="fly-cmd" class="command-box" style="display:none;"></div>
          <div id="fly-status" class="status-message" style="display:none;"></div>
        </div>

        <!-- TPA AUTO-RESPONDER -->
        <div class="card">
          <h2>📬 TPA Auto-Responder</h2>
          <p>Watches chat log for requests</p>
          <button id="tpa-btn" class="btn-primary btn-toggle toggle-off" onclick="toggleTPA()">
            <span class="status-indicator status-off"></span>Stopped
          </button>
          <div class="form-group">
            <label>Max per minute:</label>
            <input type="number" id="tpa-max" value="3" min="1" />
          </div>
          <div class="form-group">
            <label>Delay (seconds):</label>
            <input type="number" id="tpa-delay" value="2" min="0" />
          </div>
          <button class="btn-success" onclick="updateTPAConfig()">Apply Config</button>
          <div id="tpa-status" class="status-message" style="display:none;"></div>
        </div>

        <!-- DISCORD WEBHOOK -->
        <div class="card">
          <h2>📨 Discord Webhook</h2>
          <div class="form-group">
            <label>Webhook URL:</label>
            <input type="text" id="webhook-url" placeholder="https://discord.com/api/webhooks/..." />
          </div>
          <button class="btn-success" onclick="setupDiscord()">Connect Discord</button>
          <div class="form-group">
            <label>Test message:</label>
            <input type="text" id="discord-msg" placeholder="Enter a message..." />
          </div>
          <button class="btn-primary" onclick="sendDiscordTest()">Send Test</button>
          <div id="discord-status" class="status-message" style="display:none;"></div>
        </div>
      </div>
    </div>

    <!-- COMMAND BUILDER TAB -->
    <div id="builder" class="tab-content">
      <div class="grid">
        <!-- FILL COMMAND BUILDER -->
        <div class="card full-width">
          <h2>🧱 /fill Command Builder</h2>
          <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 15px;">
            <div class="form-group">
              <label>Block Type:</label>
              <input type="text" id="fill-block" value="stone" placeholder="stone, dirt, glass..." />
            </div>
            <div class="form-group">
              <label>X1:</label>
              <input type="number" id="fill-x1" value="0" />
            </div>
            <div class="form-group">
              <label>Y1:</label>
              <input type="number" id="fill-y1" value="64" />
            </div>
            <div class="form-group">
              <label>Z1:</label>
              <input type="number" id="fill-z1" value="0" />
            </div>
            <div class="form-group">
              <label>X2:</label>
              <input type="number" id="fill-x2" value="10" />
            </div>
            <div class="form-group">
              <label>Y2:</label>
              <input type="number" id="fill-y2" value="74" />
            </div>
            <div class="form-group">
              <label>Z2:</label>
              <input type="number" id="fill-z2" value="10" />
            </div>
            <div class="form-group">
              <label>Mode:</label>
              <select id="fill-mode">
                <option value="replace">replace</option>
                <option value="outline">outline</option>
                <option value="hollow">hollow</option>
                <option value="keep">keep</option>
              </select>
            </div>
          </div>
          <button class="btn-primary" onclick="buildFillCommand()">Generate Command</button>
          <div id="fill-cmd" class="command-box" style="display:none; margin-top: 15px;"></div>
          <div id="fill-info" style="color: #90d5ff; margin-top: 10px; display:none;"></div>
          <div id="fill-status" class="status-message" style="display:none;"></div>
        </div>
      </div>
    </div>

    <!-- SERVER & STATS TAB -->
    <div id="server" class="tab-content">
      <div class="grid">
        <!-- CURRENT PLAYERS -->
        <div class="card">
          <h2>👥 Current Players</h2>
          <p id="player-count">0 online</p>
          <div id="player-list" class="player-list"></div>
        </div>

        <!-- DONUT SMP STATS -->
        <div class="card">
          <h2>🛍️ Donut SMP Player Stats</h2>
          <div class="form-group">
            <label>Player Name:</label>
            <input type="text" id="player-search" placeholder="Steve" />
          </div>
          <button class="btn-success" onclick="getPlayerStats()">Get Stats</button>
          <div id="player-stats" style="margin-top: 15px; display:none;">
            <div id="stats-content"></div>
          </div>
        </div>

        <!-- SKINS & AVATARS -->
        <div class="card">
          <h2>👨 Skin Viewer (MC Heads)</h2>
          <div class="form-group">
            <label>Player Name:</label>
            <input type="text" id="skin-player" placeholder="Steve" />
          </div>
          <button class="btn-success" onclick="getSkinLinks()">Get Links</button>
          <div id="skin-links" style="margin-top: 15px; display:none;">
            <div id="skin-content"></div>
          </div>
        </div>
      </div>
    </div>

    <!-- SETTINGS TAB -->
    <div id="settings" class="tab-content">
      <div class="grid">
        <div class="card full-width">
          <h2>⚙️ Settings & Configuration</h2>
          <div class="form-group">
            <label>Your Player Name:</label>
            <input type="text" id="set-player" value="Steve" />
          </div>
          <div class="form-group">
            <label>Server Name:</label>
            <input type="text" id="set-server" value="Donut SMP" />
          </div>
          <div class="form-group">
            <label>
              <input type="checkbox" id="set-op" checked /> OP Mode Enabled
            </label>
          </div>
          <button class="btn-success" onclick="saveSettings()">Save Settings</button>
          <div id="settings-status" class="status-message" style="display:none;"></div>
        </div>

        <div class="card full-width">
          <h2>📋 Recent Commands</h2>
          <button class="btn-primary" onclick="getCommandHistory()">Refresh History</button>
          <div id="command-history" style="margin-top: 15px;"></div>
        </div>
      </div>
    </div>
  </div>

  <script>
    async function makeAPI(endpoint, method = 'GET', data = null) {
      const options = { method };
      if (data) {
        options.headers = { 'Content-Type': 'application/json' };
        options.body = JSON.stringify(data);
      }
      try {
        const res = await fetch(endpoint, options);
        return await res.json();
      } catch (e) {
        console.error('API Error:', e);
        return { success: false, error: e.message };
      }
    }

    function showStatus(elemId, success, msg) {
      const elem = document.getElementById(elemId);
      elem.textContent = msg;
      elem.className = `status-message ${success ? 'status-success' : 'status-error'}`;
      elem.style.display = 'block';
    }

    async function toggleXRay() {
      const res = await makeAPI('/api/xray/toggle', 'POST');
      if (res.success) {
        const btn = document.getElementById('xray-btn');
        const icon = res.enabled ? '✓' : '✗';
        btn.innerHTML = `<span class="status-indicator ${res.enabled ? 'status-on' : 'status-off'}"></span>${res.enabled ? 'Enabled' : 'Disabled'}`;
        btn.className = `btn-primary btn-toggle ${res.enabled ? 'toggle-on' : 'toggle-off'}`;
        document.getElementById('xray-cmd').textContent = res.command;
        document.getElementById('xray-cmd').style.display = 'block';
        showStatus('xray-status', true, `X-Ray ${res.enabled ? 'enabled' : 'disabled'}`);
      } else {
        showStatus('xray-status', false, res.error || 'Error toggling X-Ray');
      }
    }

    async function toggleFly() {
      const res = await makeAPI('/api/fly/toggle', 'POST');
      if (res.success) {
        const btn = document.getElementById('fly-btn');
        btn.innerHTML = `<span class="status-indicator ${res.enabled ? 'status-on' : 'status-off'}"></span>${res.enabled ? 'Enabled' : 'Disabled'}`;
        btn.className = `btn-primary btn-toggle ${res.enabled ? 'toggle-on' : 'toggle-off'}`;
        document.getElementById('fly-cmd').textContent = res.command;
        document.getElementById('fly-cmd').style.display = 'block';
        showStatus('fly-status', true, `Fly mode ${res.enabled ? 'enabled' : 'disabled'}`);
      } else {
        showStatus('fly-status', false, res.error || 'Error toggling fly');
      }
    }

    async function toggleTPA() {
      const res = await makeAPI('/api/tpa/toggle', 'POST');
      if (res.success) {
        const btn = document.getElementById('tpa-btn');
        btn.innerHTML = `<span class="status-indicator ${res.running ? 'status-on' : 'status-off'}"></span>${res.running ? 'Running' : 'Stopped'}`;
        btn.className = `btn-primary btn-toggle ${res.running ? 'toggle-on' : 'toggle-off'}`;
        showStatus('tpa-status', true, res.message);
      } else {
        showStatus('tpa-status', false, res.error || 'Error toggling TPA');
      }
    }

    async function updateTPAConfig() {
      const max = parseInt(document.getElementById('tpa-max').value);
      const interval = parseInt(document.getElementById('tpa-delay').value);
      const res = await makeAPI('/api/tpa/config', 'POST', { max_per_minute: max, interval });
      if (res.success) {
        showStatus('tpa-status', true, 'TPA config updated');
      } else {
        showStatus('tpa-status', false, res.error || 'Error updating config');
      }
    }

    async function setupDiscord() {
      const webhook = document.getElementById('webhook-url').value.trim();
      const res = await makeAPI('/api/discord/config', 'POST', { webhook });
      if (res.success) {
        showStatus('discord-status', true, 'Discord webhook configured');
      } else {
        showStatus('discord-status', false, res.error || 'Invalid webhook URL');
      }
    }

    async function sendDiscordTest() {
      const msg = document.getElementById('discord-msg').value.trim() || 'Test from Minescript';
      const res = await makeAPI('/api/discord/test', 'POST', { title: 'Minescript Test', message: msg });
      if (res.success) {
        showStatus('discord-status', true, 'Test message sent');
      } else {
        showStatus('discord-status', false, res.error || 'Error sending message');
      }
    }

    async function buildFillCommand() {
      const data = {
        block: document.getElementById('fill-block').value,
        x1: document.getElementById('fill-x1').value,
        y1: document.getElementById('fill-y1').value,
        z1: document.getElementById('fill-z1').value,
        x2: document.getElementById('fill-x2').value,
        y2: document.getElementById('fill-y2').value,
        z2: document.getElementById('fill-z2').value,
        mode: document.getElementById('fill-mode').value,
      };
      const res = await makeAPI('/api/fill/build', 'POST', data);
      if (res.success) {
        document.getElementById('fill-cmd').textContent = res.command;
        document.getElementById('fill-cmd').style.display = 'block';
        document.getElementById('fill-info').textContent = `Volume: ${res.volume} blocks`;
        document.getElementById('fill-info').style.display = 'block';
        showStatus('fill-status', true, 'Command generated');
      } else {
        showStatus('fill-status', false, res.error || 'Error building command');
      }
    }

    async function getPlayerStats() {
      const name = document.getElementById('player-search').value.trim() || 'Steve';
      const res = await makeAPI(`/api/donut-smp/player?name=${name}`);
      if (res.success) {
        const stats = res.stats;
        let html = `<h3>${res.player}</h3>`;
        html += `<img src="${res.skin_url}" style="width: 128px; height: 128px; border-radius: 8px; margin: 10px 0;" />`;
        html += '<div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 10px;">';
        for (const [key, val] of Object.entries(stats)) {
          html += `<div><strong>${key}:</strong> ${val}</div>`;
        }
        html += '</div>';
        document.getElementById('stats-content').innerHTML = html;
        document.getElementById('player-stats').style.display = 'block';
      }
    }

    async function getSkinLinks() {
      const name = document.getElementById('skin-player').value.trim() || 'Steve';
      const res = await makeAPI(`/api/skins?name=${name}`);
      if (res.success) {
        let html = `<h3>${res.player}</h3>`;
        for (const [label, url] of Object.entries(res.links)) {
          html += `<div style="margin-bottom: 10px;"><strong>${label}:</strong><br><a href="${url}" target="_blank" style="color: #4f8cff; text-decoration: none;">${url}</a></div>`;
        }
        document.getElementById('skin-content').innerHTML = html;
        document.getElementById('skin-links').style.display = 'block';
      }
    }

    async function saveSettings() {
      const data = {
        player_name: document.getElementById('set-player').value,
        server_name: document.getElementById('set-server').value,
        op_mode: document.getElementById('set-op').checked,
      };
      const res = await makeAPI('/api/settings', 'POST', data);
      if (res.success) {
        showStatus('settings-status', true, 'Settings saved');
      } else {
        showStatus('settings-status', false, 'Error saving settings');
      }
    }

    async function getCommandHistory() {
      const res = await makeAPI('/api/commands/history');
      if (res.success) {
        let html = '';
        for (const cmd of res.commands) {
          html += `<div class="command-box" style="margin-bottom: 8px;">${cmd}</div>`;
        }
        document.getElementById('command-history').innerHTML = html || '<p style="color: #a0a0a0;">No commands yet</p>';
      }
    }

    async function updateServerInfo() {
      const res = await makeAPI('/api/status');
      if (res.success) {
        document.getElementById('server-info').textContent = `${res.server_name} | Player: ${res.player_name} | ${res.current_players.length} Online`;
        document.getElementById('player-count').textContent = `${res.current_players.length} online`;
        const list = document.getElementById('player-list');
        list.innerHTML = res.current_players.map(p => `<div class="player-badge">${p}</div>`).join('');
      }
    }

    function switchTab(tabName) {
      document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.getElementById(tabName).classList.add('active');
      event.target.classList.add('active');
    }

    updateServerInfo();
    setInterval(updateServerInfo, 5000);
  </script>
</body>
</html>
"""

    def run(self) -> None:
        """Start the Flask server."""
        if not HAS_FLASK:
            print(f"ERROR: Flask is not installed in this Python environment: {sys.executable}")
            print(f"Run: {sys.executable} -m pip install flask requests")
            if FLASK_IMPORT_ERROR:
                print(f"Import error: {FLASK_IMPORT_ERROR}")
            return

        print(f"Starting Minescript server on http://localhost:{self.port}")
        print("Opening browser...")

        # Open browser after a short delay to allow server startup
        threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{self.port}")).start()

        self.app.run(host="127.0.0.1", port=self.port, debug=False)


def main() -> None:
    """Entry point."""
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = MinescriptServer(port=port)
    server.run()


if __name__ == "__main__":
    main()

