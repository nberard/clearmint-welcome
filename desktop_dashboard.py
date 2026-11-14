#!/usr/bin/env python3
"""Affiche une ou plusieurs pages web superposées comme fond d'écran interactif.

Usage :
  python3 desktop_dashboard.py https://nico@health.touplitoui.me
  python3 desktop_dashboard.py URL1 "URL2|x=70%,y=5%,w=28%,h=40%,opacity=0.85" "URL3|through"
  python3 desktop_dashboard.py --config layers.json
  python3 desktop_dashboard.py ... --window   # test : fenêtre normale
  python3 desktop_dashboard.py ... --forget   # oublie les mots de passe enregistrés

- Calques : la première adresse est le fond, les suivantes sont empilées par-dessus.
  Les options de chaque calque sont décrites dans layers.py et le README.
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
import sys
import time
from urllib.parse import urlsplit


def log_to_file():
    """Sans terminal (démarrage automatique), tout est écrit dans un fichier journal.

    Placé avant les autres imports pour capter aussi une erreur au chargement de GTK.
    """
    if sys.stdout is not None and sys.stdout.isatty():
        return
    folder = os.path.expanduser("~/.cache/desktop-dashboard")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "dashboard.log")
    if os.path.exists(path) and os.path.getsize(path) > 1_000_000:
        os.replace(path, path + ".old")
    f = open(path, "a", buffering=1, encoding="utf-8")
    os.dup2(f.fileno(), 1)  # aussi les messages de GTK et WebKit
    os.dup2(f.fileno(), 2)
    sys.stdout = sys.stderr = f
    print(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} démarrage : {' '.join(sys.argv)}")


log_to_file()

import cairo  # noqa: E402
import gi  # noqa: E402
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")
gi.require_version("Secret", "1")
from gi.repository import Gdk, Gio, GLib, Gtk, Secret, WebKit2
gi.require_foreign("cairo")

import layers as L  # noqa: E402
import stats  # noqa: E402

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

    def __init__(self, users):
        self.users = dict(users)  # host -> identifiant (pris dans les URL : https://nico@...)
        self.known = {}      # host -> (user, password) pour cette session
        self.pending = {}    # host -> [requêtes en attente]
        self.queue = []      # sites en attente de la fenêtre de connexion
        self.declined = set()  # sites où l'utilisateur a annulé : pas de nouvel essai
        self.dialog = None

    def on_authenticate(self, _view, request):
        host = request.get_host()
        user = self.users.get(host)
        print(f"[auth] {host} demande une connexion"
              f"{' (nouvel essai)' if request.is_retry() else ''}")
        if request.is_retry():
            # Mot de passe refusé par le serveur : on l'oublie et on redemande.
            user = self.known.get(host, (user, None))[0]
            self.known.pop(host, None)
            if user:
                keyring_clear(host, user)
        elif host in self.known:
            self._answer(request, *self.known[host])
            return True
        elif user:
            password = keyring_get(host, user)
            if password:
                print(f"[auth] mot de passe trouvé dans le trousseau pour {user}@{host}")
                self.known[host] = (user, password)
                self._answer(request, user, password)
                return True

        first = host not in self.pending
        self.pending.setdefault(host, []).append(request)
        if first:
            self.queue.append((host, request.get_realm(), request.is_retry()))
        self._next()
        return True  # on répondra plus tard

    def _next(self):
        """Une seule fenêtre de connexion à la fois, un site après l'autre."""
        if self.dialog is None and self.queue:
            self._ask(*self.queue.pop(0))

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

        known_user = self.users.get(host)
        user_entry = Gtk.Entry(text=known_user or "", placeholder_text="Utilisateur")
        pass_entry = Gtk.Entry(visibility=False, placeholder_text="Mot de passe")
        pass_entry.set_activates_default(True)
        remember = Gtk.CheckButton(label="Se souvenir du mot de passe", active=True)
        for w in (user_entry, pass_entry, remember):
            box.add(w)

        d.connect("response", self._on_response, host, user_entry, pass_entry, remember)
        d.show_all()
        (pass_entry if known_user else user_entry).grab_focus()
        d.present()
        self.dialog = d
        print("[auth] fenêtre de connexion affichée")

    def _on_response(self, dialog, response, host, user_entry, pass_entry, remember):
        user, password = user_entry.get_text().strip(), pass_entry.get_text()
        dialog.destroy()
        self.dialog = None
        requests = self.pending.pop(host, [])
        if response != Gtk.ResponseType.OK or not user:
            self.declined.add(host)
            for r in requests:
                r.cancel()
        else:
            self.declined.discard(host)
            self.users[host] = user
            self.known[host] = (user, password)
            if remember.get_active():
                keyring_set(host, user, password)
            for r in requests:
                self._answer(r, user, password)
        self._next()


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


