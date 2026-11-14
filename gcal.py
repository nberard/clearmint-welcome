"""Google Agenda et Google Tasks en direct, via les API officielles (lecture seule).

Mise en place, une seule fois :
  1. Google Cloud Console : activer « Google Calendar API » et « Google Tasks API »,
     créer un identifiant OAuth de type « Application de bureau », télécharger le JSON
     et l'enregistrer sous ~/.config/clearmint-welcome/google-oauth-client.json
  2. python3 gcal.py login     # ouvre le navigateur pour autoriser l'accès (lecture seule)
  3. python3 gcal.py test      # affiche tes agendas et les événements de la semaine

Le jeton est rangé dans ~/.config/clearmint-welcome/google-token.json (lisible par toi
seul). Aucune dépendance : tout passe par la bibliothèque standard de Python.
"""
import base64
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

CONFIG_DIR = os.path.expanduser("~/.config/clearmint-welcome")
CLIENT_FILE = os.path.join(CONFIG_DIR, "google-oauth-client.json")
TOKEN_FILE = os.path.join(CONFIG_DIR, "google-token.json")
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly",
          "https://www.googleapis.com/auth/tasks.readonly"]
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CAL_API = "https://www.googleapis.com/calendar/v3"
TASKS_API = "https://tasks.googleapis.com/tasks/v1"

# Couleurs d'événement de Google Agenda (colorId -> couleur), identiques à l'interface.
EVENT_COLORS = {"1": "#7986cb", "2": "#33b679", "3": "#8e24aa", "4": "#e67c73", "5": "#f6bf26",
                "6": "#f4511e", "7": "#039be5", "8": "#616161", "9": "#3f51b5", "10": "#0b8043",
                "11": "#d50000"}
TASK_COLOR = "#f4511e"  # orange « Mandarine »


class NotConfigured(Exception):
    pass


# --------------------------------------------------------------------------- OAuth

def load_client():
    if not os.path.isfile(CLIENT_FILE):
        raise NotConfigured(f"identifiant OAuth manquant : {CLIENT_FILE} (voir python3 gcal.py --help)")
    with open(CLIENT_FILE, encoding="utf-8") as f:
        data = json.load(f)
    c = data.get("installed") or data.get("web") or {}
    if not c.get("client_id"):
        raise NotConfigured(f"{CLIENT_FILE} : client_id introuvable")
    return c["client_id"], c.get("client_secret", "")


def pkce_pair():
    """Vérificateur et défi PKCE (protège l'échange du code, recommandé par Google)."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _post(url, fields):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"Google a refusé ({e.code}) : {body[:300]}")


def save_token(tok):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(tok, f)


def load_token():
    if not os.path.isfile(TOKEN_FILE):
        raise NotConfigured("pas encore connecté à Google : lance « python3 gcal.py login »")
    with open(TOKEN_FILE, encoding="utf-8") as f:
        return json.load(f)


def login(open_browser=True):
    """Autorisation unique : ouvre le navigateur, récupère le code sur 127.0.0.1."""
    import webbrowser
    client_id, client_secret = load_client()
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if q.get("state", [""])[0] == state:
                result.update({k: v[0] for k, v in q.items()})
            ok = "code" in result
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = ("C'est fait : tu peux fermer cet onglet." if ok else
                   f"Échec : {result.get('error', 'réponse inattendue')}")
            self.wfile.write(f"<h2 style='font-family:sans-serif'>{msg}</h2>".encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state})
    print("Autorise l'accès dans le navigateur (lecture seule de l'agenda et des tâches).")
    print(f"S'il ne s'ouvre pas, copie cette adresse :\n{url}\n")
    if open_browser:
        webbrowser.open(url)
    server.timeout = 30
    deadline = time.time() + 300  # 5 minutes pour autoriser
    while "code" not in result and "error" not in result and time.time() < deadline:
        server.handle_request()   # ignore les autres requêtes du navigateur (favicon…)
    server.server_close()
    if "code" not in result:
        raise RuntimeError(f"autorisation refusée ou expirée : {result.get('error', 'pas de réponse')}")
    tok = _post(TOKEN_URL, {"code": result["code"], "client_id": client_id, "client_secret": client_secret,
                            "redirect_uri": redirect, "grant_type": "authorization_code",
                            "code_verifier": verifier})
    if "refresh_token" not in tok:
        raise RuntimeError("Google n'a pas fourni de jeton de renouvellement : relance login")
    tok["expiry"] = time.time() + tok.get("expires_in", 3600)
    save_token(tok)
    print(f"Connecté. Jeton enregistré dans {TOKEN_FILE}")


def access_token(force=False):
    """Jeton d'accès valide (renouvelé automatiquement grâce au jeton de renouvellement)."""
    tok = load_token()
    if not force and tok.get("access_token") and tok.get("expiry", 0) > time.time() + 60:
        return tok["access_token"]
    client_id, client_secret = load_client()
    new = _post(TOKEN_URL, {"client_id": client_id, "client_secret": client_secret,
                            "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"})
    tok.update(access_token=new["access_token"], expiry=time.time() + new.get("expires_in", 3600))
    save_token(tok)
    return tok["access_token"]


