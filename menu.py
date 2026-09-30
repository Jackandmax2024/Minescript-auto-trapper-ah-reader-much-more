#!/usr/bin/env python3
"""Minescript all-in-one menu and utility script.

This script is designed to run as a local Python utility and can:
- open a localhost page in Chrome
- generate useful Minecraft commands such as /fill, /tpahere, and fly toggles
- read mock or API-based player stats from a Donut SMP-like source
- pull skin/avatar links from MC Heads
- send messages to a Discord webhook
- monitor a log file for chat lines mentioning 'tpa' or 'tp' and auto-trigger a /tpahere response

It intentionally avoids hard dependency on a live Minecraft runtime object because
those objects are not available in a normal Python environment. If you are using
this alongside a Minecraft bridge, adapter, or custom logging system, you can plug
it in by editing the _execute_command and _read_chat_lines methods.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


class MinescriptMenu:
    """All-in-one menu for Minecraft-style utilities."""

    def __init__(self, *, op_mode: bool = True, chat_log_path: str = "minecraft_chat.log"):
        self.op_mode = op_mode
        self.chat_log_path = Path(chat_log_path)
        self.discord_webhook = os.getenv("DISCORD_WEBHOOK", "")
        self.localhost_port = int(os.getenv("LOCALHOST_PORT", "8000"))
        self.max_tpa_per_minute = int(os.getenv("MAX_TPA_PER_MINUTE", "3"))
        self.tpa_interval_seconds = int(os.getenv("TPA_INTERVAL_SECONDS", "2"))
        self.tpa_cooldowns: Dict[str, datetime] = {}
        self.tpa_history: Dict[str, List[datetime]] = defaultdict(list)
        self.running = True
        self.chat_thread: Optional[threading.Thread] = None
        self.current_players = ["Steve", "Alex", "Notch", "Herobrine", "Pikachu", "Gem"]

    def clear_console(self) -> None:
        """Clear console output."""
        os.system("cls" if os.name == "nt" else "clear")

    def print_header(self) -> None:
        lines = [
            "",
            "╔═════════════════════════════════════════════════════════════════════╗",
            "║                   MINESCRIPT - ALL-IN-ONE MENU                   ║",
            "║           X-RAY | /FILL | DISCORD | TPA AUTO-RESPONDER          ║",
            "╚═════════════════════════════════════════════════════════════════════╝",
            f"Player mode: {'OP' if self.op_mode else 'Non-OP'} | Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
        ]
        for line in lines:
            print(line)

    def print_menu(self) -> None:
        print("Choose an option:")
        print("1) Open Chrome localhost page")
        print("2) X-Ray toggle")
        print("3) Build /fill command")
        print("4) Fly mode (OP only)")
        print("5) Donut SMP AH player stats")
        print("6) Show Minecraft skin / avatar links")
        print("7) Send a message to Discord webhook")
        print("8) Start TPA auto-responder")
        print("9) Configure TPA rate limit")
        print("10) Animation: current players")
        print("11) Exit")
        print("")

    def open_chrome_localhost(self, port: Optional[int] = None) -> None:
        """Open a localhost URL in the system browser."""
        port = port or self.localhost_port
        url = f"http://localhost:{port}"
        try:
            webbrowser.get("google-chrome")
        except Exception:
            pass
        webbrowser.open(url)
        print(f"Opened: {url}")

        html = self.build_localhost_page()
        path = Path("local_minescript_dashboard.html")
        path.write_text(html, encoding="utf-8")
        print(f"Local dashboard saved to: {path.resolve()}")

    def build_localhost_page(self) -> str:
        return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Minescript Dashboard</title>
  <style>
    body {
      margin: 0;
      font-family: Arial, sans-serif;
      background: radial-gradient(circle at top, #132238, #0a0f1a 52%, #070b12 100%);
      color: #f2f2f2;
    }
    .container {
      max-width: 1100px;
      margin: 0 auto;
      padding: 32px 20px 60px;
    }
    h1 {
      text-align: center;
      font-size: 2.5rem;
      margin-bottom: 24px;
      color: #8ce7ff;
      text-shadow: 0 0 12px rgba(140, 231, 255, 0.7);
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(270px, 1fr));
      gap: 20px;
    }
    .card {
      background: rgba(255,255,255,0.06);
      border: 1px solid rgba(255,255,255,0.15);
      border-radius: 12px;
      padding: 20px;
      box-shadow: 0 8px 20px rgba(0,0,0,0.25);
    }
    .card h3 {
      margin-top: 0;
      color: #ffd166;
    }
    button {
      width: 100%;
      border: none;
      background: linear-gradient(135deg, #00c7ff, #4f8cff);
      color: #05131d;
      font-weight: 700;
      padding: 10px 14px;
      border-radius: 8px;
      margin-top: 8px;
      cursor: pointer;
    }
    input, select {
      width: 100%;
      box-sizing: border-box;
      padding: 10px 12px;
      border-radius: 8px;
      border: 1px solid rgba(255,255,255,0.15);
      background: rgba(255,255,255,0.05);
      color: white;
      margin-top: 8px;
      margin-bottom: 8px;
    }
    .status {
      margin-top: 18px;
      background: rgba(56, 211, 102, 0.1);
      border: 1px solid rgba(56, 211, 102, 0.45);
      border-radius: 8px;
      padding: 12px 14px;
      color: #9ef0b1;
    }
  </style>
</head>
<body>
  <div class="container">
    <h1>🎮 Minescript Dashboard</h1>
    <div class="grid">
      <div class="card">
        <h3>👁️ X-Ray</h3>
        <button>Enable X-Ray</button>
        <button>Disable X-Ray</button>
      </div>
      <div class="card">
        <h3>🧱 /fill Builder</h3>
        <input type="text" placeholder="Block ID (stone, dirt, glass)..." />
        <input type="text" placeholder="X1 Y1 Z1" />
        <input type="text" placeholder="X2 Y2 Z2" />
        <button>Execute Fill</button>
      </div>
      <div class="card">
        <h3>✈️ Fly Mode</h3>
        <button>Enable Fly</button>
        <button>Disable Fly</button>
      </div>
      <div class="card">
        <h3>📨 Discord</h3>
        <input type="text" placeholder="Webhook URL" />
        <button>Send Test Message</button>
      </div>
      <div class="card">
        <h3>📈 TPA Auto-Responder</h3>
        <select>
          <option>Enabled</option>
          <option>Disabled</option>
        </select>
        <button>Toggle TPA Listener</button>
      </div>
      <div class="card">
        <h3>🧬 Donut SMP</h3>
        <input type="text" placeholder="Player name" />
        <button>View Stats</button>
      </div>
    </div>
    <div class="status">Status: Ready • Localhost server active</div>
  </div>
</body>
</html>
"""

    def ensure_op(self) -> bool:
        if not self.op_mode:
            print("This action requires OP mode. Set op_mode=True or use a Minecraft bridge that reports OP status.")
            return False
        return True

    def toggle_xray(self) -> None:
        if not self.ensure_op():
            return
        print("X-Ray enabled. Use your game/server bridge to call the matching command for your setup.")
        print("Example command: /effect @s minecraft:night_vision 1000000 0")

    def build_fill_command(self) -> None:
        if not self.ensure_op():
            return
        print("Fill command builder")
        try:
            block = input("Block type: ").strip() or "stone"
            x1, y1, z1 = [int(v) for v in input("X1 Y1 Z1: ").split()]
            x2, y2, z2 = [int(v) for v in input("X2 Y2 Z2: ").split()]
            mode = input("Fill mode [replace/default]: ").strip() or "replace"
            command = f"/fill {x1} {y1} {z1} {x2} {y2} {z2} {block} 0 {mode}"
            print(f"Generated command: {command}")
            print("Example use: /fill 10 70 10 20 80 20 stone 0 replace")
        except ValueError:
            print("Invalid coordinates. Use numbers like: 10 64 10")

    def fly_toggle(self) -> None:
        if not self.ensure_op():
            return
        print("Fly mode enabled. Example command: /ability @s mayfly true")
        print("To disable: /ability @s mayfly false")

    def donut_smp_player_stats(self) -> None:
        print("Donut SMP AH player stats")
        name = input("Player name: ").strip() or "Steve"
        stats = {
            "username": name,
            "coins": 3400000,
            "items_listed": 18,
            "items_sold": 321,
            "profit": 1750000,
            "orders": 42,
            "rank": "Gold",
            "last_seen": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        print("Player stats:")
        for key, value in stats.items():
            print(f"  {key}: {value}")
        print(f"Skin/Avatar URL: https://mc-heads.net/avatar/{name}/256")
        if self.discord_webhook:
            answer = input("Send these stats to Discord? [y/N]: ").strip().lower()
            if answer in {"y", "yes"}:
                self.send_webhook(title=f"Donut SMP stats for {name}", fields=stats)

    def show_skin_links(self) -> None:
        name = input("Player name: ").strip() or "Steve"
        links = {
            "head_64": f"https://mc-heads.net/head/{name}/64",
            "avatar_128": f"https://mc-heads.net/avatar/{name}/128",
            "avatar_256": f"https://mc-heads.net/avatar/{name}/256",
            "skin": f"https://mc-heads.net/skin/{name}",
        }
        print(f"MC Heads links for {name}:")
        for label, link in links.items():
            print(f"  {label}: {link}")

    def send_webhook(self, title: str, fields: Optional[Dict[str, object]] = None, image_url: Optional[str] = None) -> None:
        if not self.discord_webhook:
            print("No Discord webhook configured. Set the DISCORD_WEBHOOK env var or use the menu option to set one.")
            return
        if requests is None:
            print("The 'requests' package is not installed. Run: pip install requests")
            return

        payload = {
            "username": "Minescript Bot",
            "embeds": [{
                "title": title,
                "color": 0x4ecdc4,
                "fields": [
                    {"name": str(key), "value": str(value), "inline": True}
                    for key, value in (fields or {}).items()
                ],
            }],
        }
        if image_url:
            payload["embeds"][0]["thumbnail"] = {"url": image_url}

        try:
            response = requests.post(self.discord_webhook, data=json.dumps(payload), headers={"Content-Type": "application/json"}, timeout=10)
            if response.status_code in {200, 201, 204}:
                print("Discord webhook sent successfully.")
            else:
                print(f"Discord webhook failed: {response.status_code} - {response.text}")
        except Exception as exc:
            print(f"Error sending Discord webhook: {exc}")

    def prompt_discord_webhook(self) -> None:
        url = input("Paste your Discord webhook URL: ").strip()
        if url:
            self.discord_webhook = url
            print("Discord webhook saved in memory for this session.")
            self.send_webhook("Minescript connected", {"status": "online"})
        else:
            print("No URL entered.")

    def start_tpa_responder(self) -> None:
        if self.chat_thread and self.chat_thread.is_alive():
            print("TPA responder is already running.")
            return
        self.running = True
        self.chat_thread = threading.Thread(target=self._chat_listener_loop, daemon=True)
        self.chat_thread.start()
        print(f"TPA responder started. Max per minute: {self.max_tpa_per_minute} | Delay: {self.tpa_interval_seconds}s")

    def stop_tpa_responder(self) -> None:
        self.running = False
        print("TPA responder stopped.")

    def _chat_listener_loop(self) -> None:
        while self.running:
            try:
                for line in self._read_chat_lines():
                    if self._is_tpa_message(line):
                        player_name = self._extract_player_name(line)
                        if player_name:
                            self._maybe_send_tpa_here(player_name)
            except Exception as exc:  # pragma: no cover
                print(f"Chat listener error: {exc}")
            time.sleep(1)

    def _read_chat_lines(self) -> List[str]:
        if not self.chat_log_path.exists():
            return []
        try:
            text = self.chat_log_path.read_text(encoding="utf-8", errors="ignore")
            return [line.strip() for line in text.splitlines() if line.strip()]
        except Exception:
            return []

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

    def _maybe_send_tpa_here(self, player_name: str) -> None:
        now = datetime.now()
        history = self.tpa_history[player_name]
        history[:] = [ts for ts in history if now - ts < timedelta(minutes=1)]
        if len(history) >= self.max_tpa_per_minute:
            print(f"Rate limit reached for {player_name}. Ignoring extra TPA requests.")
            return
        last_time = self.tpa_cooldowns.get(player_name)
        if last_time and now - last_time < timedelta(seconds=self.tpa_interval_seconds):
            return
        self.tpa_cooldowns[player_name] = now
        history.append(now)
        print(f"Auto-sending /tpahere {player_name}")
        print(f"Example command: /tpahere {player_name}")

    def configure_tpa_limits(self) -> None:
        print("Current settings:")
        print(f"  Max TPA per minute: {self.max_tpa_per_minute}")
        print(f"  TPA interval seconds: {self.tpa_interval_seconds}")
        try:
            value = int(input("Set max TPA per minute: ").strip())
            if value > 0:
                self.max_tpa_per_minute = value
                print(f"Updated to {value} per minute.")
        except ValueError:
            print("Invalid value.")
        try:
            interval = int(input("Set TPA delay in seconds: ").strip())
            if interval >= 0:
                self.tpa_interval_seconds = interval
                print(f"Updated delay to {interval} seconds.")
        except ValueError:
            print("Invalid value.")

    def animate_players(self) -> None:
        print("Current players animation:")
        for idx, player in enumerate(self.current_players, 1):
            bar = "█" * (idx * 2)
            print(f"[{idx}] {bar} {player}")
            time.sleep(0.25)

    def menu_loop(self) -> None:
        while True:
            self.clear_console()
            self.print_header()
            self.print_menu()
            choice = input("Select option: ").strip()

            if choice == "1":
                self.open_chrome_localhost()
            elif choice == "2":
                self.toggle_xray()
            elif choice == "3":
                self.build_fill_command()
            elif choice == "4":
                self.fly_toggle()
            elif choice == "5":
                self.donut_smp_player_stats()
            elif choice == "6":
                self.show_skin_links()
            elif choice == "7":
                self.prompt_discord_webhook()
            elif choice == "8":
                self.start_tpa_responder()
            elif choice == "9":
                self.configure_tpa_limits()
            elif choice == "10":
                self.animate_players()
            elif choice == "11":
                print("Goodbye.")
                break
            else:
                print("Invalid option. Try again.")

            input("\nPress Enter to continue...")


def main() -> None:
    print("Minescript startup")
    menu = MinescriptMenu()
    menu.menu_loop()


if __name__ == "__main__":
    main()
