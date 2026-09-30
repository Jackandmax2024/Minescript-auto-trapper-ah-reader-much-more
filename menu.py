#!/usr/bin/env python3
"""Minescript all-in-one menu utility.

This script is intended to run locally as a helper tool. It can:
- open a localhost page in your browser
- generate sample Minecraft commands such as /fill and /tpahere
- show mock Donut SMP player stats
- display MC Heads skin/avatar URLs
- send a Discord webhook message
- monitor a chat log file for "tpa" or "tp" and rate-limit auto responses

It does not require a live Minecraft runtime object. If you want this to
interact with your actual server, plug in your own bridge or command execution
layer in the functions that print the commands.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import DefaultDict, Dict, List, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


class MinescriptMenu:
    """Simple all-in-one Minecraft utility menu."""

    def __init__(self, *, op_mode: bool = True, chat_log_path: str = "minecraft_chat.log"):
        self.op_mode = op_mode
        self.chat_log_path = Path(chat_log_path)
        self.discord_webhook = os.getenv("DISCORD_WEBHOOK", "")
        self.localhost_port = int(os.getenv("LOCALHOST_PORT", "8000"))
        self.max_tpa_per_minute = int(os.getenv("MAX_TPA_PER_MINUTE", "3"))
        self.tpa_interval_seconds = int(os.getenv("TPA_INTERVAL_SECONDS", "2"))
        self.tpa_cooldowns: Dict[str, datetime] = {}
        self.tpa_history: DefaultDict[str, List[datetime]] = defaultdict(list)
        self.running = True
        self.chat_thread: Optional[threading.Thread] = None
        self.current_players = ["Steve", "Alex", "Notch", "Herobrine", "Pikachu", "Gem"]

    def clear_console(self) -> None:
        try:
            os.system("cls" if os.name == "nt" else "clear")
        except Exception:
            pass

    def safe_input(self, prompt: str) -> str:
        try:
            return input(prompt)
        except EOFError:
            print("\nInput ended. Exiting.")
            raise SystemExit(0)

    def print_header(self) -> None:
        print("")
        print("===============================================")
        print("     MINESCRIPT - ALL-IN-ONE MENU")
        print("     OP X-RAY | /FILL | DISCORD | TPA")
        print("===============================================")
        print(f"Player mode: {'OP' if self.op_mode else 'Non-OP'} | Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("")

    def print_menu(self) -> None:
        print("Choose an option:")
        print("1) Open Chrome localhost page")
        print("2) X-Ray toggle")
        print("3) Build /fill command")
        print("4) Fly mode (OP only)")
        print("5) Donut SMP player stats")
        print("6) Show skin / avatar links")
        print("7) Set Discord webhook")
        print("8) Start TPA auto-responder")
        print("9) Configure TPA limits")
        print("10) Current players animation")
        print("11) Exit")
        print("")

    def ensure_op(self) -> bool:
        if not self.op_mode:
            print("This action requires OP mode. Set op_mode=True or use a Minecraft bridge that reports OP status.")
            return False
        return True

    def open_chrome_localhost(self, port: Optional[int] = None) -> None:
        port = port or self.localhost_port
        url = f"http://localhost:{port}"
        try:
            webbrowser.get("google-chrome")
        except Exception:
            pass
        webbrowser.open(url)
        print(f"Opened browser to: {url}")

        html = self.build_localhost_page()
        outfile = Path("local_minescript_dashboard.html")
        outfile.write_text(html, encoding="utf-8")
        print(f"Dashboard saved to: {outfile.resolve()}")

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
      background: linear-gradient(135deg, #0f172a, #111827, #0b1120);
      color: white;
    }
    .container { max-width: 1100px; margin: 0 auto; padding: 30px 18px 60px; }
    h1 {
      text-align: center;
      margin-bottom: 24px;
      color: #8ce7ff;
      text-shadow: 0 0 12px rgba(140, 231, 255, 0.7);
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 18px;
    }
    .card {
      background: rgba(255,255,255,0.06);
      border: 1px solid rgba(255,255,255,0.15);
      border-radius: 12px;
      padding: 18px;
      box-shadow: 0 8px 18px rgba(0,0,0,0.25);
    }
    .card h3 { margin-top: 0; color: #ffd166; }
    button {
      width: 100%;
      border: none;
      background: linear-gradient(135deg, #00c7ff, #4f8cff);
      color: #05131d;
      font-weight: bold;
      padding: 10px 12px;
      border-radius: 8px;
      margin-top: 8px;
      cursor: pointer;
    }
    input, select {
      width: 100%;
      box-sizing: border-box;
      padding: 10px 12px;
      border-radius: 8px;
      border: 1px solid rgba(255,255,255,0.2);
      background: rgba(255,255,255,0.05);
      color: white;
      margin-top: 8px;
      margin-bottom: 8px;
    }
    .status {
      margin-top: 18px;
      padding: 12px 14px;
      border-radius: 8px;
      background: rgba(52, 211, 153, 0.12);
      border: 1px solid rgba(52, 211, 153, 0.45);
      color: #a7f3d0;
    }
  </style>
</head>
<body>
  <div class="container">
    <h1>🎮 Minescript Dashboard</h1>
    <div class="grid">
      <div class="card">
        <h3>X-Ray</h3>
        <button>Enable X-Ray</button>
        <button>Disable X-Ray</button>
      </div>
      <div class="card">
        <h3>/fill Builder</h3>
        <input type="text" placeholder="Block ID" />
        <input type="text" placeholder="X1 Y1 Z1" />
        <input type="text" placeholder="X2 Y2 Z2" />
        <button>Execute Fill</button>
      </div>
      <div class="card">
        <h3>Fly Mode</h3>
        <button>Enable Fly</button>
        <button>Disable Fly</button>
      </div>
      <div class="card">
        <h3>Discord</h3>
        <input type="text" placeholder="Webhook URL" />
        <button>Send Test Message</button>
      </div>
      <div class="card">
        <h3>TPA Auto-Responder</h3>
        <select>
          <option>Enabled</option>
          <option>Disabled</option>
        </select>
        <button>Toggle TPA Listener</button>
      </div>
      <div class="card">
        <h3>Donut SMP</h3>
        <input type="text" placeholder="Player name" />
        <button>View Stats</button>
      </div>
    </div>
    <div class="status">Status: Ready • Localhost server active</div>
  </div>
</body>
</html>
"""

    def toggle_xray(self) -> None:
        if not self.ensure_op():
            return
        print("X-Ray enabled. Your server bridge can trigger: /effect @s minecraft:night_vision 1000000 0")
        print("Example: use your Minecraft bridge or command API to send that command.")

    def build_fill_command(self) -> None:
        if not self.ensure_op():
            return
        print("Fill command builder")
        try:
            block = self.safe_input("Block type: ").strip() or "stone"
            x1, y1, z1 = [int(v) for v in self.safe_input("X1 Y1 Z1: ").split()]
            x2, y2, z2 = [int(v) for v in self.safe_input("X2 Y2 Z2: ").split()]
            mode = self.safe_input("Fill mode [replace/default]: ").strip() or "replace"
            command = f"/fill {x1} {y1} {z1} {x2} {y2} {z2} {block} 0 {mode}"
            print(f"Generated command: {command}")
            print("Example: /fill 10 70 10 20 80 20 stone 0 replace")
        except ValueError:
            print("Invalid coordinates. Use numbers like: 10 64 10")

    def fly_toggle(self) -> None:
        if not self.ensure_op():
            return
        print("Fly mode enabled. Example commands:")
        print("/ability @s mayfly true")
        print("/effect @s minecraft:speed 1000000 2")
        print("Disable with: /ability @s mayfly false")

    def donut_smp_player_stats(self) -> None:
        print("Donut SMP AH player stats")
        name = self.safe_input("Player name: ").strip() or "Steve"
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
            answer = self.safe_input("Send these stats to Discord? [y/N]: ").strip().lower()
            if answer in {"y", "yes"}:
                self.send_webhook(title=f"Donut SMP stats for {name}", fields=stats)

    def show_skin_links(self) -> None:
        name = self.safe_input("Player name: ").strip() or "Steve"
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
            print("No Discord webhook configured. Set the DISCORD_WEBHOOK environment variable or paste the URL in the menu.")
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
            response = requests.post(
                self.discord_webhook,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            if response.status_code in {200, 201, 204}:
                print("Discord webhook sent successfully.")
            else:
                print(f"Discord webhook failed: {response.status_code} - {response.text}")
        except Exception as exc:
            print(f"Error sending Discord webhook: {exc}")

    def prompt_discord_webhook(self) -> None:
        url = self.safe_input("Paste your Discord webhook URL: ").strip()
        if url:
            self.discord_webhook = url
            print("Discord webhook saved for this session.")
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
            content = self.chat_log_path.read_text(encoding="utf-8", errors="ignore")
            return [line.strip() for line in content.splitlines() if line.strip()]
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
            value = int(self.safe_input("Set max TPA per minute: ").strip())
            if value > 0:
                self.max_tpa_per_minute = value
                print(f"Updated to {value} per minute.")
        except ValueError:
            print("Invalid value.")

        try:
            interval = int(self.safe_input("Set TPA delay in seconds: ").strip())
            if interval >= 0:
                self.tpa_interval_seconds = interval
                print(f"Updated delay to {interval} seconds.")
        except ValueError:
            print("Invalid value.")

    def animate_players(self) -> None:
        print("Current players animation:")
        for idx, player in enumerate(self.current_players, 1):
            bar = "#" * (idx * 2)
            print(f"[{idx}] {bar} {player}")
            time.sleep(0.25)

    def menu_loop(self) -> None:
        while True:
            self.clear_console()
            self.print_header()
            self.print_menu()
            choice = self.safe_input("Select option: ").strip()

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

            self.safe_input("\nPress Enter to continue...")


def main() -> None:
    print("Minescript startup")
    menu = MinescriptMenu()
    menu.menu_loop()


if __name__ == "__main__":
    main()