def install_css():
    """Fond noir sous les calques (sans dépendre de cairo)."""
    provider = Gtk.CssProvider()
    provider.load_from_data(b".dashboard-base { background-color: #000; }")
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


RETRIES = []


class Retry:
    """Recharge une page qui n'a pas pu se charger, avec une attente croissante
    (5 s, 10 s, 20 s... jusqu'à 5 min), et tout de suite quand le réseau revient.

    Au démarrage de la session, le réseau n'est souvent pas encore prêt.
    """

    def __init__(self, view, url, auth, name):
        self.view, self.url, self.auth, self.name = view, url, auth, name
        self.attempt, self.failed, self.timer = 0, False, None
        view.connect("load-failed", self.on_failed)
        view.connect("load-changed", self.on_changed)
        RETRIES.append(self)

    def on_failed(self, _view, _event, _uri, _error):
        if urlsplit(self.url).hostname in self.auth.declined:
            return False  # connexion annulée par l'utilisateur
        self.failed = True
        if self.timer is None:
            delay = min(300, 5 * 2 ** self.attempt)
            self.attempt += 1
            print(f"[{self.name}] nouvel essai dans {delay} s")
            self.timer = GLib.timeout_add_seconds(delay, self.reload)
        return False

    def on_changed(self, _view, event):
        # La page d'erreur de WebKit se « charge » aussi : on ne remet le compteur à
        # zéro que si aucun échec n'a été signalé depuis le dernier essai.
        if event == WebKit2.LoadEvent.FINISHED and not self.failed:
            self.attempt = 0

    def reload(self):
        self.timer = None
        self.failed = False
        self.view.load_uri(self.url)
        return False

    def network_back(self):
        if self.failed:
            if self.timer is not None:
                GLib.source_remove(self.timer)
                self.timer = None
            print(f"[{self.name}] réseau revenu : rechargement")
            self.attempt = 0
            self.reload()


def watch_network():
    monitor = Gio.NetworkMonitor.get_default()

    def changed(_monitor, available):
        if available:
            for r in RETRIES:
                r.network_back()

    monitor.connect("network-changed", changed)


def connect_view(view, auth, name):
    view.connect("authenticate", auth.on_authenticate)
    view.connect("load-changed", on_load_changed, name)
    view.connect("load-failed", on_load_failed, name)


def make_view(layer, context, auth, name):
    """Vue pour un calque opaque (le fond, ou une page à fond plein)."""
    content = WebKit2.UserContentManager()
    if layer.opacity < 1:
        content.add_style_sheet(WebKit2.UserStyleSheet(
            f"html {{ opacity: {layer.opacity} !important; }}",
            WebKit2.UserContentInjectedFrames.TOP_FRAME,
            WebKit2.UserStyleLevel.USER, None, None))
    view = WebKit2.WebView(web_context=context, user_content_manager=content)
    if layer.zoom != 1:
        view.set_zoom_level(layer.zoom)
    connect_view(view, auth, name)
    Retry(view, layer.url, auth, name)
    if layer.refresh:
        GLib.timeout_add_seconds(layer.refresh, lambda: (view.reload(), True)[1])
    view.load_uri(layer.url)
    return view


