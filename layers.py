"""Description des calques : lecture depuis la ligne de commande ou un fichier JSON.

Ce module n'utilise pas GTK, pour pouvoir être testé sans écran.

Un calque en ligne de commande s'écrit « URL|option,option=valeur,... », par exemple :
  "https://nico@health.touplitoui.me"
  "http://localhost:8080/hud.html|x=70%,y=5%,w=28%,h=40%,opacity=0.85"
  "http://localhost:8080/fx.html|through"

Options :
  x, y, w, h    position et taille, en pixels (300) ou en pourcentage de l'écran (50%)
                (défaut : tout l'écran)
  opacity       opacité du calque, de 0 à 1 (défaut : 1)
  transparent   fond de page transparent, pour voir les calques du dessous
                (défaut : oui pour tous les calques sauf le premier)
  through       les clics traversent ce calque vers ceux du dessous (défaut : non)
  zoom          zoom de la page (défaut : 1)
  refresh       recharge la page toutes les N secondes (défaut : 0 = jamais)
"""
import json
from dataclasses import dataclass, fields
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

TRUE = {"", "1", "true", "yes", "oui", "on"}
FALSE = {"0", "false", "no", "non", "off"}


class LayerError(ValueError):
    pass


@dataclass
class Layer:
    url: str
    user: Optional[str] = None
    x: str = "0"
    y: str = "0"
    w: str = "100%"
    h: str = "100%"
    opacity: float = 1.0
    transparent: Optional[bool] = None  # None : automatique (voir finalize)
    through: bool = False
    zoom: float = 1.0
    refresh: int = 0

    @property
    def host(self):
        return urlsplit(self.url).hostname


def split_user(url):
    """Retire 'nico@' de l'adresse : WebKit n'accepte pas l'identifiant dans l'URL."""
    parts = urlsplit(url)
    if not parts.scheme or not parts.hostname:
        raise LayerError(f"adresse invalide : {url!r} (il faut http:// ou https://)")
    netloc = parts.hostname
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment)), parts.username


def _to_bool(key, value):
    v = str(value).strip().lower()
    if v in TRUE:
        return True
    if v in FALSE:
        return False
    raise LayerError(f"{key}={value!r} : valeur attendue oui/non")


def _check_size(key, value):
    v = str(value).strip()
    num = v[:-1] if v.endswith("%") else v
    try:
        float(num)
    except ValueError:
        raise LayerError(f"{key}={value!r} : valeur attendue en pixels (300) ou en % (50%)")
    return v


def _apply(layer, key, value):
    key = key.strip().lower()
    if key in ("x", "y", "w", "h"):
        layer.__setattr__(key, _check_size(key, value))
    elif key in ("opacity", "zoom"):
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise LayerError(f"{key}={value!r} : nombre attendu")
        if key == "opacity" and not 0 <= num <= 1:
            raise LayerError(f"opacity={value!r} : doit être entre 0 et 1")
        if key == "zoom" and num <= 0:
            raise LayerError(f"zoom={value!r} : doit être positif")
        layer.__setattr__(key, num)
    elif key in ("transparent", "through"):
        layer.__setattr__(key, _to_bool(key, value))
    elif key == "refresh":
        try:
            layer.refresh = max(0, int(value))
        except (TypeError, ValueError):
            raise LayerError(f"refresh={value!r} : nombre de secondes attendu")
    elif key == "user":
        layer.user = str(value)
    else:
        known = ", ".join(f.name for f in fields(Layer) if f.name != "url")
        raise LayerError(f"option inconnue : {key!r} (options : {known})")


def parse_spec(spec):
    """'URL|x=10%,w=30%,through' -> Layer."""
    url, _, opts = spec.partition("|")
    url, user = split_user(url.strip())
    layer = Layer(url=url, user=user)
    for item in filter(None, (o.strip() for o in opts.split(","))):
        key, _, value = item.partition("=")
        _apply(layer, key, value)
    return layer


def from_dict(data):
    if not isinstance(data, dict) or "url" not in data:
        raise LayerError(f"chaque calque doit avoir une « url » : {data!r}")
    url, user = split_user(str(data["url"]))
    layer = Layer(url=url, user=user)
    for key, value in data.items():
        if key != "url":
            _apply(layer, key, value)
    return layer


def load_config(path):
    """Fichier JSON : une liste de calques, ou {"layers": [...]}."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        raise LayerError(f"impossible de lire {path} : {e}")
    except json.JSONDecodeError as e:
        raise LayerError(f"{path} n'est pas du JSON valide : {e}")
    items = data.get("layers") if isinstance(data, dict) else data
    if not isinstance(items, list) or not items:
        raise LayerError(f"{path} : il faut une liste de calques")
    return [from_dict(d) for d in items]


def finalize(layers):
    """Règles par défaut : seul le premier calque a un fond opaque."""
    for i, layer in enumerate(layers):
        if layer.transparent is None:
            layer.transparent = i > 0
    return layers


def to_px(value, total):
    v = str(value).strip()
    if v.endswith("%"):
        return round(total * float(v[:-1]) / 100)
    return round(float(v))


def place(layer, width, height):
    """Position et taille en pixels dans une zone width x height, sans déborder."""
    x = min(max(to_px(layer.x, width), 0), max(width - 1, 0))
    y = min(max(to_px(layer.y, height), 0), max(height - 1, 0))
    w = min(max(to_px(layer.w, width), 1), width - x)
    h = min(max(to_px(layer.h, height), 1), height - y)
    return x, y, w, h


def is_fullscreen(layer, width, height):
    return place(layer, width, height) == (0, 0, width, height)
