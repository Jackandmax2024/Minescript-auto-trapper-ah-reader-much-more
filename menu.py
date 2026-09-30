# menu.py -- Minescript <-> browser bridge
# Put this file in your  minecraft/minescript/  folder, then type  \menu  in Minecraft.
# Optional: \menu 9000   (use a different port)
#
# Stdlib only, no pip installs. Binds to 127.0.0.1 and requires a secret token.
import json, os, re, sys, time, math, threading, secrets, queue, collections, webbrowser, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import minescript as m

# ----------------------------------------------------------------- config
PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8765
try:
    HERE = os.path.dirname(os.path.abspath(__file__))
except NameError:
    HERE = os.path.abspath(os.path.join(os.getcwd(), "minescript"))
SCRIPT_PREFIX = "web_"                      # scripts saved from the browser -> web_<name>.py
TOKEN_FILE = os.path.join(HERE, "menu_token.txt")
CONFIG_FILE = os.path.join(HERE, "menu_config.json")   # webhook, quick buttons, auto settings (keep private)
CHAT_SOURCE = "both"       # "both" | "listener" | "log"   (log = tail logs/latest.log, catches player chat)
LOG_PATH = None            # set a path here if your latest.log is somewhere unusual

# "Tools" tab (ore scanner, look-at, key holds, op quick-commands) only unlocks on
# singleplayer or on these hosts. Add your own test server address here.
TEST_HOSTS = ["localhost", "127.0.0.1", "::1", "donutsmp", "play.donutsmp.com", "donutsmp.com"]
BLOCKED_HOSTS = []
AUTO_OPEN_BROWSER = True
FORCE_TOOLS_UNLOCK = True

LAST_DENY = 0.0
MC = threading.Lock()
TOKEN = ""

# ----------------------------------------------------------------- event bus
HISTORY = collections.deque(maxlen=500)
CLIENTS = set()
CLIENTS_LOCK = threading.Lock()


def broadcast(ev):
    ev["t"] = time.time()
    with CLIENTS_LOCK:
        HISTORY.append(ev)
        for q in list(CLIENTS):
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass


# ----------------------------------------------------------------- chat parsing
NAME = r"[A-Za-z0-9_.]{3,16}"
CHAT_PATTERNS = [
    re.compile(rf"^<(?P<n>{NAME})>\s*(?P<m>.*)$"),
    re.compile(rf"^(?:.*?[^A-Za-z0-9_.])?(?P<n>{NAME})\s*[\":>\ufffd]+\s*(?P<m>.*)$"),
]
REQ_PATTERNS = [
    re.compile(rf"(?P<n>{NAME})\s+(?:has\s+)?requested\s+to\s+teleport\s+to\s+you", re.I),
    re.compile(rf"(?P<n>{NAME})\s+(?:has\s+)?requested\s+(?:that\s+)?you\s+teleport\s+to\s+(?:them|him|her)", re.I),
    re.compile(rf"(?P<n>{NAME})\s+(?:sent\s+you\s+a\s+)?tpa(?:here)?\s+request", re.I),
]
TP_WORD = re.compile(r"(?<![\w/])(/?)(tpahere|tpaccept|tpa|tp)(?![\w])", re.I)
COLOR = re.compile("\u00a7.")
NON_PLAYER = {"tip", "info", "note", "warning", "warn", "server", "broadcast", "announcement",
              "system", "shop", "vote", "event", "error", "usage", "help", "reminder"}
LOG_RE = re.compile(r"\]: (?:\[(?!CHAT)[^\]]*\] )*\[CHAT\] (.*)$")


def normalize(msg):
    t = " ".join(COLOR.sub("", str(msg)).split())
    return re.sub(r"^\[Not Secure\]\s*", "", t)


def parse_tp(msg):
    clean = normalize(msg)
    if not clean:
        return None
    for p in REQ_PATTERNS:
        mo = p.search(clean)
        if mo:
            low = clean.lower()
            kind = "tpahere" if ("here" in low or "you teleport" in low) else "tpa"
            return {"name": mo.group("n"), "kind": kind, "text": clean, "via": "request", "msg": clean, "slash": False}
    for p in CHAT_PATTERNS:
        mo = p.match(clean)
        if mo:
            if mo.group("n").lower() in NON_PLAYER:
                return None
            body = mo.group("m")
            w = TP_WORD.search(body)
            if w:
                kind = w.group(2).lower()
                return {"name": mo.group("n"), "kind": kind, "text": clean, "via": "chat", "msg": body, "slash": bool(w.group(1))}
    return None


PENDING = collections.deque()
PENDING_LOCK = threading.Lock()
ME = {"name": None}


def is_dup(src, text):
    now = time.time()
    with PENDING_LOCK:
        while PENDING and now - PENDING[0][0] > 3:
            PENDING.popleft()
        for i, (t, s2, x) in enumerate(PENDING):
            if s2 != src and x == text:
                del PENDING[i]
                return True
        PENDING.append((now, src, text))
    return False


def ingest(src, msg):
    text = normalize(msg)
    if not text or is_dup(src, text):
        return
    broadcast({"type": "chat", "msg": text})
    try:
        r = parse_tp(text)
    except Exception:
        r = None
    if not r:
        diag_unparsed(text)
        return
    broadcast({"type": "tp", "name": r["name"], "kind": r["kind"], "text": r["text"], "via": r["via"]})
    try:
        maybe_auto_tpahere(r)
    except Exception as e:
        broadcast({"type": "info", "msg": f"auto-tpahere error: {e}"})


def on_chat(msg):
    ingest("listener", msg)


def chat_loop():
    def extract(ev):
        if isinstance(ev, dict):
            return ev.get("message")
        return getattr(ev, "message", None)

    if hasattr(m, "EventQueue"):
        with m.EventQueue() as q:
            q.register_chat_listener()
            while True:
                msg = extract(q.get())
                if msg:
                    on_chat(str(msg))
    else:
        with m.ChatEventListener() as lst:
            while True:
                msg = extract(lst.get())
                if msg:
                    on_chat(str(msg))


def _dec(b):
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("cp1252", "replace")


def log_tail_loop():
    path = LOG_PATH or os.path.normpath(os.path.join(HERE, "..", "logs", "latest.log"))
    f, buf, seek_end, warned = None, b"", True, False
    while True:
        try:
            if f is None:
                if not os.path.isfile(path):
                    if not warned:
                        warned = True
                        m.echo(f"[menu] can't find {path} -- set LOG_PATH in menu.py to use the log chat source")
                    time.sleep(2)
                    continue
                f = open(path, "rb")
                if seek_end:
                    f.seek(0, 2)
                buf = b""
            chunk = f.read(65536)
            if chunk:
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for ln in lines:
                    mo = LOG_RE.search(_dec(ln).rstrip("\r"))
                    if mo:
                        ingest("log", mo.group(1))
                continue
            if os.path.getsize(path) < f.tell():
                f.close()
                f, seek_end = None, False
                continue
            time.sleep(0.2)
        except Exception:
            try:
                if f:
                    f.close()
            except Exception:
                pass
            f = None
            time.sleep(1)


def send(text):
    text = text.replace("\r", " ").replace("\n", " ").strip()[:256]
    if not text:
        return
    with MC:
        if text[0] in "/\\":
            m.execute(text)
        else:
            m.chat(text)
    broadcast({"type": "sent", "msg": text})


def safe_name(n):
    return (re.sub(r"[^A-Za-z0-9_]", "_", str(n))[:40] or "script").strip("_") or "script"


def script_path(n):
    return os.path.join(HERE, SCRIPT_PREFIX + safe_name(n) + ".py")


def list_scripts():
    out = []
    for f in sorted(os.listdir(HERE)):
        if f.startswith(SCRIPT_PREFIX) and f.endswith(".py"):
            out.append(f[len(SCRIPT_PREFIX):-3])
    return out


def run_script(name):
    with MC:
        m.execute("\\" + SCRIPT_PREFIX + safe_name(name))
    broadcast({"type": "info", "msg": f"started script {SCRIPT_PREFIX}{safe_name(name)} (output shows in Minecraft chat)"})


