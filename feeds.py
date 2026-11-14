"""Fil d'actualités : Google News (RSS) mélangé aux dernières vidéos de tes abonnements
YouTube (flux publics des chaînes, liste tirée de Google Takeout ou donnée à la main).

Aucune clé d'API ni connexion Google : tout passe par des flux publics.
"""
import csv
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

NS = {"a": "http://www.w3.org/2005/Atom", "media": "http://search.yahoo.com/mrss/",
      "yt": "http://www.youtube.com/xml/schemas/2015"}
UA = "Mozilla/5.0 (X11; Linux x86_64) clearmint-welcome"


def fetch(url, timeout=10):
    """Contenu d'une adresse http(s), ou d'un fichier local (pratique pour tester)."""
    if not url.startswith(("http://", "https://")):
        with open(os.path.expanduser(url.removeprefix("file://")), encoding="utf-8") as f:
            return f.read()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")


def _date(text):
    if not text:
        return None
    try:
        d = parsedate_to_datetime(text)            # RSS : « Sat, 14 Nov 2026 18:02:00 GMT »
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(text.replace("Z", "+00:00"))   # Atom
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_rss(xml, kind="news"):
    """Éléments d'un flux RSS 2.0 (Google News et la plupart des sites)."""
    root = ET.fromstring(xml)
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        src = it.find("source")
        source = (src.text or "").strip() if src is not None else ""
        site = urllib.parse.urlsplit(src.get("url", "")).hostname if src is not None else None
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3]       # Google News ajoute « - Source »
        items.append({"kind": kind, "title": title, "url": (it.findtext("link") or "").strip(),
                      "source": source, "date": _date(it.findtext("pubDate")), "thumb": None,
                      "site": site or urllib.parse.urlsplit(it.findtext("link") or "").hostname})
    return items


def parse_youtube(xml, shorts=False):
    """Vidéos d'un flux de chaîne YouTube (Atom)."""
    root = ET.fromstring(xml)
    channel = root.findtext("a:title", default="", namespaces=NS)
    items = []
    for e in root.findall("a:entry", NS):
        link = e.find("a:link[@rel='alternate']", NS)
        url = link.get("href") if link is not None else ""
        if not shorts and "/shorts/" in url:
            continue
        thumb = e.find("media:group/media:thumbnail", NS)
        items.append({"kind": "youtube", "title": e.findtext("a:title", default="", namespaces=NS),
                      "url": url, "source": e.findtext("a:author/a:name", default=channel, namespaces=NS),
                      "date": _date(e.findtext("a:published", namespaces=NS)),
                      "thumb": thumb.get("url") if thumb is not None else None,
                      "site": "youtube.com"})
    return items


def google_news_url(hl="fr", gl="FR", q=None):
    """Une : …/rss ; recherche : …/rss/search?q=… ; langue et pays configurables."""
    ceid = f"{gl}:{hl.split('-')[0]}"
    params = urllib.parse.urlencode({"hl": hl, "gl": gl, "ceid": ceid})
    if q:
        return f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&{params}"
    return f"https://news.google.com/rss?{params}"


def youtube_channels(path=None, ids=""):
    """Identifiants de chaînes (UC…) : fichier abonnements.csv de Google Takeout
    (« Abonnements YouTube ») et/ou liste séparée par des virgules."""
    out = [c.strip() for c in ids.split(",") if c.strip().startswith("UC")]
    if path:
        with open(os.path.expanduser(path), encoding="utf-8-sig", newline="") as f:
            for row in csv.reader(f):
                for cell in row:
                    m = re.search(r"(UC[\w-]{22})", cell)
                    if m:
                        out.append(m.group(1))
                        break
    return list(dict.fromkeys(out))  # sans doublons, ordre conservé


SOURCE_CACHE = {}   # adresse -> (heure, éléments) : dernier résultat réussi de chaque source
SOURCE_TTL = {"api": 3600, "youtube": 1800, "default": 600}  # API : 1 h pour rester dans le quota


def _fetch_source(url, parser):
    """Éléments d'une source, avec un cache par source.

    YouTube : 30 min (beaucoup de chaînes, YouTube limite les requêtes trop fréquentes),
    nouvel essai via la playlist « uploads » de la chaîne en cas d'erreur, et en dernier
    recours on garde les vidéos déjà connues plutôt que de les faire disparaître.
    """
    import time
    is_api = "googleapis.com/youtube" in url
    is_yt = "youtube.com/feeds" in url or is_api
    ttl = SOURCE_TTL["api" if is_api else "youtube" if is_yt else "default"]
    hit = SOURCE_CACHE.get(url)
    if hit and time.time() - hit[0] < ttl:
        return [dict(i) for i in hit[1]], None
    urls = [url]
    m = re.search(r"channel_id=UC([\w-]{22})", url)
    if m:
        urls.append(f"https://www.youtube.com/feeds/videos.xml?playlist_id=UU{m.group(1)}")
    error = None
    for u in urls:
        try:
            items = parser(fetch(u))
            SOURCE_CACHE[url] = (time.time(), items)
            return [dict(i) for i in items], None
        except Exception as e:
            error = hide_key(f"{url}: {e}")
    if hit:  # ancienne version plutôt que rien
        return [dict(i) for i in hit[1]], error
    return [], error