def api_get(url, params=None):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    for attempt in (0, 1):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token(force=attempt == 1)}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                continue  # jeton expiré entre-temps : on le renouvelle une fois
            body = e.read().decode(errors="replace")
            raise RuntimeError(f"API Google ({e.code}) : {body[:300]}")


def paged(url, params):
    params = dict(params)
    while True:
        data = api_get(url, params)
        yield from data.get("items", [])
        if not data.get("nextPageToken"):
            return
        params["pageToken"] = data["nextPageToken"]


# --------------------------------------------------------------------------- données

def calendars():
    """Agendas affichés dans Google Agenda (cochés), avec leur couleur."""
    return [c for c in paged(f"{CAL_API}/users/me/calendarList", {"minAccessRole": "reader"})
            if c.get("selected") or c.get("primary")]


def convert_event(ev, cal_color, tz=None):
    """Événement de l'API -> format du panneau agenda."""
    tz = tz or datetime.now().astimezone().tzinfo
    if ev.get("status") == "cancelled":
        return None
    s, e = ev.get("start", {}), ev.get("end", {})
    all_day = "date" in s
    if all_day:
        start, end = s["date"], e.get("date", s["date"])
    else:
        start = datetime.fromisoformat(s["dateTime"].replace("Z", "+00:00")).astimezone(tz).isoformat()
        end = datetime.fromisoformat(e.get("dateTime", s["dateTime"]).replace("Z", "+00:00")).astimezone(tz).isoformat()
    return {"title": ev.get("summary", "(sans titre)"), "location": ev.get("location", ""),
            "all_day": all_day, "start": start, "end": end,
            "color": EVENT_COLORS.get(ev.get("colorId", ""), cal_color), "task": False}


def convert_task(t):
    """Tâche Google Tasks (échéance = journée) -> élément orange du panneau agenda."""
    due = t.get("due")
    if not due or t.get("deleted") or t.get("hidden"):
        return None
    day = due[:10]
    end = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
    return {"title": t.get("title", "(tâche)"), "location": "", "all_day": True, "start": day, "end": end,
            "color": TASK_COLOR, "task": True, "done": t.get("status") == "completed"}


def events_between(start, end, with_tasks=True, hide=()):
    """Événements de tous les agendas affichés + tâches à échéance dans la période.
    hide : noms d'agendas à ne pas afficher sur le bureau (ex. « Numéros de semaine »)."""
    hidden = {h.strip().lower() for h in hide if h.strip()}
    out, errors = [], []
    params = {"timeMin": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
              "timeMax": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
              "singleEvents": "true", "orderBy": "startTime", "maxResults": 250}
    for cal in calendars():
        name = (cal.get("summaryOverride") or cal.get("summary", "")).strip().lower()
        if name in hidden:
            continue
        cid = urllib.parse.quote(cal["id"], safe="")
        try:
            for ev in paged(f"{CAL_API}/calendars/{cid}/events", params):
                conv = convert_event(ev, cal.get("backgroundColor", "#039be5"))
                if conv:
                    conv["calendar"] = cal.get("summaryOverride") or cal.get("summary", "")
                    out.append(conv)
        except RuntimeError as e:
            errors.append(f"{cal.get('summary')} : {e}")
    if with_tasks:
        try:
            due = {"dueMin": params["timeMin"], "dueMax": params["timeMax"],
                   "showCompleted": "true", "showHidden": "false", "maxResults": 100}
            for lst in paged(f"{TASKS_API}/users/@me/lists", {}):
                lid = urllib.parse.quote(lst["id"], safe="")
                for t in paged(f"{TASKS_API}/lists/{lid}/tasks", due):
                    conv = convert_task(t)
                    if conv:
                        out.append(conv)
        except RuntimeError as e:
            errors.append(f"Tâches : {e}")
    out.sort(key=lambda e: (e["start"][:10], not e["all_day"], e["start"]))
    return out, errors


# --------------------------------------------------------------------------- ligne de commande

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "--help"
    try:
        if cmd == "login":
            login()
        elif cmd == "logout":
            if os.path.exists(TOKEN_FILE):
                os.remove(TOKEN_FILE)
            print("Déconnecté (jeton supprimé). Pour retirer l'accès côté Google : "
                  "myaccount.google.com/permissions")
        elif cmd == "test":
            cals = calendars()
            print(f"{len(cals)} agenda(s) affiché(s) :")
            for c in cals:
                print(f"  ■ {c.get('backgroundColor')}  {c.get('summaryOverride') or c.get('summary')}")
            now = datetime.now().astimezone()
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            evs, errors = events_between(start, start + timedelta(days=7))
            print(f"\n{len(evs)} élément(s) dans les 7 prochains jours :")
            for e in evs[:30]:
                kind = "✓ tâche" if e["task"] else ("journée" if e["all_day"] else e["start"][11:16])
                print(f"  {e['start'][:10]}  {kind:>8}  {e['title']}")
            for err in errors:
                print(f"  ⚠ {err}")
        else:
            print(__doc__)
    except (NotConfigured, RuntimeError) as e:
        sys.exit(f"Erreur : {e}")


if __name__ == "__main__":
    main()