def make_composer(layers, context, auth, name):
    """Une seule vue plein écran, transparente, qui empile tous les calques transparents.

    Plusieurs vues WebKit transparentes empilées dans GTK laissent des zones noires
    quand elles se redessinent ; dans une seule page, le navigateur gère tout.
    """
    view = WebKit2.WebView(web_context=context)
    view.set_background_color(Gdk.RGBA(0, 0, 0, 0))
    connect_view(view, auth, name)
    view.load_html(L.composer_html(layers), "http://127.0.0.1/")
    return view


def place_layers(views, width, height):
    """Positionne chaque vue opaque (en pixels) pour une zone width x height."""
    for layer, view in views:
        if L.is_fullscreen(layer, width, height):
            view.set_halign(Gtk.Align.FILL)
            view.set_valign(Gtk.Align.FILL)
            view.set_margin_start(0)
            view.set_margin_top(0)
            view.set_size_request(-1, -1)
        else:
            x, y, w, h = L.place(layer, width, height)
            view.set_halign(Gtk.Align.START)
            view.set_valign(Gtk.Align.START)
            view.set_margin_start(x)
            view.set_margin_top(y)
            view.set_size_request(w, h)


def set_click_area(view, overlay, rects):
    """Le calque n'accepte les clics que dans ces rectangles ; ailleurs, ils passent dessous.

    On découpe la zone de clic de la vue et des fenêtres GDK qui l'entourent (WebKit
    reçoit les clics par sa propre fenêtre, que le réglage « pass-through » de GTK
    n'atteint pas).
    """
    region = cairo.Region([cairo.RectangleInt(x, y, w, h) for x, y, w, h in rects])
    gdk_win = view.get_window()
    top = overlay.get_window()
    while gdk_win is not None and gdk_win != top:
        if os.environ.get("DASHBOARD_DEBUG"):
            print(f"[clics] zone {rects} sur {gdk_win.get_width()}x{gdk_win.get_height()}")
        gdk_win.input_shape_combine_region(region, 0, 0)
        gdk_win = gdk_win.get_parent()


def clickable_rects(layers, width, height):
    return [L.place(l, width, height) for l in layers if not l.through]


def make_window(monitor, context, layer_list, auth, test_mode, screen):
    geo = monitor.get_geometry()
    win = Gtk.Window(title=f"Desktop Dashboard ({screen})")
    if test_mode:
        # Fenêtre normale, pour vérifier les pages et la connexion sans l'effet « bureau ».
        width, height = min(1280, geo.width), min(800, geo.height)
        win.connect("destroy", Gtk.main_quit)
    else:
        width, height = geo.width, geo.height
        win.set_type_hint(Gdk.WindowTypeHint.DESKTOP)  # se comporte comme le bureau
        win.set_decorated(False)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        win.set_keep_below(True)
        win.move(geo.x, geo.y)
    win.set_default_size(width, height)

    # Fond noir, puis les calques opaques dans l'ordre, puis tous les calques
    # transparents réunis dans une seule vue, au-dessus.
    overlay = Gtk.Overlay()
    base = Gtk.Box()
    base.get_style_context().add_class("dashboard-base")
    overlay.add(base)

    opaque = [(i, l) for i, l in enumerate(layer_list) if not l.transparent]
    transparent = [(i, l) for i, l in enumerate(layer_list) if l.transparent]

    views = []
    for i, layer in opaque:
        view = make_view(layer, context, auth, f"{screen} / calque {i + 1}")
        overlay.add_overlay(view)
        if layer.through:
            view.connect_after("map", lambda v: set_click_area(v, overlay, []))
        views.append((layer, view))
    place_layers(views, width, height)

    composer = None
    if transparent:
        names = ", ".join(str(i + 1) for i, _ in transparent)
        composer = make_composer([l for _, l in transparent], context, auth,
                                 f"{screen} / calques transparents {names}")
        overlay.add_overlay(composer)
        # Clics : seulement sur les panneaux transparents cliquables, le reste passe
        # aux calques du dessous. Recalculé quand la taille change.
        t_layers = [l for _, l in transparent]

        def apply_click_area(view, *_):
            if view.get_realized():
                alloc = view.get_allocation()
                set_click_area(view, overlay,
                               clickable_rects(t_layers, alloc.width, alloc.height))

        composer.connect_after("realize", apply_click_area)
        composer.connect_after("map", apply_click_area)
        composer.connect_after("size-allocate", apply_click_area)

    # La fenêtre de test peut être redimensionnée : on recalcule les positions.
    size = {"wh": (width, height)}

    def on_configure(_win, event):
        if (event.width, event.height) != size["wh"]:
            size["wh"] = (event.width, event.height)
            place_layers(views, event.width, event.height)
        return False

    win.connect("configure-event", on_configure)
    win.add(overlay)
    win.show_all()
    if not test_mode:
        win.stick()  # visible sur tous les espaces de travail
    print(f"[{screen}] fenêtre {'de test' if test_mode else 'bureau'} "
          f"{width}x{height} à ({geo.x},{geo.y}), {len(layer_list)} calque(s)")
    for i, layer in enumerate(layer_list):
        x, y, w, h = L.place(layer, width, height)
        print(f"  calque {i + 1}: {layer.url}  {w}x{h} à ({x},{y})"
              f"{', opacité ' + str(layer.opacity) if layer.opacity < 1 else ''}"
              f"{', fond transparent' if layer.transparent else ''}"
              f"{', clics traversants' if layer.through else ''}")
    return win