def tools_state():
    try:
        with MC:
            wi = m.world_info()
    except Exception:
        wi = None
    if wi is None:
        return (True, "forced") if FORCE_TOOLS_UNLOCK else (False, "Couldn't read the server address. If this is your own test server, set FORCE_TOOLS_UNLOCK = True in menu.py.")
    get = (lambda k: wi.get(k)) if isinstance(wi, dict) else (lambda k: getattr(wi, k, None))
    name = str(get("name") or "").lower()
    has_addr = ("address" in wi) if isinstance(wi, dict) else hasattr(wi, "address")
    addr = str(get("address") or "").lower()
    if any(b in (name + " " + addr) for b in BLOCKED_HOSTS):
        return False, "Tools are disabled on this server."
    if FORCE_TOOLS_UNLOCK:
        return True, "forced"
    if not has_addr:
        return False, "This Minescript version doesn't expose the server address. If this is your own test server, set FORCE_TOOLS_UNLOCK = True."
    if not addr:
        return True, "singleplayer"
    host = addr.split(":")[0] if addr.count(":") == 1 else addr
    if any(host == h or host.endswith("." + h) for h in TEST_HOSTS):
        return True, "test host " + host
    return False, f"Server '{addr}' is not in TEST_HOSTS in menu.py."


def require_tools():
    ok, why = tools_state()
    if not ok:
        raise PermissionError(why)


ORE_COLORS = {"coal": "#555", "iron": "#d8af93", "copper": "#e77c56", "gold": "#fcee4b",
              "redstone": "#ff2a2a", "lapis": "#345ec3", "diamond": "#5decf5",
              "emerald": "#17dd62", "quartz": "#eeeeee", "ancient_debris": "#8a5a44"}
SCAN = {"cancel": False, "running": False}


def ore_kind(block):
    b = block.split("[")[0].replace("minecraft:", "")
    if b == "ancient_debris":
        return "ancient_debris"
    if b.endswith("_ore"):
        return b.replace("deepslate_", "").replace("nether_", "")[:-4]
    return None


def scan_ores(radius, up, down, wanted):
    SCAN["running"], SCAN["cancel"] = True, False
    try:
        with MC:
            px, py, pz = [math.floor(v) for v in m.player_position()]
        y0, y1 = max(-64, py - down), min(319, py + up)
        found = []
        batch = []

        def flush():
            nonlocal batch
            if not batch:
                return
            try:
                with MC:
                    names = m.getblocklist(batch)
            except Exception:
                names = []
            for pos, nm in zip(batch, names):
                k = ore_kind(str(nm))
                if k and (not wanted or k in wanted):
                    found.append({"x": pos[0], "y": pos[1], "z": pos[2], "kind": k,
                                  "dist": round(math.dist(pos, (px, py, pz)), 1)})
            batch = []

        for x in range(px - radius, px + radius + 1):
            for y in range(y0, y1 + 1):
                for z in range(pz - radius, pz + radius + 1):
                    if SCAN["cancel"]:
                        broadcast({"type": "info", "msg": "scan cancelled"})
                        return
                    batch.append([x, y, z])
                    if len(batch) >= 6000:
                        flush()
        flush()
        found.sort(key=lambda r: r["dist"])
        broadcast({"type": "scan", "origin": [px, py, pz], "radius": radius,
                   "results": found[:1500], "colors": ORE_COLORS})
    except Exception as e:
        broadcast({"type": "info", "msg": f"scan failed: {e}"})
    finally:
        SCAN["running"] = False


def look_at(x, y, z):
    with MC:
        px, py, pz = m.player_position()
        dx, dy, dz = x + .5 - px, y + .5 - (py + 1.62), z + .5 - pz
        yaw = math.degrees(math.atan2(-dx, dz))
        pitch = -math.degrees(math.atan2(dy, math.hypot(dx, dz)))
        m.player_set_orientation(yaw, pitch)


def hold_key(key, on):
    fn = {"forward": "player_press_forward", "sprint": "player_press_sprint",
          "jump": "player_press_jump", "sneak": "player_press_sneak"}.get(key)
    f = getattr(m, fn, None) if fn else None
    if not f:
        raise ValueError("unsupported key on this Minescript version")
    with MC:
        f(bool(on))


def status():
    ok, why = tools_state()
    st = {"port": PORT, "tools": ok, "tools_reason": why, "scanning": SCAN["running"]}
    try:
        with MC:
            st["pos"] = [round(v, 1) for v in m.player_position()]
            st["player"] = m.player_name() if hasattr(m, "player_name") else None
    except Exception:
        pass
    return st


