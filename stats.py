"""Mesures système (CPU, température, mémoire, réseau, disques) servies en local.

Tout est lu dans /proc et /sys : aucune dépendance. Un petit serveur web local sert
la page du panneau (hud/stats.html) et les mesures en JSON (/api/stats), mises à
jour chaque seconde.

  python3 stats.py            # test seul : http://127.0.0.1:8765/stats.html
"""
import glob
import json
import os
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


class Sampler(threading.Thread):
    """Mesure chaque seconde et garde le dernier relevé."""

    def __init__(self, interval=1.0):
        super().__init__(daemon=True)
        self.interval = interval
        self.latest = {}
        self.lock = threading.Lock()

    def run(self):
        prev_cpu, prev_net, prev_disk, prev_t = cpu_times(), net_bytes(), disk_bytes(), time.time()
        while True:
            time.sleep(self.interval)
            cpu, net, disk, now = cpu_times(), net_bytes(), disk_bytes(), time.time()
            dt = max(now - prev_t, 1e-3)
            busy, total = cpu[0] - prev_cpu[0], cpu[1] - prev_cpu[1]
            sample = {
                "time": now,
                "cpu": {"percent": round(100 * busy / total, 1) if total > 0 else 0,
                        "temp": cpu_temp(), "cores": os.cpu_count()},
                "mem": memory(),
                "net": {"down": max(0, (net[0] - prev_net[0]) / dt),
                        "up": max(0, (net[1] - prev_net[1]) / dt)},
                "disk": {"read": max(0, (disk[0] - prev_disk[0]) / dt),
                         "write": max(0, (disk[1] - prev_disk[1]) / dt),
                         "mounts": mounts()},
            }
            with self.lock:
                self.latest = sample
            prev_cpu, prev_net, prev_disk, prev_t = cpu, net, disk, now

    def get(self):
        with self.lock:
            return dict(self.latest)


# --------------------------------------------------------------------------- serveur

def make_handler(sampler):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/stats":
                body = json.dumps(sampler.get()).encode()
                ctype = "application/json"
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