def merged(sources, limit=40, workers=6, ratio=2):
    """Récupère toutes les sources en parallèle et mélange les éléments par date.

    sources : liste de (adresse, analyseur). ratio : actualités par vidéo (0 = par date).
    Renvoie (éléments, erreurs).
    """
    items, errors = [], []

    def one(src):
        url, parser = src
        return _fetch_source(url, parser)  # une source en panne ne bloque pas les autres

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for got, err in pool.map(one, sources):
            items += got
            if err:
                errors.append(err)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    for i in items:
        if isinstance(i["date"], str):
            i["date"] = _date(i["date"])
    items.sort(key=lambda i: i["date"] or epoch, reverse=True)
    items = interleave(items, ratio)
    for i in items:
        i["date"] = i["date"].isoformat() if i["date"] else None
    return items[:limit], errors


def interleave(items, ratio=2):
    """Mélange : ratio actualités, puis 1 vidéo, etc. (chaque groupe reste trié par date).

    Trié uniquement par date, les vidéos (publiées moins souvent) finiraient tout en bas.
    """
    news = [i for i in items if i["kind"] != "youtube"]
    videos = [i for i in items if i["kind"] == "youtube"]
    if not news or not videos or ratio <= 0:
        return items
    out = []
    while news or videos:
        out += news[:ratio]
        del news[:ratio]
        out += videos[:1]
        del videos[:1]
    return out


# --------------------------------------------------------------------------- API YouTube

YT_API = "https://www.googleapis.com/youtube/v3"
KEY_FILE = "~/.config/clearmint-welcome/youtube-api-key"


def yt_api_key(value=""):
    """Clé d'API YouTube : paramètre, variable YOUTUBE_API_KEY, ou fichier
    ~/.config/clearmint-welcome/youtube-api-key (recommandé : hors du dépôt git)."""
    if value:
        return value.strip()
    if os.environ.get("YOUTUBE_API_KEY"):
        return os.environ["YOUTUBE_API_KEY"].strip()
    path = os.path.expanduser(KEY_FILE)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or None
    return None


def hide_key(text):
    """Ne jamais écrire la clé dans les journaux ou les messages d'erreur."""
    return re.sub(r"key=[^&\s]+", "key=…", str(text))


def api_uploads_url(channel_id, key, n=5):
    """Dernières vidéos d'une chaîne via sa playlist « uploads » (UU… = UC… sans le C)."""
    return (f"{YT_API}/playlistItems?part=snippet,contentDetails&maxResults={n}"
            f"&playlistId=UU{channel_id[2:]}&key={key}")


def parse_playlist_items(text):
    """Réponse JSON de playlistItems.list -> éléments du fil."""
    import json
    data = json.loads(text)
    if "error" in data:
        raise ValueError(data["error"].get("message", "erreur de l'API YouTube"))
    items = []
    for it in data.get("items", []):
        sn = it.get("snippet", {})
        vid = sn.get("resourceId", {}).get("videoId") or it.get("contentDetails", {}).get("videoId")
        if not vid or sn.get("title") in ("Private video", "Deleted video"):
            continue
        th = sn.get("thumbnails", {})
        thumb = (th.get("medium") or th.get("high") or th.get("default") or {}).get("url")
        items.append({"kind": "youtube", "title": sn.get("title", ""), "video_id": vid,
                      "url": f"https://www.youtube.com/watch?v={vid}",
                      "source": sn.get("videoOwnerChannelTitle") or sn.get("channelTitle", ""),
                      "date": _date(it.get("contentDetails", {}).get("videoPublishedAt")
                                    or sn.get("publishedAt")),
                      "thumb": thumb, "site": "youtube.com"})
    return items


def iso_duration(text):
    """'PT1H2M3S' -> secondes."""
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", text or "")
    if not m:
        return None
    d, h, mi, sec = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + sec


DURATIONS = {}  # identifiant de vidéo -> durée en secondes (ne change pas : cache permanent)


def drop_shorts(items, key, max_seconds=60):
    """Retire les Shorts (vidéos de 60 s ou moins), durées demandées par paquets de 50."""
    import json
    ids = [i["video_id"] for i in items if i.get("video_id") and i["video_id"] not in DURATIONS]
    for start in range(0, len(ids), 50):
        batch = ",".join(ids[start:start + 50])
        try:
            data = json.loads(fetch(f"{YT_API}/videos?part=contentDetails&id={batch}&key={key}"))
        except Exception:
            break  # en cas d'erreur, on garde les vidéos plutôt que de les perdre
        for v in data.get("items", []):
            DURATIONS[v["id"]] = iso_duration(v.get("contentDetails", {}).get("duration"))
    return [i for i in items if not (i.get("video_id") and (DURATIONS.get(i["video_id"]) or 999) <= max_seconds)]


