"""Mesures système (CPU, température, mémoire, réseau, disques) servies en local.

Tout est lu dans /proc et /sys : aucune dépendance. Un petit serveur web local sert
la page du panneau (hud/stats.html) et les mesures en JSON (/api/stats), mises à
jour chaque seconde.

  python3 stats.py            # test seul : http://127.0.0.1:8765/stats.html
"""
import glob
import json
import re
import os
import shutil
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import calendar_ics
import feeds
import gcal

HUD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hud")
MOUNTS = ("/", "/home")
CPU_SENSORS = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz")


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


# --------------------------------------------------------------------------- mesures

def cpu_times():
    """(temps actif, temps total) depuis le démarrage, pour tous les cœurs."""
    for line in _read("/proc/stat").splitlines():
        if line.startswith("cpu "):
            v = [int(x) for x in line.split()[1:]]
            idle = v[3] + (v[4] if len(v) > 4 else 0)  # idle + iowait
            total = sum(v[:8])
            return total - idle, total
    return 0, 0


def core_times():
    """[(temps actif, temps total), ...] pour chaque cœur."""
    out = []
    for line in _read("/proc/stat").splitlines():
        if line.startswith("cpu") and line[3:4].isdigit():
            v = [int(x) for x in line.split()[1:]]
            idle = v[3] + (v[4] if len(v) > 4 else 0)
            total = sum(v[:8])
            out.append((total - idle, total))
    return out


def cpu_temp():
    """Température du processeur en °C, ou None si aucun capteur n'est trouvé."""
    for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        if _read(f"{hw}/name").strip() not in CPU_SENSORS:
            continue
        # « Package id 0 » (Intel) ou « Tctl » (AMD) si présent, sinon le premier capteur
        inputs = sorted(glob.glob(f"{hw}/temp*_input"))
        best = None
        for inp in inputs:
            label = _read(inp.replace("_input", "_label")).strip().lower()
            if label.startswith(("package", "tctl", "tdie")):
                best = inp
                break
        best = best or (inputs[0] if inputs else None)
        if best and _read(best).strip():
            return int(_read(best)) / 1000
    for zone in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        if _read(f"{zone}/type").strip() in ("x86_pkg_temp", "cpu-thermal", "acpitz"):
            t = _read(f"{zone}/temp").strip()
            if t:
                return int(t) / 1000
    return None


def memory():
    info = {}
    for line in _read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        if rest:
            info[key] = int(rest.split()[0]) * 1024
    total = info.get("MemTotal", 0)
    used = total - info.get("MemAvailable", info.get("MemFree", 0))
    return {"total": total, "used": used, "percent": round(100 * used / total, 1) if total else 0}


def net_bytes():
    """(octets reçus, octets envoyés) cumulés, hors boucle locale et interfaces virtuelles."""
    rx = tx = 0
    for line in _read("/proc/net/dev").splitlines()[2:]:
        name, _, data = line.partition(":")
        name = name.strip()
        if name == "lo" or name.startswith(("docker", "veth", "br-", "virbr", "tailscale")):
            continue
        v = data.split()
        if len(v) >= 9:
            rx += int(v[0])
            tx += int(v[8])
    return rx, tx


def disk_bytes():
    """(octets lus, octets écrits) cumulés, disques physiques uniquement."""
    disks = {os.path.basename(p) for p in glob.glob("/sys/block/*")
             if not os.path.basename(p).startswith(("loop", "ram", "zram", "dm-", "sr"))}
    rd = wr = 0
    for line in _read("/proc/diskstats").splitlines():
        v = line.split()
        if len(v) >= 10 and v[2] in disks:
            rd += int(v[5]) * 512
            wr += int(v[9]) * 512
    return rd, wr


def mounts():
    out = []
    seen = set()
    for m in MOUNTS:
        try:
            st = os.stat(m)
            if st.st_dev in seen:
                continue
            seen.add(st.st_dev)
            u = shutil.disk_usage(m)
            out.append({"mount": m, "total": u.total, "used": u.used, "free": u.free,
                        "percent": round(100 * u.used / u.total, 1) if u.total else 0})
        except OSError:
            pass
    return out


CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")


def process_times():
    """{pid: (nom, temps CPU en ticks, mémoire en octets)} pour tous les processus."""
    out = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        raw = _read(f"/proc/{d}/stat")
        if not raw:
            continue  # processus terminé entre-temps
        name = raw[raw.find("(") + 1:raw.rfind(")")]
        f = raw[raw.rfind(")") + 2:].split()
        if len(f) < 22:
            continue
        out[int(d)] = (name, int(f[11]) + int(f[12]), int(f[21]) * PAGE_SIZE)
    return out


def top_processes(prev, cur, dt, n=8):
    """Programmes les plus gourmands, regroupés par nom (tous les « firefox » ensemble)."""
    groups = {}
    for pid, (name, ticks, rss) in cur.items():
        g = groups.setdefault(name, {"name": name, "cpu": 0.0, "mem": 0, "count": 0})
        if pid in prev and prev[pid][0] == name:
            g["cpu"] += max(0, ticks - prev[pid][1]) / CLK_TCK / dt * 100
        g["mem"] += rss
        g["count"] += 1
    items = list(groups.values())
    for g in items:
        g["cpu"] = round(g["cpu"], 1)
    by_cpu = sorted((g for g in items if g["cpu"] > 0), key=lambda g: -g["cpu"])[:n]
    by_mem = sorted(items, key=lambda g: -g["mem"])[:n]
    return {"cpu": by_cpu, "mem": by_mem, "total": len(cur)}


def battery():
    for bat in sorted(glob.glob("/sys/class/power_supply/BAT*")):
        cap = _read(f"{bat}/capacity").strip()
        if cap:
            return {"percent": int(cap), "status": _read(f"{bat}/status").strip()}
    return None


def local_ip():
    """(adresse IP locale, interface) de la connexion par défaut, sans envoyer de paquet."""
    import socket
    ip = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))  # adresse de test : rien n'est envoyé
            ip = sock.getsockname()[0]
    except OSError:
        return None, None
    iface = None
    for line in _read("/proc/net/route").splitlines()[1:]:
        f = line.split()
        if len(f) > 1 and f[1] == "00000000":  # route par défaut
            iface = f[0]
            break
    return ip, iface


PUBLIC_IP = {"ip": None, "checked": 0.0}


