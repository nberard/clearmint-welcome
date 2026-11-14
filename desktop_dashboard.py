#!/usr/bin/env python3
"""Affiche une page web comme fond d'écran interactif (une fenêtre par écran).

Usage :
  python3 desktop_dashboard.py https://nico@health.touplitoui.me
  python3 desktop_dashboard.py https://nico@health.touplitoui.me --forget   # oublie le mot de passe
  python3 desktop_dashboard.py https://nico@health.touplitoui.me --window   # test : fenêtre normale

- Authentification HTTP (la fenêtre « nom d'utilisateur / mot de passe » du navigateur) :
  le mot de passe est demandé une seule fois, puis rangé dans le trousseau de Mint
  (le même que celui des mots de passe Wi-Fi), jamais en clair dans un fichier.
- Page de connexion classique (formulaire) : les cookies sont conservés entre deux
  démarrages, donc tu restes connecté.

Dépendances : sudo apt install python3-gi gir1.2-webkit2-4.1 gir1.2-secret-1
"""
import argparse
import os
import signal
from urllib.parse import urlsplit, urlunsplit

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")
gi.require_version("Secret", "1")
from gi.repository import Gdk, GLib, Gtk, Secret, WebKit2

APP_ID = "desktop-dashboard"
DATA_DIR = os.path.join(os.path.expanduser("~/.local/share"), APP_ID)
CACHE_DIR = os.path.join(os.path.expanduser("~/.cache"), APP_ID)

SCHEMA = Secret.Schema.new(
    "me.desktop-dashboard.http-auth",
    Secret.SchemaFlags.NONE,
    {"host": Secret.SchemaAttributeType.STRING, "user": Secret.SchemaAttributeType.STRING},
)


# --------------------------------------------------------------------------- trousseau

def keyring_get(host, user):
    try:
        return Secret.password_lookup_sync(SCHEMA, {"host": host, "user": user}, None)
    except Exception as e:  # trousseau verrouillé ou absent
        print(f"Trousseau indisponible : {e}")
        return None


def keyring_set(host, user, password):
    try:
        Secret.password_store_sync(SCHEMA, {"host": host, "user": user},
                                   Secret.COLLECTION_DEFAULT,
                                   f"Desktop Dashboard - {user}@{host}", password, None)
    except Exception as e:
        print(f"Impossible d'enregistrer le mot de passe : {e}")


def keyring_clear(host, user):
    try:
        Secret.password_clear_sync(SCHEMA, {"host": host, "user": user}, None)
    except Exception:
        pass


# --------------------------------------------------------------------------- authentification

class Auth:
    """Répond aux demandes d'authentification HTTP de toutes les fenêtres.

    Avec plusieurs écrans, chaque fenêtre demande l'accès en même temps : on ne pose
    la question qu'une fois, puis on répond à toutes les demandes en attente.
    """

    def __init__(self, default_user):
        self.default_user = default_user
        self.known = {}      # host -> (user, password) pour cette session
        self.pending = {}    # host -> [requêtes en attente]
        self.dialog = None

    def on_authenticate(self, _view, request):
        host = request.get_host()
        print(f"[auth] {host} demande une connexion"
              f"{' (nouvel essai)' if request.is_retry() else ''}")
        if request.is_retry():
            # Mot de passe refusé par le serveur : on l'oublie et on redemande.
            user = self.known.get(host, (self.default_user, None))[0]
            self.known.pop(host, None)
            if user:
                keyring_clear(host, user)
        elif host in self.known:
            self._answer(request, *self.known[host])
            return True
        elif self.default_user:
            password = keyring_get(host, self.default_user)
            if password:
                print(f"[auth] mot de passe trouvé dans le trousseau pour {self.default_user}")
                self.known[host] = (self.default_user, password)
                self._answer(request, self.default_user, password)
                return True

        self.pending.setdefault(host, []).append(request)
        if self.dialog is None:
            self._ask(host, request.get_realm(), request.is_retry())
        return True  # on répondra plus tard

    @staticmethod
    def _answer(request, user, password):
        request.authenticate(
            WebKit2.Credential.new(user, password, WebKit2.CredentialPersistence.FOR_SESSION))

    def _ask(self, host, realm, retry):
        d = Gtk.Dialog(title="Connexion", flags=0)
        d.set_keep_above(True)
        d.set_position(Gtk.WindowPosition.CENTER)
        d.add_buttons("Annuler", Gtk.ResponseType.CANCEL, "Se connecter", Gtk.ResponseType.OK)
        d.set_default_response(Gtk.ResponseType.OK)

        box = d.get_content_area()
        box.set_spacing(8)
        box.set_border_width(12)
        msg = f"<b>{host}</b> demande une connexion"
        if realm:
            msg += f"\n({realm})"
        if retry:
            msg += "\n<span foreground='red'>Identifiant ou mot de passe incorrect.</span>"
        label = Gtk.Label()
        label.set_markup(msg)
        box.add(label)

        user_entry = Gtk.Entry(text=self.default_user or "", placeholder_text="Utilisateur")
        pass_entry = Gtk.Entry(visibility=False, placeholder_text="Mot de passe")
        pass_entry.set_activates_default(True)
        remember = Gtk.CheckButton(label="Se souvenir du mot de passe", active=True)
        for w in (user_entry, pass_entry, remember):
            box.add(w)

        d.connect("response", self._on_response, host, user_entry, pass_entry, remember)
        d.show_all()
        (pass_entry if self.default_user else user_entry).grab_focus()
        d.present()
        self.dialog = d
        print("[auth] fenêtre de connexion affichée")

    def _on_response(self, dialog, response, host, user_entry, pass_entry, remember):
        user, password = user_entry.get_text().strip(), pass_entry.get_text()
        dialog.destroy()
        self.dialog = None
        requests = self.pending.pop(host, [])
        if response != Gtk.ResponseType.OK or not user:
            for r in requests:
                r.cancel()
            return
        self.default_user = user
        self.known[host] = (user, password)
        if remember.get_active():
            keyring_set(host, user, password)
        for r in requests:
            self._answer(r, user, password)