def build_sources(hl="fr", gl="FR", q="", news=True, yt_csv=None, yt_ids="", rss="", shorts=False,
                  errors=None, yt_key=None):
    """Liste des sources. Un fichier d'abonnements introuvable est signalé dans errors
    sans empêcher les autres sources (actualités, flux RSS) de s'afficher."""
    sources = []
    errors = errors if errors is not None else []
    if news:
        for query in [x.strip() for x in q.split(",")] if q else [None]:
            sources.append((google_news_url(hl, gl, query or None), parse_rss))
    try:
        channels = youtube_channels(yt_csv, yt_ids)
    except OSError as e:
        errors.append(f"abonnements YouTube : {e.strerror} ({yt_csv})")
        channels = youtube_channels(None, yt_ids)
    for cid in channels:
        if yt_key:   # API officielle : fiable (les flux publics renvoient souvent 404/500)
            sources.append((api_uploads_url(cid, yt_key), parse_playlist_items))
        else:
            sources.append((f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}",
                            lambda xml: parse_youtube(xml, shorts)))
    for url in [u.strip() for u in rss.split(",") if u.strip()]:
        sources.append((url, parse_rss))
    return sources


# --------------------------------------------------------------------------- diagnostic

def diagnose(yt_csv=None, yt_ids="", sample=5):
    """Vérifie pas à pas la lecture YouTube : fichier, chaînes trouvées, accès aux flux."""
    import time
    import urllib.error
    print("1. Liste des chaînes")
    if yt_csv:
        path = os.path.expanduser(yt_csv)
        if not os.path.isfile(path):
            print(f"   ✗ fichier introuvable : {path}")
            return
        print(f"   fichier : {path} ({os.path.getsize(path)} octets)")
    try:
        ids = youtube_channels(yt_csv, yt_ids)
    except Exception as e:
        print(f"   ✗ lecture impossible : {e}")
        return
    print(f"   {len(ids)} chaînes trouvées" + (f", ex. {', '.join(ids[:3])}" if ids else ""))
    if not ids:
        print("   ✗ aucune chaîne : le fichier doit contenir des identifiants UC… (abonnements.csv)")
        return

    key = yt_api_key()
    if key:
        print(f"\n2. API YouTube (clé trouvée, {min(sample, len(ids))} chaînes testées)")
        ok = 0
        for cid in ids[:sample]:
            try:
                vids = parse_playlist_items(fetch(api_uploads_url(cid, key)))
                print(f"   ✓ {cid} : {len(vids)} vidéos" + (f", {vids[0]['source']}" if vids else ""))
                ok += 1
            except Exception as e:
                print(f"   ✗ {cid} : {hide_key(e)}")
        print("\n3. Bilan")
        print(f"   {ok}/{min(sample, len(ids))} chaînes lisibles par l'API." if ok else
              "   L'API ne répond pas : vérifie la clé et que « YouTube Data API v3 » est activée.")
        return
    print(f"\n   (pas de clé d'API : test des flux publics ; voir {KEY_FILE})")
    print(f"\n2. Accès aux flux ({min(sample, len(ids))} chaînes testées)")
    ok = 0
    for cid in ids[:sample]:
        for label, url in (("chaîne", f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"),
                           ("uploads", f"https://www.youtube.com/feeds/videos.xml?playlist_id=UU{cid[2:]}")):
            t = time.time()
            try:
                vids = parse_youtube(fetch(url))
                title = vids[0]["source"] if vids else "?"
                print(f"   ✓ {cid} ({label}) : {len(vids)} vidéos, {title} — {time.time() - t:.1f} s")
                ok += 1
                break
            except urllib.error.HTTPError as e:
                print(f"   ✗ {cid} ({label}) : HTTP {e.code} {e.reason}")
            except Exception as e:
                print(f"   ✗ {cid} ({label}) : {e}")
    print("\n3. Bilan")
    if ok:
        print(f"   {ok}/{min(sample, len(ids))} chaînes lisibles : la lecture YouTube fonctionne.")
        print(f"   Avec {len(ids)} chaînes, le premier chargement prend environ "
              f"{max(5, len(ids) // 6)} s dans le tableau de bord.")
    else:
        print("   Aucune chaîne lisible. Erreurs 404/429 : YouTube limite temporairement ta")
        print("   connexion (trop de requêtes) ; réessaie dans une heure. Autre erreur : réseau.")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Diagnostic de la lecture YouTube.")
    ap.add_argument("--yt", help="fichier abonnements.csv (Google Takeout)")
    ap.add_argument("--ytids", default="", help="identifiants de chaînes UC…, séparés par des virgules")
    ap.add_argument("--sample", type=int, default=5, help="nombre de chaînes à tester")
    a = ap.parse_args()
    diagnose(a.yt, a.ytids, a.sample)
