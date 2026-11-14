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
        source = (it.findtext("source") or "").strip()
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3]       # Google News ajoute « - Source »
        items.append({"kind": kind, "title": title, "url": (it.findtext("link") or "").strip(),
                      "source": source, "date": _date(it.findtext("pubDate")), "thumb": None})
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
                      "thumb": thumb.get("url") if thumb is not None else None})
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


def merged(sources, limit=40, workers=8, ratio=2):
    """Récupère toutes les sources en parallèle et mélange les éléments par date.

    sources : liste de (adresse, analyseur). ratio : actualités par vidéo (0 = par date).
    Renvoie (éléments, erreurs).
    """
    items, errors = [], []

    def one(src):
        url, parser = src
        try:
            return parser(fetch(url)), None
        except Exception as e:  # une source en panne ne doit pas tout bloquer
            return [], f"{url}: {e}"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for got, err in pool.map(one, sources):
            items += got
            if err:
                errors.append(err)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
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


def build_sources(hl="fr", gl="FR", q="", news=True, yt_csv=None, yt_ids="", rss="", shorts=False):
    sources = []
    if news:
        for query in [x.strip() for x in q.split(",")] if q else [None]:
            sources.append((google_news_url(hl, gl, query or None), parse_rss))
    for cid in youtube_channels(yt_csv, yt_ids):
        sources.append((f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}",
                        lambda xml: parse_youtube(xml, shorts)))
    for url in [u.strip() for u in rss.split(",") if u.strip()]:
        sources.append((url, parse_rss))
    return sources