def main():
    ap = argparse.ArgumentParser(
        description="Pages web superposées en fond d'écran interactif.",
        epilog='Calque : "URL|x=70%,y=5%,w=28%,h=40%,opacity=0.85,transparent,through,'
               'zoom=1.2,refresh=60". La première adresse est le fond. '
               'Panneau système intégré : "stats?city=Paris|x=1%,y=2%,w=460,h=230".')
    ap.add_argument("urls", nargs="*", metavar="URL",
                    help="adresses à empiler, du fond vers le dessus")
    ap.add_argument("--config", help="fichier JSON décrivant les calques")
    ap.add_argument("--user", help="identifiant par défaut (sinon pris dans l'URL : https://nico@...)")
    ap.add_argument("--forget", action="store_true", help="oublie les mots de passe enregistrés")
    ap.add_argument("--window", action="store_true",
                    help="test : une fenêtre normale au lieu du fond d'écran")
    args = ap.parse_args()

    try:
        if args.config:
            layer_list = L.load_config(args.config)
            layer_list += [L.parse_spec(s) for s in args.urls]
        else:
            layer_list = [L.parse_spec(s) for s in (args.urls or ["http://localhost:8080"])]
    except L.LayerError as e:
        sys.exit(f"Erreur : {e}")
    L.finalize(layer_list)
    if args.user:
        for layer in layer_list:
            layer.user = layer.user or args.user

    users = {layer.host: layer.user for layer in layer_list if layer.user}
    builtins = [layer for layer in layer_list if layer.builtin]

    if args.forget:
        if not users:
            print("Indique l'utilisateur : https://nico@... ou --user nico")
        for host, user in users.items():
            keyring_clear(host, user)
            print(f"Mot de passe oublié pour {user}@{host}.")
        return

    # Ctrl+C quitte proprement au lieu d'afficher une erreur.
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, Gtk.main_quit)

    if builtins:
        # Pages intégrées (« stats ») : servies par un petit serveur local.
        base = stats.start_server()
        print(f"Panneau système : {base}/stats.html")
        for layer in builtins:
            layer.url = base + layer.url[len(L.BUILTIN):]

    install_css()
    watch_network()
    auth = Auth(users)
    context = make_context()
    display = Gdk.Display.get_default()
    count = 1 if args.window else display.get_n_monitors()
    windows = [make_window(display.get_monitor(i), context, layer_list, auth, args.window,
                           f"écran {i + 1}")
               for i in range(count)]
    Gtk.main()
    return windows


if __name__ == "__main__":
    main()