def watch_public_ip(interval=300):
    """IP publique, vérifiée toutes les 5 min en arrière-plan (1 min après un échec)."""
    import urllib.request

    def loop():
        while True:
            try:
                req = urllib.request.Request("https://api.ipify.org", headers={"User-Agent": "clearmint"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    ip = r.read().decode().strip()
                if re.fullmatch(r"[0-9a-fA-F:.]{3,45}", ip):
                    PUBLIC_IP.update(ip=ip, checked=time.time())
                delay = interval
            except Exception:
                delay = 60
            time.sleep(delay)

    threading.Thread(target=loop, daemon=True).start()


def system_info():
    up = _read("/proc/uptime").split()
    load = os.getloadavg()
    distro = ""
    for line in _read("/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            distro = line.split("=", 1)[1].strip('"')
    u = os.uname()
    ip, iface = local_ip()
    return {"host": u.nodename, "kernel": u.release, "distro": distro,
            "uptime": float(up[0]) if up else 0, "load": [round(x, 2) for x in load],
            "battery": battery(), "local_ip": ip, "iface": iface, "public_ip": PUBLIC_IP["ip"]}


class Sampler(threading.Thread):
    """Mesure chaque seconde et garde le dernier relevé."""

    def __init__(self, interval=1.0):
        super().__init__(daemon=True)
        self.interval = interval
        self.latest = {}
        self.lock = threading.Lock()

    def run(self):
        prev_cpu, prev_net, prev_disk, prev_t = cpu_times(), net_bytes(), disk_bytes(), time.time()
        prev_cores = core_times()
        prev_procs, prev_procs_t, procs, tick, system = process_times(), time.time(), {}, 0, {}
        while True:
            time.sleep(self.interval)
            cpu, net, disk, now = cpu_times(), net_bytes(), disk_bytes(), time.time()
            dt = max(now - prev_t, 1e-3)
            busy, total = cpu[0] - prev_cpu[0], cpu[1] - prev_cpu[1]
            cores = core_times()
            per_core = [round(100 * (c[0] - p[0]) / (c[1] - p[1]), 1) if c[1] > p[1] else 0
                        for c, p in zip(cores, prev_cores)]
            prev_cores = cores
            sample = {
                "time": now,
                "cpu": {"percent": round(100 * busy / total, 1) if total > 0 else 0,
                        "temp": cpu_temp(), "cores": os.cpu_count(), "per_core": per_core},
                "mem": memory(),
                "net": {"down": max(0, (net[0] - prev_net[0]) / dt),
                        "up": max(0, (net[1] - prev_net[1]) / dt)},
                "disk": {"read": max(0, (disk[0] - prev_disk[0]) / dt),
                         "write": max(0, (disk[1] - prev_disk[1]) / dt),
                         "mounts": mounts()},
            }
            tick += 1
            if tick % 4 == 1:  # processus : toutes les 4 s, c'est la mesure la plus lourde
                cur = process_times()
                procs = top_processes(prev_procs, cur, max(now - prev_procs_t, 1e-3))
                prev_procs, prev_procs_t = cur, now
            sample["procs"] = procs
            if tick % 5 == 1 or not system:  # infos système : toutes les 5 s suffisent
                system = system_info()
            sample["system"] = system
            with self.lock:
                self.latest = sample
            prev_cpu, prev_net, prev_disk, prev_t = cpu, net, disk, now

    def get(self):
        with self.lock:
            return dict(self.latest)


# --------------------------------------------------------------------------- serveur

CACHE = {}           # clé -> (heure, données)
CACHE_TTL = 600      # 10 min
OPENABLE = set()     # adresses du fil qu'on accepte d'ouvrir dans le navigateur
CACHE_LOCK = threading.Lock()


def cached(key, compute, ttl=CACHE_TTL):
    """Résultat mis en cache ; en cas d'erreur, on renvoie l'ancien résultat s'il existe."""
    with CACHE_LOCK:
        hit = CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        data = compute()
    except Exception as e:
        if hit:
            return {**hit[1], "stale": True, "error": str(e)}
        return {"error": str(e)}
    with CACHE_LOCK:
        CACHE[key] = (time.time(), data)
    return data


def api_calendar(q):
    """Un ou plusieurs agendas : calendar?ics=A&ics=B (ou ics=A,B). Adresses iCal (relues
    toutes les 10 min) ou fichiers .ics locaux. Chaque événement garde le numéro de son
    agenda (« cal ») pour la couleur."""
    sources = [v.strip() for vals in q.get("ics", []) for v in vals.split(",") if v.strip()]
    use_google = q.get("google", ["0"])[0] not in ("0", "")
    if not sources and not use_google:
        return {"error": "agenda manquant : calendar?google=1 ou calendar?ics=…", "events": []}
    weeks = max(1, min(4, int(q.get("weeks", ["1"])[0] or 1)))
    start, end = calendar_ics.week_bounds(weeks=weeks, from_today=q.get("from", [""])[0] == "today")
    out = {"start": start.isoformat(), "end": end.isoformat(), "events": [], "errors": []}
    for i, ics in enumerate(sources):
        text = cached(("ics", ics), lambda ics=ics: {"text": feeds.fetch(ics, timeout=15)})
        if "text" not in text:
            out["errors"].append(f"agenda {i + 1} : {text.get('error')}")
            continue
        if text.get("stale"):
            out["stale"] = True
        events = cached(("week", ics, start.isoformat(), weeks, id(text["text"])),
                        lambda t=text["text"]: {"events": calendar_ics.events_between(t, start, end)})
        for ev in events.get("events", []):
            out["events"].append({**ev, "cal": i})
    if use_google:
        # API Google : relue toutes les 2 minutes (quasi temps réel), tâches comprises.
        tasks = q.get("tasks", ["1"])[0] != "0"
        hide = tuple(h for vals in q.get("hide", []) for h in vals.split(","))

        def google_week():
            evs, errs = gcal.events_between(start, end, with_tasks=tasks, hide=hide)
            return {"events": evs, "errors": errs}

        g = cached(("google", start.isoformat(), weeks, tasks, hide), google_week, ttl=120)
        out["events"] += g.get("events", [])
        out["errors"] += g.get("errors", [])
        if g.get("error"):
            err = g["error"]
            if "Name or service not known" in err or "Temporary failure in name resolution" in err \
                    or "Network is unreachable" in err:
                err = "pas de connexion Internet pour l'instant (nouvel essai dans 30 s)"
            out["errors"].append(f"Google : {err}")
        if g.get("stale"):
            out["stale"] = True
    out["events"].sort(key=lambda e: (e["start"][:10], not e["all_day"], e["start"]))
    if out["errors"] and not out["events"]:
        out["error"] = out["errors"][0]
    return out


def api_feed(q):
    def get(name, default=""):
        return q.get(name, [default])[0]

    key = ("feed", tuple(sorted((k, tuple(v)) for k, v in q.items())))

    def compute():
        setup_errors = []
        key = feeds.yt_api_key(get("ytkey"))
        sources = feeds.build_sources(
            hl=get("hl", "fr"), gl=get("gl", "FR"), q=get("q"), news=get("news", "1") != "0",
            yt_csv=get("yt") or None, yt_ids=get("ytids"), rss=get("rss"),
            shorts=get("shorts", "0") == "1", errors=setup_errors, yt_key=key)
        t = time.time()
        items, errors = feeds.merged(sources, limit=int(get("limit", "40") or 40),
                                     ratio=int(get("ratio", "2") or 0))
        if key and get("shorts", "0") != "1":
            items = feeds.drop_shorts(items, key)
        videos = sum(i["kind"] == "youtube" for i in items)
        print(f"[fil] {len(sources)} sources lues en {time.time() - t:.0f} s : "
              f"{len(items) - videos} actualités, {videos} vidéos, {len(setup_errors + errors)} erreurs", flush=True)
        for err in (setup_errors + errors)[:3]:
            print(f"[fil]   ✗ {err}", flush=True)
        return {"items": items, "errors": (setup_errors + errors)[:5], "sources": len(sources)}

    data = cached(key, compute)
    for item in data.get("items", []):
        OPENABLE.add(item["url"])
    return data


def api_open(q):
    url = q.get("url", [""])[0]
    if url not in OPENABLE or not url.startswith(("http://", "https://")):
        return {"ok": False}
    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return {"ok": True}


def make_handler(sampler):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path, _, query = self.path.partition("?")
            q = urllib.parse.parse_qs(query)
            apis = {"/api/calendar": api_calendar, "/api/feed": api_feed, "/api/open": api_open}
            if path == "/api/stats":
                body = json.dumps(sampler.get()).encode()
                ctype = "application/json"
            elif path in apis:
                body = json.dumps(apis[path](q), ensure_ascii=False).encode()
                ctype = "application/json; charset=utf-8"
            else:
                name = os.path.basename(path) or "stats.html"
                file = os.path.join(HUD_DIR, name)
                if not os.path.isfile(file):
                    self.send_error(404)
                    return
                with open(file, "rb") as f:
                    body = f.read()
                ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript",
                         ".css": "text/css"}.get(os.path.splitext(name)[1], "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


def start_server(port=8765, tries=20):
    """Démarre le serveur en arrière-plan ; essaie les ports suivants si occupé."""
    sampler = Sampler()
    sampler.start()
    watch_public_ip()
    for p in range(port, port + tries):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), make_handler(sampler))
        except OSError:
            continue
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{p}"
    raise OSError(f"aucun port libre entre {port} et {port + tries - 1}")


if __name__ == "__main__":
    base = start_server()
    print(f"Panneau : {base}/stats.html   Mesures : {base}/api/stats   (Ctrl+C pour quitter)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