# ----------------------------------------------------------------- config / discord / auto / market / builders
DEFAULT_CFG = {
    "webhook": "",
    "quick": [{"label": "Shop", "cmd": "/shop"}, {"label": "AH", "cmd": "/ah"},
              {"label": "Home 1", "cmd": "/home 1"}, {"label": "Home 2", "cmd": "/home 2"},
              {"label": "Home 3", "cmd": "/home 3"}, {"label": "RTP", "cmd": "/rtp"},
              {"label": "Spawn", "cmd": "/spawn"}, {"label": "Accept", "cmd": "/tpaccept"},
              {"label": "Deny", "cmd": "/tpdeny"}],
    "auto": {"enabled": False, "cooldown": 120, "per_min": 4, "strict": True, "ignore": []},
}
CFG = {}
CFG_LOCK = threading.Lock()
HOOK_RE = re.compile(r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$")


def load_cfg():
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        for k in ("webhook", "quick"):
            if k in saved:
                cfg[k] = saved[k]
        if isinstance(saved.get("auto"), dict):
            cfg["auto"].update(saved["auto"])
    except (OSError, ValueError):
        pass
    cfg["auto"]["enabled"] = bool(cfg["auto"].get("enabled", False))
    return cfg


def save_cfg():
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(CFG, f, indent=2)
    os.replace(tmp, CONFIG_FILE)
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass


def public_cfg():
    with CFG_LOCK:
        hook = CFG.get("webhook", "")
        return {"quick": CFG["quick"], "auto": CFG["auto"], "webhook_set": bool(hook),
                "webhook_hint": hook[-4:] if hook else ""}


def update_cfg(d):
    with CFG_LOCK:
        if "quick" in d:
            q = []
            for it in list(d["quick"])[:30]:
                lab, cmd = str(it.get("label", "")).strip()[:20], str(it.get("cmd", "")).strip()[:100]
                if lab and cmd:
                    q.append({"label": lab, "cmd": cmd})
            CFG["quick"] = q
        if "auto" in d:
            a, cur = d["auto"], CFG["auto"]
            cur["enabled"] = bool(a.get("enabled"))
            cur["cooldown"] = max(10, min(int(a.get("cooldown", 120)), 3600))
            cur["per_min"] = max(1, min(int(a.get("per_min", 4)), 20))
            cur["strict"] = bool(a.get("strict", True))
            cur["ignore"] = [n for n in (str(x).strip()[:16] for x in list(a.get("ignore", []))[:50]) if n]
        if "webhook" in d:
            w = str(d["webhook"]).strip()
            if w and not HOOK_RE.match(w):
                raise ValueError("That doesn't look like a Discord webhook URL")
            CFG["webhook"] = w
        save_cfg()


def discord_post(text):
    with CFG_LOCK:
        url = CFG.get("webhook", "")
    if not url:
        raise ValueError("No Discord webhook saved yet (Market tab)")
    body = json.dumps({"content": text[:1900], "username": "Minescript Menu",
                       "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "MinescriptMenu/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Discord replied HTTP {e.code}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Couldn't reach Discord: {e.reason}")


AUTO_STATE = {"last": {}, "sent": collections.deque()}


def my_name():
    if ME["name"] is None:
        try:
            with MC:
                ME["name"] = m.player_name() if hasattr(m, "player_name") else ""
        except Exception:
            ME["name"] = ""
    return ME["name"]


def auto_enabled():
    with CFG_LOCK:
        return bool(CFG.get("auto", {}).get("enabled"))


def auto_check(r, commit=False):
    with CFG_LOCK:
        a = dict(CFG.get("auto", {}))
    if r["via"] != "chat" or r["kind"] not in ("tpa", "tp", "tpahere"):
        return False, f"not a chat request for tpa/tp (kind={r['kind']}, via={r['via']})"
    if r.get("slash"):
        return False, "the message has /tpa with a slash (server tip)"
    name = r["name"]
    low = name.lower()
    if low in NON_PLAYER:
        return False, f"'{name}' looks like a server label, not a player"
    if low == my_name().lower():
        return False, "that's you"
    if low in [x.lower() for x in a.get("ignore", [])]:
        return False, f"{name} is on your ignore list"
    if a.get("strict", True) and len(r["msg"].split()) > 5:
        return False, "message is longer than 5 words"
    now = time.time()
    left = a.get("cooldown", 120) - (now - AUTO_STATE["last"].get(low, 0))
    if left > 0:
        return False, f"cooldown: {int(left)}s left for {name}"
    dq = AUTO_STATE["sent"]
    while dq and now - dq[0] > 60:
        dq.popleft()
    if len(dq) >= a.get("per_min", 4):
        return False, "rate limit reached"
    if commit:
        AUTO_STATE["last"][low] = now
        dq.append(now)
    return True, f"would send /tpahere {name}"


def maybe_auto_tpahere(r):
    if not auto_enabled():
        return
    ok, why = auto_check(r, commit=True)
    if ok:
        send("/tpahere " + r["name"])
        broadcast({"type": "info", "msg": f"auto: sent /tpahere {r['name']} (they said: {r['msg'][:40]})"})
    elif r["via"] == "chat" and r["kind"] in ("tpa", "tp", "tpahere") and not r.get("slash"):
        broadcast({"type": "info", "msg": f"auto: skipped {r['name']} -- {why}"})


DIAG = {"t": 0.0}


def diag_unparsed(text):
    if not auto_enabled() or time.time() - DIAG["t"] < 2:
        return
    if any(t in text.lower() for t in ("tpa", "tpahere", "tpaccept", "tp")):
        DIAG["t"] = time.time()
        broadcast({"type": "info", "msg": f"auto: saw a tpa message but couldn't read the player name from: {text[:100]}"})


# ---- auction house / price reader (read-only) ----
MARKET = {"last": [], "query": ""}
PRICE_RE = re.compile(r"(?:\bprice\b|\bcost\b|\bbuy now\b|\bb\.n\.\b|\bbn\b)\W{0,10}\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*([KkMmBb])?(?![A-Za-z])", re.I)
ALT_PRICE_RE = re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*([KkMmBb])?(?![A-Za-z])", re.I)
ITEM_NAME_RE = re.compile(r"(?:item|name)\s*[:=]\s*(?:\"([^\"]+)\"|'([^']+)'|([^\n,]+))", re.I)
COUNT_RE = re.compile(r"(?:qty|count|amount|stack)\s*[:=]\s*([0-9][0-9,]*)", re.I)
SELLER_RE = re.compile(r"seller\W{0,4}([A-Za-z0-9_.]{3,16})", re.I)
MULT = {"k": 1e3, "m": 1e6, "b": 1e9}


def parse_price(text):
    if not text:
        return None
    text = str(text)
    for rx in (PRICE_RE, ALT_PRICE_RE):
        mo = rx.search(text)
        if mo:
            val = float(mo.group(1).replace(",", ""))
            suffix = (mo.group(2) or "").lower()
            return val * MULT.get(suffix, 1.0)
    return None


def parse_amount(x):
    mo = re.match(r"^([0-9][0-9,]*(?:\.[0-9]+)?)\s*([kKmMbB])?$", str(x).strip())
    if not mo:
        raise ValueError("Alert price should look like 500 or 2k")
    return float(mo.group(1).replace(",", "")) * MULT.get((mo.group(2) or "").lower(), 1.0)


def fmt_num(n):
    return "?" if n is None else f"{n:,.2f}".rstrip("0").rstrip(".")


def format_rows(title, rows):
    lines = [f"**{title}**"]
    for i, r in enumerate(rows, 1):
        item = r.get("name") or r.get("item") or "item"
        count = r.get("count") or 1
        price = r.get("price")
        seller = r.get("seller") or "unknown"
        unit = r.get("unit")
        if price is None:
            lines.append(f"{i}. {item} x{count} by {seller} - price unknown")
        else:
            lines.append(f"{i}. {item} x{count} by {seller} - ${fmt_num(price)} (${fmt_num(unit) if unit is not None else '?'} each)")
    return "\n".join(lines)


def read_screen():
    with MC:
        items = m.container_get_items()
        scr = m.screen_name() if hasattr(m, "screen_name") else None
    if items is None:
        raise RuntimeError(f"No chest/GUI is open right now (screen: {scr}). Open the /ah screen first.")
    rows = []
    for it in items:
        if isinstance(it, dict):
            g = it.get
        else:
            g = lambda k, d=None, it=it: getattr(it, k, d)
        item = g("item") or g("name")
        count = g("count") or 1
        nbt = g("nbt") or ""
        slot = g("slot")
        if not item:
            continue
        short = str(item).replace("minecraft:", "")
        item_name = short.split("[")[0].replace("_", " ").strip()
        raw = str(nbt or "")
        raw_flat = raw + " " + short
        name_match = ITEM_NAME_RE.search(raw_flat)
        if name_match:
            item_name = next((v for v in name_match.groups() if v), item_name)
        price = parse_price(raw_flat)
        if price is None:
            mprice = re.search(r"(?i)(?:price|cost|buy now|bn|b\.n\.)\W{0,10}\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*([KkMmBb])?", raw_flat)
            if mprice:
                val = float(mprice.group(1).replace(",", ""))
                price = val * MULT.get((mprice.group(2) or "").lower(), 1.0)
        count_match = COUNT_RE.search(raw_flat)
        if count_match:
            try:
                count = int(count_match.group(1).replace(",", ""))
            except Exception:
                pass
        try:
            count_int = int(count or 1)
        except Exception:
            count_int = 1
        seller = SELLER_RE.search(raw_flat)
        unit = (price / count_int) if price is not None and count_int else None
        rows.append({
            "slot": slot,
            "item": short,
            "count": count_int,
            "name": item_name,
            "price": price,
            "unit": unit,
            "seller": seller.group(1) if seller else None,
            "info": raw[:180] if raw else item_name,
        })
    priced = [r for r in rows if r["price"] is not None]
    return (priced or rows), []


def market_job(query, wait, alert, notify):
    q = query or ""
    try:
        if query is not None:
            send(("/ah " + query).strip())
            time.sleep(wait)
        rows, debug = read_screen()
        rows.sort(key=lambda r: (r["unit"] is None, r["unit"] or 0))
        MARKET["last"], MARKET["query"] = rows, q
        broadcast({"type": "market", "query": q, "items": rows[:200], "debug": debug})
        if alert is not None and notify:
            hits = [r for r in rows if r["price"] is not None and r["price"] <= alert]
            if hits:
                discord_post(format_rows(f"AH alert: '{q}' at or under ${fmt_num(alert)}", hits[:10]))
                broadcast({"type": "info", "msg": f"sent {min(len(hits), 10)} deal(s) to Discord"})
    except Exception as e:
        broadcast({"type": "market", "query": q, "items": [], "debug": [], "error": str(e)})


# ---- creative builders (Tools tab, test servers only) ----
FONT = {
    "A": [".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
    "B": ["####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."],
    "C": [".###.", "#...#", "#....", "#....", "#....", "#...#", ".###."],
    "D": ["####.", "#...#", "#...#", "#...#", "#...#", "#...#", "####."],
    "E": ["#####", "#....", "#....", "####.", "#....", "#....", "#####"],
    "F": ["#####", "#....", "#....", "####.", "#....", "#....", "#...."],
    "G": [".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".###."],
    "H": ["#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
    "I": ["#####", "..#..", "..#..", "..#..", "..#..", "..#..", "#####"],
    "J": ["..###", "...#.", "...#.", "...#.", "...#.", "#..#.", ".##.."],
    "K": ["#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"],
    "L": ["#....", "#....", "#....", "#....", "#....", "#....", "#####"],
    "M": ["#...#", "##.##", "#.#.#", "#.#.#", "#...#", "#...#", "#...#"],
    "N": ["#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#", "#...#"],
    "O": [".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
    "P": ["####.", "#...#", "#...#", "####.", "#....", "#....", "#...."],
    "Q": [".###.", "#...#", "#...#", "#...#", "#.#.#", "#..#.", ".##.#"],
    "R": ["####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"],
    "S": [".####", "#....", "#....", ".###.", "....#", "....#", "####."],
    "T": ["#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."],
    "U": ["#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
    "V": ["#...#", "#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."],
    "W": ["#...#", "#...#", "#...#", "#.#.#", "#.#.#", "##.##", "#...#"],
    "X": ["#...#", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "#...#"],
    "Y": ["#...#", "#...#", ".#.#.", "..#..", "..#..", "..#..", "..#.."],
    "Z": ["#####", "....#", "...#.", "..#..", ".#...", "#....", "#####"],
    "0": [".###.", "#...#", "#..##", "#.##.", "##..#", "#...#", ".###."],
    "1": ["..#..", ".##..", "..#..", "..#..", "..#..", "..#..", ".###."],
    "2": [".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"],
    "3": ["####.", "....#", "....#", ".###.", "....#", "....#", "####."],
    "4": ["...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."],
    "5": ["#####", "#....", "####.", "....#", "....#", "#...#", ".###."],
    "6": [".###.", "#....", "#....", "####.", "#...#", "#...#", ".###."],
    "7": ["#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."],
    "8": [".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."],
    "9": [".###.", "#...#", "#...#", ".####", "....#", "....#", ".###."],
    "!": ["..#..", "..#..", "..#..", "..#..", "..#..", ".....", "..#.."],
    "?": [".###.", "#...#", "....#", "...#.", "..#..", ".....", "..#.."],
    ".": [".....", ".....", ".....", ".....", ".....", ".##..", ".##.."],
    "-": [".....", ".....", ".....", "#####", ".....", ".....", "....."],
    ":": [".....", "..#..", "..#..", ".....", "..#..", "..#..", "....."],
    "+": [".....", "..#..", "..#..", "#####", "..#..", "..#..", "....."],
    "/": ["....#", "....#", "...#.", "..#..", ".#...", "#....", "#...."],
    "$": ["..#..", ".####", "#.#..", ".###.", "..#.#", "####.", "..#.."],
    " ": ["...", "...", "...", "...", "...", "...", "..."],
}
BLOCK_RE = re.compile(r"^(minecraft:)?[a-z0-9_]{1,50}(\[[a-z0-9_=,]{1,80}\])?$")


def _block(b, allow_empty=False):
    b = (b or "").strip().lower()
    if not b and allow_empty:
        return ""
    if not BLOCK_RE.match(b):
        raise ValueError(f"'{b}' isn't a valid block id")
    return b


def facing_vec(yaw):
    yaw %= 360
    if yaw >= 315 or yaw < 45:
        return (0, 1)
    if yaw < 135:
        return (-1, 0)
    if yaw < 225:
        return (0, -1)
    return (1, 0)


def _fill(a, b, blk):
    return f"/fill {a[0]} {a[1]} {a[2]} {b[0]} {b[1]} {b[2]} {blk}"


def sign_commands(text, block, back, scale, dist):
    text = " ".join(str(text).upper().split())[:24]
    if not text:
        raise ValueError("Type some text first")
    block, back = _block(block), _block(back, True)
    scale, dist = max(1, min(int(scale), 8)), max(2, min(int(dist), 30))
    rows = [""] * 7
    for i, ch in enumerate(text):
        g = FONT.get(ch, FONT["?"])
        gap = "." if i < len(text) - 1 else ""
        for r in range(7):
            rows[r] += g[r] + gap
    W = len(rows[0])
    pad = scale if back else 0
    total_w = W * scale
    if total_w + 2 * pad > 250:
        raise ValueError("Sign is too wide -- use shorter text or a smaller size")
    with MC:
        px, py, pz = [math.floor(v) for v in m.player_position()]
        yaw = m.player_orientation()[0]
    dx, dz = facing_vec(yaw)
    rx, rz = -dz, dx
    ox, oz = px + dx * dist, pz + dz * dist
    total_h = 7 * scale + 2 * pad
    if py + total_h > 318:
        raise ValueError("Sign would go above the build limit")
    left = -(total_w // 2)

    def P(cx, cy):
        return (ox + rx * cx, py + cy, oz + rz * cx)

    cmds = []
    if back:
        cmds.append(_fill(P(left - pad, 0), P(left + total_w + pad - 1, total_h - 1), back))
    for r, row in enumerate(rows):
        ylow = pad + (6 - r) * scale
        yhigh = ylow + scale - 1
        c = 0
        while c < W:
            if row[c] == "#":
                c0 = c
                while c < W and row[c] == "#":
                    c += 1
                cmds.append(_fill(P(left + c0 * scale, ylow), P(left + c * scale - 1, yhigh), block))
            else:
                c += 1
    return cmds


def shape_commands(kind, block, w, h, d):
    block = _block(block)
    w, h, d = [max(1, min(int(v), 32)) for v in (w, h, d)]
    with MC:
        px, py, pz = [math.floor(v) for v in m.player_position()]
        yaw = m.player_orientation()[0]
    if kind == "platform":
        x1, z1 = px - w // 2, pz - d // 2
        return [f"/fill {x1} {py - 1} {z1} {x1 + w - 1} {py - 1} {z1 + d - 1} {block}"]
    if kind == "room":
        h = max(h, 3)
        x1, z1 = px - w // 2, pz - d // 2
        return [f"/fill {x1} {py - 1} {z1} {x1 + w - 1} {py - 2 + h} {z1 + d - 1} {block} hollow"]
    if kind == "wall":
        dx, dz = facing_vec(yaw)
        rx, rz = -dz, dx
        ox, oz, l = px + dx * 3, pz + dz * 3, -(w // 2)
        return [_fill((ox + rx * l, py, oz + rz * l), (ox + rx * (l + w - 1), py + h - 1, oz + rz * (l + w - 1)), block)]
    raise ValueError("unknown shape")


def run_cmds(cmds, label):
    try:
        broadcast({"type": "info", "msg": f"{label}: sending {len(cmds)} command(s)"})
        for c in cmds:
            with MC:
                m.execute(c)
            time.sleep(0.06)
        broadcast({"type": "info", "msg": f"{label}: done"})
    except Exception as e:
        broadcast({"type": "info", "msg": f"{label} failed: {e}"})


# ----------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    server_version = "MinescriptBridge"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authed(self, qs):
        host = self.headers.get("Host", "")
        host = host.rsplit(":", 1)[0] if ":" in host else host
        host = host.strip("[]")
        if host not in ("127.0.0.1", "localhost", "0.0.0.0", "::1", "") and not host.startswith("127."):
            self._why = f"wrong address '{host}' -- use 127.0.0.1:{PORT}"
            return False
        tok = self.headers.get("X-Token") or (qs.get("t") or [""])[0]
        if not tok:
            self._why = "no token in the link (it must end with ?t=<token>)"
            return False
        try:
            good = secrets.compare_digest(tok.encode(), TOKEN.encode())
        except Exception:
            good = False
        if not good:
            self._why = "token doesn't match the running menu (stale link?)"
            return False
        return True

    def _deny(self, page):
        why = getattr(self, "_why", "forbidden")
        global LAST_DENY
        if time.time() - LAST_DENY > 5:
            LAST_DENY = time.time()
            try:
                m.echo(f"[menu] blocked a browser request: {why}")
            except Exception:
                pass
        if page:
            body = ("<body style='font:16px system-ui;background:#0d1117;color:#e6edf3;padding:30px'>"
                    "<h2>Menu says no</h2><p>Reason: <b>" + why.replace("<", "&lt;") + "</b></p>"
                    "<p>Use the link from the <code>[menu] running -&gt;</code> line in Minecraft chat.</p>").encode()
            self.send_response(403)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._json(403, {"error": why})

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if not self._authed(qs):
            return self._deny(u.path == "/")
        try:
            if u.path == "/":
                body = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/events":
                return self._sse()
            elif u.path == "/api/status":
                self._json(200, status())
            elif u.path == "/api/config":
                self._json(200, public_cfg())
            elif u.path == "/api/scripts":
                self._json(200, {"scripts": list_scripts()})
            elif u.path == "/api/script":
                n = (qs.get("name") or [""])[0]
                p = script_path(n)
                if not os.path.isfile(p):
                    return self._json(404, {"error": "no such script"})
                with open(p, encoding="utf-8") as f:
                    self._json(200, {"name": safe_name(n), "code": f.read()})
            else:
                self._json(404, {"error": "not found"})
        except Exception as e:
            self._json(500, {"error": str(e)})

    def do_POST(self):
        u = urlparse(self.path)
        if not self._authed({}):
            return self._deny(False)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 300_000:
                return self._json(413, {"error": "too large"})
            d = json.loads(self.rfile.read(n) or b"{}")
            p = u.path
            if p == "/api/send":
                send(str(d.get("text", "")))
            elif p == "/api/script/save":
                with open(script_path(d["name"]), "w", encoding="utf-8") as f:
                    f.write(str(d.get("code", "")))
            elif p == "/api/script/run":
                if "code" in d:
                    with open(script_path(d["name"]), "w", encoding="utf-8") as f:
                        f.write(str(d["code"]))
                if not os.path.isfile(script_path(d["name"])):
                    return self._json(404, {"error": "save the script first"})
                run_script(d["name"])
            elif p == "/api/script/delete":
                if os.path.isfile(script_path(d["name"])):
                    os.remove(script_path(d["name"]))
            elif p == "/api/scan":
                require_tools()
                if SCAN["running"]:
                    return self._json(409, {"error": "scan already running"})
                r = max(4, min(int(d.get("radius", 24)), 32))
                up, down = max(0, min(int(d.get("up", 16)), 48)), max(0, min(int(d.get("down", 32)), 48))
                wanted = set(d.get("kinds") or [])
                threading.Thread(target=scan_ores, args=(r, up, down, wanted), daemon=True).start()
            elif p == "/api/scan/cancel":
                SCAN["cancel"] = True
            elif p == "/api/look":
                require_tools()
                look_at(float(d["x"]), float(d["y"]), float(d["z"]))
            elif p == "/api/hold":
                require_tools()
                hold_key(str(d["key"]), d.get("on"))
            elif p == "/api/opcmd":
                require_tools()
                send(str(d.get("text", "")))
            elif p == "/api/parse":
                r = parse_tp(str(d.get("line", ""))[:300])
                if r:
                    ok, why = auto_check(r)
                    auto = ("YES - " if ok else "no - ") + why
                else:
                    auto = "no - couldn't find a player name plus tpa/tp in that line"
                return self._json(200, {"parsed": r and {k: r[k] for k in ("name", "kind", "via", "slash")},
                                        "auto": auto, "enabled": auto_enabled()})
            elif p == "/api/config":
                update_cfg(d)
                return self._json(200, public_cfg())
            elif p == "/api/webhook/test":
                discord_post("Minescript Menu is connected.")
            elif p in ("/api/market/search", "/api/market/read"):
                if p.endswith("read"):
                    args = (None, 0, None, False)
                else:
                    query = " ".join(str(d.get("query", "")).split())[:40]
                    wait = max(0.5, min(float(d.get("wait", 2)), 10))
                    raw_alert = d.get("alert")
                    alert = None if raw_alert in (None, "") else parse_amount(raw_alert)
                    args = (query, wait, alert, alert is not None)
                threading.Thread(target=market_job, args=args, daemon=True).start()
            elif p == "/api/market/post":
                rows = MARKET["last"][:10]
                if not rows:
                    raise ValueError("Nothing to send yet -- run a search first")
                discord_post(format_rows(f"AH prices: {MARKET['query'] or 'open screen'}", rows))
            elif p == "/api/sign":
                require_tools()
                cmds = sign_commands(d.get("text", ""), d.get("block", ""), d.get("back", ""),
                                     d.get("scale", 2), d.get("dist", 4))
                threading.Thread(target=run_cmds, args=(cmds, "sign"), daemon=True).start()
            elif p == "/api/build":
                require_tools()
                cmds = shape_commands(str(d.get("kind", "")), d.get("block", ""), d.get("w", 9), d.get("h", 5), d.get("d", 9))
                threading.Thread(target=run_cmds, args=(cmds, "build"), daemon=True).start()
            elif p == "/api/shutdown":
                self._json(200, {"ok": True})
                threading.Timer(0.3, lambda: os._exit(0)).start()
                return
            else:
                return self._json(404, {"error": "not found"})
            self._json(200, {"ok": True})
        except PermissionError as e:
            self._json(403, {"error": str(e)})
        except Exception as e:
            self._json(500, {"error": f"{type(e).__name__}: {e}"})

    def _sse(self):
        q = queue.Queue(maxsize=2000)
        with CLIENTS_LOCK:
            backlog = list(HISTORY)
            CLIENTS.add(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            for ev in backlog:
                self.wfile.write(b"data: " + json.dumps(ev).encode() + b"\n\n")
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=15)
                    self.wfile.write(b"data: " + json.dumps(ev).encode() + b"\n\n")
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with CLIENTS_LOCK:
                CLIENTS.discard(q)


def load_token():
    try:
        with open(TOKEN_FILE) as f:
            t = f.read().strip()
            if len(t) >= 16:
                return t
    except OSError:
        pass
    t = secrets.token_urlsafe(18)
    try:
        with open(TOKEN_FILE, "w") as f:
            f.write(t)
    except OSError:
        pass
    return t


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Minescript Menu</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0d1117;--p:#161b22;--p2:#1f2630;--b:#2d3642;--t:#e6edf3;--m:#8b98a8;--a:#58e6a9;--a2:#7c9cff;--r:#ff6b6b;--y:#ffd166}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--t);font:14px/1.45 system-ui,Segoe UI,sans-serif;height:100vh;display:flex;flex-direction:column}
header{display:flex;align-items:center;gap:14px;padding:10px 16px;background:var(--p);border-bottom:1px solid var(--b)}
header h1{font-size:16px;margin:0;background:linear-gradient(90deg,var(--a),var(--a2));-webkit-background-clip:text;color:transparent}
.dot{width:10px;height:10px;border-radius:50%;background:var(--r)}.dot.on{background:var(--a);box-shadow:0 0 8px var(--a)}
nav{display:flex;gap:4px;margin-left:12px}nav button{background:none;border:0;color:var(--m);padding:8px 14px;border-radius:8px;cursor:pointer;font-size:14px}
nav button.on{background:var(--p2);color:var(--t)}#pos{margin-left:auto;color:var(--m);font-family:monospace}
main{flex:1;min-height:0;position:relative}.tab{display:none;position:absolute;inset:0;padding:14px;overflow:auto}.tab.on{display:flex;flex-direction:column;gap:10px}
button.b,select,input,textarea{background:var(--p2);color:var(--t);border:1px solid var(--b);border-radius:8px;padding:7px 11px;font:inherit}
button.b{cursor:pointer}button.b:hover{border-color:var(--a)}button.pri{background:var(--a);color:#062;border-color:var(--a);font-weight:600}
button.dng{border-color:var(--r);color:var(--r)}
#log{flex:1;min-height:0;overflow:auto;background:var(--p);border:1px solid var(--b);border-radius:10px;padding:8px 10px;font-family:Consolas,monospace;font-size:13px}
#log div{padding:1px 0;white-space:pre-wrap;word-break:break-word}#log .ts{color:var(--m);margin-right:8px}#log .sent{color:var(--a2)}#log .info{color:var(--y)}#log .tp{color:var(--a)}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.row input[type=text],.row input:not([type]){flex:1;min-width:120px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px}
.card{background:var(--p);border:1px solid var(--b);border-radius:10px;padding:10px 12px;cursor:pointer}.card:hover{border-color:var(--a)}
.card b{font-size:15px}.card small{color:var(--m);display:block;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tag{display:inline-block;background:var(--p2);border-radius:6px;padding:0 6px;margin-left:6px;font-size:11px;color:var(--a)}
.panel{background:var(--p);border:1px solid var(--b);border-radius:10px;padding:12px}.warn{background:#3a2a10;border:1px solid var(--y);color:var(--y);padding:10px;border-radius:10px}
#modal{display:none;position:fixed;inset:0;background:#000a;align-items:center;justify-content:center;z-index:9}#modal.on{display:flex}
#mbox{background:var(--p);border:1px solid var(--a);border-radius:14px;padding:18px;width:min(460px,92vw)}
#mbox h2{margin:0 0 4px}#stats{margin-top:10px;max-height:180px;overflow:auto;background:var(--bg);border-radius:8px;padding:8px;font-family:monospace;font-size:12px;white-space:pre-wrap;display:none}
textarea#code{flex:1;min-height:260px;font-family:Consolas,monospace;font-size:13px;resize:none;tab-size:4}
table{border-collapse:collapse;width:100%}td,th{padding:3px 8px;text-align:left;border-bottom:1px solid var(--b)}tr.r{cursor:pointer}tr.r:hover{background:var(--p2)}
.scroll{overflow:auto;max-height:300px}.cols{display:flex;gap:12px;flex-wrap:wrap}.cols>*{flex:1;min-width:280px}
label.k{display:inline-flex;gap:4px;align-items:center;margin-right:8px}
</style></head><body>
<header><h1>MINESCRIPT MENU</h1><span class="dot" id="dot"></span><span id="conn" style="color:var(--m)">connecting</span>
<nav id="nav"></nav><span id="pos"></span></header>
<main>
<section class="tab on" id="t-chat"><div id="log"></div>
<div class="row"><input id="cin" placeholder="Type chat or a /command  (Enter to send, Up = history)" autocomplete="off"><button class="b pri" id="csend">Send</button></div></section>

<section class="tab" id="t-donut">
<div class="panel"><div class="row"><b>Quick</b><span class="row" id="quick2"></span></div>
<div class="row" style="margin-top:8px"><input id="rtpreg" placeholder="rtp region (optional)" style="max-width:210px"><button class="b" id="rtpgo">/rtp</button><button class="b" id="qadd">+ add button</button></div></div>
<div class="panel"><div class="row"><label class="k"><input type="checkbox" id="autoon"><b>Auto /tpahere</b>&nbsp;when someone says tpa / tp</label>
<span style="color:var(--m)">per-player cooldown</span><input id="autocd" type="number" value="120" style="width:80px">s <span style="color:var(--m)">max per min</span><input id="autopm" type="number" value="4" style="width:70px"><label class="k"><input type="checkbox" id="autostrict" checked>short messages only</label></div>
<div class="row" style="margin-top:8px"><input id="autoign" placeholder="never auto-tpahere these names (comma separated)"><button class="b" id="autosave">Save</button></div></div>
<div class="panel"><b>Test a chat line</b> <span style="color:var(--m)">paste a real line from chat and see how the menu reads it</span>
<div class="row" style="margin-top:8px"><input id="ptest" placeholder="e.g.  Steve » tpa"><button class="b" id="ptestgo">Test</button></div><div id="pres" style="margin-top:8px;font-family:monospace;white-space:pre-wrap"></div></div>
<div class="panel"><div class="row"><b>Teleport requests &amp; tp talk</b><input id="dfilter" placeholder="filter name"><button class="b" id="dclear">Clear</button>
<button class="b" id="dacc">/tpaccept</button><button class="b" id="dden">/tpdeny</button></div>
<div class="grid" id="dgrid"></div><div style="color:var(--m)">Click a name for actions. Shows players who say tpa / tp / tpahere in chat and incoming request messages.</div></div></section>

<section class="tab" id="t-scripts"><div class="row"><select id="slist"></select><input id="sname" placeholder="script name"><button class="b" id="sload">Load</button>
<button class="b" id="ssave">Save</button><button class="b pri" id="srun">Save &amp; Run</button><button class="b dng" id="sdel">Delete</button></div>
<textarea id="code" spellcheck="false"></textarea>
<div style="color:var(--m)">Saved as <code>web_&lt;name&gt;.py</code> in your minescript folder and started with <code>\web_&lt;name&gt;</code>. Script output appears in Minecraft chat. Stop scripts in-game with /stop or /kill when needed.</div></section>

<section class="tab" id="t-market"><div class="panel"><b>Auction house price check</b>
<div class="row" style="margin:8px 0"><input id="mq" placeholder="search e.g. diamond" style="max-width:240px"><span style="color:var(--m)">wait</span><input id="mwait" type="number" value="2" style="width:70px"><span style="color:var(--m)">alert if &le;</span><input id="malert" placeholder="price e.g. 500 or 2k" style="width:150px"></div>
<div class="row"><button class="b pri" id="msearch">/ah search + read</button><button class="b" id="mread">Read open screen</button><button class="b" id="mpost">Send top 10 to Discord</button><span id="mstat" style="color:var(--m)"></span></div>
<div style="color:var(--m);margin-top:6px">Read-only: it never clicks or buys anything. Sorted cheapest per item first.</div></div>
<div class="panel"><b>Discord webhook</b><div class="row" style="margin-top:8px"><input id="hook" type="password" placeholder="https://discord.com/api/webhooks/..." autocomplete="off"><button class="b" id="hooksave">Save</button><button class="b" id="hooktest">Test</button><span id="hookstate" style="color:var(--m)"></span></div></div>
<div class="panel scroll" style="max-height:none"><table id="mtab"><thead><tr><th>Item</th><th>Qty</th><th>Price</th><th>Each</th><th>Seller</th><th>Info</th></tr></thead><tbody></tbody></table></div>
<details class="panel"><summary>Debug: raw item data (send me this if prices don't parse)</summary><pre id="mdebug" style="white-space:pre-wrap;word-break:break-all;font-size:12px"></pre></details></section>

<section class="tab" id="t-tools"><div id="lock" class="warn" style="display:none"></div>
<div class="cols"><div class="panel"><b>Ore scanner (x-ray list)</b>
<div class="row" style="margin:8px 0">Radius <input id="rad" type="number" value="24" min="4" max="32" style="width:70px"> Up <input id="up" type="number" value="16" style="width:70px"> Down <input id="down" type="number" value="32" style="width:70px"></div>
<div id="kinds" style="margin-bottom:8px"></div><div class="row"><button class="b pri" id="scan">Scan</button><button class="b" id="scancel">Cancel</button><span id="scanmsg" style="color:var(--m)"></span></div></div>
<div class="panel"><b>Movement holds</b><div class="row" style="margin:8px 0" id="holds"></div><b>Op quick commands</b><div class="row" style="margin-top:8px" id="quick"></div></div></div>
<div class="cols"><div class="panel"><canvas id="map" width="360" height="360" style="width:100%;max-width:420px;background:#0a0e13;border-radius:8px"></canvas></div>
<div class="panel scroll"><table id="ores"><tbody></tbody></table></div></div>
<div class="cols"><div class="panel"><b>Sign builder (creative / op)</b>
<div class="row" style="margin:8px 0"><input id="stext" value="HELLO" maxlength="24" placeholder="text"><input id="sblock" value="white_concrete" placeholder="letter block"><input id="sback" value="black_concrete" placeholder="background block"></div>
<div class="row"><span>size</span><input id="sscale" type="number" value="2" min="1" max="8" style="width:60px"><span>distance</span><input id="sdist" type="number" value="4" min="2" max="30" style="width:70px"><button class="b pri" id="sbuild">Build Sign</button></div></div>
<div class="panel"><b>Quick fills</b><div class="row" style="margin:8px 0"><select id="shape"><option>platform</option><option>wall</option><option>room</option></select><input id="fblock" value="stone" placeholder="block"><input id="fw" type="number" value="9" style="width:60px">&times;<input id="fh" type="number" value="5" style="width:60px">&times;<input id="fd" type="number" value="9" style="width:60px"><button class="b pri" id="fbuild">Build</button></div>
<div style="color:var(--m)">width &times; height &times; depth, max 32 each.</div></div></div></section>
</main>
<div id="modal"><div id="mbox"><h2 id="mname"></h2><div id="msub" style="color:var(--m);margin-bottom:12px"></div>
<div class="row"><button class="b pri" id="a-tpahere">/tpahere</button><button class="b" id="a-tpa">/tpa</button><button class="b" id="a-stats">/stats</button><button class="b" id="a-msg">/msg</button></div>
<div class="row" style="margin-top:10px"><input id="payamt" placeholder="amount e.g. 500 or 2k" style="max-width:190px"><button class="b" id="a-pay">/pay</button></div>
<div id="stats"></div><div class="row" style="margin-top:12px"><button class="b" id="mclose">Close</button></div></div></div>
<script>
const T=new URLSearchParams(location.search).get('t')||'';
const $=id=>document.getElementById(id);
async function api(p,b){const r=await fetch(p,{method:b?'POST':'GET',headers:{'X-Token':T,'Content-Type':'application/json'},body:b?JSON.stringify(b):undefined});
  const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||('HTTP '+r.status));return j}
const say=t=>api('/api/send',{text:t}).catch(e=>note('Error: '+e.message));
function strip(s){return String(s).replace(/\u00a7./g,'')}
const tabs=[['chat','Chat'],['donut','DonutSMP'],['scripts','Scripts'],['market','Market'],['tools','Tools']];
tabs.forEach(([id,n])=>{const b=document.createElement('button');b.textContent=n;b.onclick=()=>show(id);b.dataset.id=id;$('nav').appendChild(b)});
function show(id){document.querySelectorAll('.tab').forEach(e=>e.classList.toggle('on',e.id==='t-'+id));document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('on',b.dataset.id===id));
  if(id==='scripts')loadList();if(id==='tools')loadStatus();if(id==='donut'||id==='market')loadCfg().catch(()=>{})}
show('chat');
const log=$('log');let hist=[],hi=0;
function line(text,cls,ts){const d=document.createElement('div');if(cls)d.className=cls;const s=document.createElement('span');s.className='ts';
  s.textContent=new Date((ts||Date.now()/1000)*1000).toLocaleTimeString();d.appendChild(s);d.appendChild(document.createTextNode(text));
  const stick=log.scrollTop+log.clientHeight>=log.scrollHeight-30;log.appendChild(d);while(log.childElementCount>1500)log.firstChild.remove();if(stick)log.scrollTop=log.scrollHeight}
function note(t){line(t,'info')}
function submit(){const v=$('cin').value;if(!v.trim())return;hist.push(v);hi=hist.length;$('cin').value='';say(v)}
$('csend').onclick=submit;$('cin').onkeydown=e=>{if(e.key==='Enter')submit();else if(e.key==='ArrowUp'&&hi>0){$('cin').value=hist[--hi]}else if(e.key==='ArrowDown'){hi=Math.min(hi+1,hist.length);$('cin').value=hist[hi]||''}}
const reqs=new Map();let cur=null,capture=null;
function renderDonut(){const g=$('dgrid');g.textContent='';const f=$('dfilter').value.toLowerCase();
  [...reqs.values()].sort((a,b)=>b.ts-a.ts).filter(r=>r.name.toLowerCase().includes(f)).forEach(r=>{
    const c=document.createElement('div');c.className='card';const b=document.createElement('b');b.textContent=r.name;const t=document.createElement('span');t.className='tag';t.textContent=r.kind+' x';
    const s=document.createElement('small');s.textContent=r.text;const s2=document.createElement('small');s2.textContent=new Date(r.ts*1000).toLocaleTimeString();
    c.append(b,t,s,s2);c.onclick=()=>openModal(r);g.appendChild(c)})}
function openModal(r){cur=r;$('mname').textContent=r.name;$('msub').textContent=r.text;$('stats').style.display='none';$('stats').textContent='';$('modal').classList.add('on')}
$('mclose').onclick=()=>$('modal').classList.remove('on');$('modal').onclick=e=>{if(e.target.id==='modal')$('modal').classList.remove('on')};
$('a-tpahere').onclick=()=>say('/tpahere '+cur.name);$('a-tpa').onclick=()=>say('/tpa '+cur.name);
$('a-msg').onclick=()=>{const t=prompt('Message to '+cur.name);if(t)say('/msg '+cur.name+' '+t)};
$('a-stats').onclick=()=>{capture={until:Date.now()+5000};const s=$('stats');s.style.display='block';s.textContent='Checking ...';say('/stats '+cur.name)};
$('a-pay').onclick=()=>{const a=$('payamt').value.trim();if(!/^\d+(\.\d+)?[kKmMbB]?$/.test(a)){alert('Enter an amount like 500 or 2k');return}
  if(confirm('Send $'+a+' to '+cur.name+'?'))say('/pay '+cur.name+' '+a)};
$('dclear').onclick=()=>{reqs.clear();renderDonut()};$('dfilter').oninput=renderDonut;$('dacc').onclick=()=>say('/tpaccept');$('dden').onclick=()=>say('/tpdeny');
const tmpl='import minescript as m\n\nm.echo("hello from the browser!")\nx, y, z = m.player_position()\nm.echo(f"You are at {x:.1f} {y:.1f} {z:.1f}")\n';
$('code').value=tmpl;
async function loadList(){const j=await api('/api/scripts');const s=$('slist');s.textContent='';j.scripts.forEach(n=>{const o=document.createElement('option');o.textContent=n;s.appendChild(o)})}
$('sload').onclick=async()=>{const n=$('slist').value;if(!n)return;const j=await api('/api/script?name='+encodeURIComponent(n));$('code').value=j.code;$('sname').value=j.name};
const sn=()=>$('sname').value.trim();
$('ssave').onclick=async()=>{if(!sn())return alert('Name it first');await api('/api/script/save',{name:sn(),code:$('code').value});note('saved '+sn());loadList()};
$('srun').onclick=async()=>{if(!sn())return alert('Name it first');try{await api('/api/script/run',{name:sn(),code:$('code').value});loadList()}catch(e){note('Error: '+e.message)}};
$('sdel').onclick=async()=>{const n=$('slist').value;if(n&&confirm('Delete web_'+n+'.py?')){await api('/api/script/delete',{name:n});loadList()}};
$('code').onkeydown=e=>{if(e.key==='Tab'){e.preventDefault();const t=e.target,s=t.selectionStart;t.value=t.value.slice(0,s)+'    '+t.value.slice(t.selectionEnd);t.selectionStart=t.selectionEnd=s+4}};
const KINDS=['coal','iron','copper','gold','redstone','lapis','diamond','emerald','quartz','ancient_debris'];
KINDS.forEach(k=>{const l=document.createElement('label');l.className='k';const c=document.createElement('input');c.type='checkbox';c.value=k;c.checked=['diamond','emerald','ancient_debris','gold'].includes(k);const n=document.createElement('span');n.textContent=k;l.append(c,n);$('kinds').appendChild(l)});
[['forward','Forward'],['sprint','Sprint'],['jump','Jump'],['sneak','Sneak']].forEach(([k,n])=>{const l=document.createElement('label');l.className='k';const c=document.createElement('input');c.type='checkbox';c.value=k;l.append(c,n);$('holds').appendChild(l);c.onchange=()=>api('/api/hold',{key:k,on:c.checked}).catch(e=>{note('Error: '+e.message);c.checked=false})});
[['Creative (fly)','/gamemode creative'],['Survival','/gamemode survival'],['Day','/time set day'],['Night','/time set night'],['Clear weather','/weather clear'],['Float (0 gravity)','/attribute @s minecraft:generic.gravity base set 0']].forEach(([n,c])=>{const b=document.createElement('button');b.className='b';b.textContent=n;b.onclick=()=>api('/api/opcmd',{text:c}).catch(e=>note('Error: '+e.message));$('quick').appendChild(b)});
async function loadStatus(){try{const s=await api('/api/status');if(s.pos)$('pos').textContent=s.pos.join(' ');const w=$('lock');w.style.display=s.tools?'none':'block';w.textContent=s.tools?'':'Tools are locked on this server. Set FORCE_TOOLS_UNLOCK = True in menu.py or use a test host.';
  document.querySelectorAll('#t-tools button,#t-tools input,#t-tools select').forEach(e=>e.disabled=!s.tools)}catch(e){}}
setInterval(()=>{if(document.hidden)return;api('/api/status').then(s=>{if(s.pos)$('pos').textContent=s.pos.join(' ')}).catch(()=>{})},4000);
$('scan').onclick=()=>{const kinds=[...document.querySelectorAll('#kinds input:checked')].map(c=>c.value);$('scanmsg').textContent='scanning...';
  api('/api/scan',{radius:+$('rad').value,up:+$('up').value,down:+$('down').value,kinds}).catch(e=>{$('scanmsg').textContent=e.message})};
$('scancel').onclick=()=>api('/api/scan/cancel',{});
function drawScan(e){const rs=e.results,R=e.radius,cv=$('map'),g=cv.getContext('2d'),S=cv.width,px=S/(2*R+1);g.clearRect(0,0,S,S);g.strokeStyle='#1c2531';
  for(let i=0;i<=2*R+1;i+=8){g.beginPath();g.moveTo(i*px,0);g.lineTo(i*px,S);g.moveTo(0,i*px);g.lineTo(S,i*px);g.stroke()}
  rs.forEach(r=>{g.fillStyle=e.colors[r.kind]||'#aaa';g.fillRect((r.x-e.origin[0]+R)*px,(r.z-e.origin[2]+R)*px,Math.max(3,px),Math.max(3,px))});
  g.fillStyle='#fff';g.fillRect(R*px-2,R*px-2,5,5);
  const tb=$('ores').firstChild||$('ores').appendChild(document.createElement('tbody'));tb.textContent='';
  rs.slice(0,300).forEach(r=>{const tr=document.createElement('tr');tr.className='r';[r.kind,r.x+' '+r.y+' '+r.z,r.dist+'m'].forEach((v,i)=>{const td=document.createElement('td');td.textContent=v;if(i===2)td.style.color='#a7f3d0';tr.appendChild(td)});tr.onclick=()=>api('/api/look',r).catch(x=>note('Error: '+x.message));tb.appendChild(tr)});
  $('scanmsg').textContent=rs.length+' found (click a row to look at it; map is top-down)'}
let CFG={quick:[],auto:{},webhook_set:false};
function renderCfg(){const box=$('quick2');box.textContent='';
  CFG.quick.forEach((q,i)=>{const b=document.createElement('button');b.className='b';b.textContent=q.label;b.title=q.cmd;b.onclick=()=>say(q.cmd);
    b.oncontextmenu=e=>{e.preventDefault();if(confirm('Remove button "'+q.label+'"?')){const n=CFG.quick.slice();n.splice(i,1);saveCfg({quick:n})}};box.appendChild(b)});
  const a=CFG.auto||{};$('autoon').checked=!!a.enabled;$('autocd').value=a.cooldown||120;$('autopm').value=a.per_min||4;$('autostrict').checked=a.strict!==false;$('autoign').value=(a.ignore||[]).join(', ');
  $('hookstate').textContent=CFG.webhook_set?('saved (ends ...'+CFG.webhook_hint+')'):'not set'}
async function loadCfg(){CFG=await api('/api/config');renderCfg()}
async function saveCfg(p){try{CFG=await api('/api/config',p);renderCfg();return true}catch(e){note('Error: '+e.message);return false}}
$('rtpgo').onclick=()=>{const r=$('rtpreg').value.trim();say('/rtp'+(r?' '+r:''))};
$('qadd').onclick=()=>{const l=prompt('Button label');if(!l)return;const c=prompt('Command, e.g. /warp farm');if(!c)return;saveCfg({quick:CFG.quick.concat([{label:l.slice(0,20),cmd:c.slice(0,100)}])})};
function autoPayload(){return {auto:{enabled:$('autoon').checked,cooldown:+$('autocd').value||120,per_min:+$('autopm').value||4,strict:$('autostrict').checked,ignore:$('autoign').value.split(',').map(x=>x.trim()).filter(Boolean)}}}
$('autoon').onchange=async()=>{if($('autoon').checked&&!confirm('Auto /tpahere sends a command by itself when someone says tpa or tp. Some servers count that as a macro, so check the rules first. Turn it on only if your server allows it.')){$('autoon').checked=false;return}
  if(await saveCfg(autoPayload()))note($('autoon').checked?'auto /tpahere is ON':'auto /tpahere is OFF')};
$('autosave').onclick=()=>saveCfg(autoPayload());
$('hooksave').onclick=async()=>{const v=$('hook').value.trim();if(await saveCfg({webhook:v}))$('hook').value=''};
$('hooktest').onclick=()=>api('/api/webhook/test',{}).then(()=>{$('hookstate').textContent='test message sent'}).catch(e=>{$('hookstate').textContent=e.message});
const fmt=n=>n==null?'?':Number(n).toLocaleString(undefined,{maximumFractionDigits:2});
function drawMarket(e){const tb=$('mtab').tBodies[0];tb.textContent='';
  (e.items||[]).forEach(r=>{const tr=document.createElement('tr');[r.name||r.item,'x'+r.count,r.price==null?'?':'$'+fmt(r.price),r.unit==null?'?':'$'+fmt(r.unit),r.seller||'',r.info||''].forEach(v=>{const td=document.createElement('td');td.textContent=v;tr.appendChild(td)});tb.appendChild(tr)});
  $('mdebug').textContent=(e.debug||[]).join('\n\n');
  $('mstat').textContent=e.error?e.error:((e.items||[]).length+' items'+(e.query?' for "'+e.query+'"':''))}
const mErr=e=>{$('mstat').textContent=e.message};
$('msearch').onclick=()=>{$('mstat').textContent='searching...';api('/api/market/search',{query:$('mq').value.trim(),wait:+$('mwait').value||2,alert:$('malert').value.trim()}).catch(mErr)};
$('mread').onclick=()=>{$('mstat').textContent='reading...';api('/api/market/read',{}).catch(mErr)};
$('mpost').onclick=()=>api('/api/market/post',{}).then(()=>{$('mstat').textContent='sent to Discord'}).catch(mErr);
$('sbuild').onclick=()=>api('/api/sign',{text:$('stext').value,block:$('sblock').value,back:$('sback').value,scale:+$('sscale').value,dist:+$('sdist').value}).then(()=>note('building sign...')).catch(e=>note('Error: '+e.message));
$('fbuild').onclick=()=>api('/api/build',{kind:$('shape').value,block:$('fblock').value,w:+$('fw').value,h:+$('fh').value,d:+$('fd').value}).then(()=>note('building...')).catch(e=>note('Error: '+e.message));
$('ptestgo').onclick=async()=>{try{const j=await api('/api/parse',{line:$('ptest').value});
  $('pres').textContent=(j.parsed?('player: '+j.parsed.name+'\nkind:   '+j.parsed.kind+' ('+j.parsed.via+(j.parsed.slash?', has slash':'')+')\n'):'not read as a tpa/tp message\n')+'auto:   '+j.auto+(j.enabled?' (enabled)': ' (disabled)')}
catch(e){$('pres').textContent='Error: '+e.message}};
$('ptest').onkeydown=e=>{if(e.key==='Enter')$('ptestgo').click()};
function reset(){log.textContent='';reqs.clear();renderDonut()}
function connect(){const es=new EventSource('/events?t='+encodeURIComponent(T));
  es.onopen=()=>{reset();$('dot').classList.add('on');$('conn').textContent='live'};
  es.onerror=()=>{$('dot').classList.remove('on');$('conn').textContent='reconnecting'};
  es.onmessage=m=>{const e=JSON.parse(m.data);
    if(e.type==='chat'){const t=strip(e.msg);line(t,'',e.t);if(capture&&Date.now()<capture.until){const s=$('stats');s.textContent += t+'\n';}}
    else if(e.type==='sent')line('> '+e.msg,'sent',e.t);
    else if(e.type==='info'){line(e.msg,'info',e.t);if(/scan/.test(e.msg))$('scanmsg').textContent=e.msg}
    else if(e.type==='tp'){const o=reqs.get(e.name)||{name:e.name,n:0};o.n++;o.kind=e.kind;o.text=e.text;o.ts=e.t;reqs.set(e.name,o);renderDonut()}
    else if(e.type==='scan')drawScan(e);else if(e.type==='market')drawMarket(e)}}
loadCfg().catch(()=>{});
connect();
</script></body></html>
"""


def main():
    global TOKEN, CFG
    TOKEN = load_token()
    CFG = load_cfg()
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        m.echo(f"[menu] port {PORT} is busy -- is the menu already running? Try: \\menu {PORT + 1}")
        return
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{PORT}/?t={TOKEN}"
    m.echo(f"[menu] running -> {url}")
    if AUTO_OPEN_BROWSER:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    m.echo("[menu] stop it with \\jobs then \\killjob <id>")
    m.echo(f"[menu] chat source: {CHAT_SOURCE}")
    if CHAT_SOURCE in ("both", "log"):
        threading.Thread(target=log_tail_loop, daemon=True).start()
    try:
        if CHAT_SOURCE == "log":
            while True:
                time.sleep(3600)
        else:
            chat_loop()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