# --------------------------------------------------------------------------- fenêtres

def make_context():
    """Contexte web partagé, avec cookies et cache conservés entre les démarrages."""
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)
    manager = WebKit2.WebsiteDataManager(base_data_directory=DATA_DIR,
                                         base_cache_directory=CACHE_DIR)
    manager.get_cookie_manager().set_persistent_storage(
        os.path.join(DATA_DIR, "cookies.sqlite"), WebKit2.CookiePersistentStorage.SQLITE)
    return WebKit2.WebContext.new_with_website_data_manager(manager)


def on_load_changed(_view, event, name):
    labels = {WebKit2.LoadEvent.STARTED: "chargement...",
              WebKit2.LoadEvent.COMMITTED: "réponse reçue",
              WebKit2.LoadEvent.FINISHED: "page chargée"}
    if event in labels:
        print(f"[{name}] {labels[event]}")


def on_load_failed(_view, _event, uri, error, name):
    print(f"[{name}] ÉCHEC du chargement de {uri} : {error.message}")
    return False


def make_window(monitor, context, url, auth, test_mode, name):
    geo = monitor.get_geometry()
    win = Gtk.Window(title=f"Desktop Dashboard ({name})")
    if test_mode:
        # Fenêtre normale, pour vérifier la page et la connexion sans l'effet « bureau ».
        win.set_default_size(min(1280, geo.width), min(800, geo.height))
        win.connect("destroy", Gtk.main_quit)
    else:
        win.set_type_hint(Gdk.WindowTypeHint.DESKTOP)  # se comporte comme le bureau
        win.set_decorated(False)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        win.set_keep_below(True)
        win.move(geo.x, geo.y)
        win.set_default_size(geo.width, geo.height)

    view = WebKit2.WebView.new_with_context(context)
    view.connect("authenticate", auth.on_authenticate)
    view.connect("load-changed", on_load_changed, name)
    view.connect("load-failed", on_load_failed, name)
    view.load_uri(url)
    win.add(view)
    win.show_all()
    if not test_mode:
        win.stick()  # visible sur tous les espaces de travail
    print(f"[{name}] fenêtre {'de test' if test_mode else 'bureau'} "
          f"{geo.width}x{geo.height} à ({geo.x},{geo.y})")
    return win


def split_user(url):
    """Retire 'nico@' de l'adresse : WebKit n'accepte pas l'identifiant dans l'URL."""
    parts = urlsplit(url)
    user = parts.username
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment)), user


def main():
    ap = argparse.ArgumentParser(description="Page web en fond d'écran interactif.")
    ap.add_argument("url", nargs="?", default="http://localhost:8080")
    ap.add_argument("--user", help="identifiant (sinon pris dans l'URL : https://nico@...)")
    ap.add_argument("--forget", action="store_true", help="oublie le mot de passe enregistré")
    ap.add_argument("--window", action="store_true",
                    help="test : une fenêtre normale au lieu du fond d'écran")
    args = ap.parse_args()

    url, url_user = split_user(args.url)
    user = args.user or url_user
    host = urlsplit(url).hostname

    if args.forget:
        if user:
            keyring_clear(host, user)
            print(f"Mot de passe oublié pour {user}@{host}.")
        else:
            print("Indique l'utilisateur : https://nico@... ou --user nico")
        return

    # Ctrl+C quitte proprement au lieu d'afficher une erreur.
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, Gtk.main_quit)

    print(f"Adresse : {url}  (utilisateur : {user or 'aucun'})")
    auth = Auth(user)
    context = make_context()
    display = Gdk.Display.get_default()
    count = 1 if args.window else display.get_n_monitors()
    windows = [make_window(display.get_monitor(i), context, url, auth, args.window, f"écran {i + 1}")
               for i in range(count)]
    Gtk.main()
    return windows


if __name__ == "__main__":
    main()
