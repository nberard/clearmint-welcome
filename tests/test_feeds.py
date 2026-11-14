import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import feeds  # noqa: E402

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Une</title>
<item><title>Titre A - Le Monde</title><link>https://news.example/a</link>
<pubDate>Sat, 14 Nov 2026 16:00:00 GMT</pubDate><source url="https://lemonde.fr">Le Monde</source></item>
<item><title>Titre B - BFM</title><link>https://news.example/b</link>
<pubDate>Sat, 14 Nov 2026 10:00:00 GMT</pubDate><source url="https://bfm.fr">BFM</source></item>
<item><title>Titre C</title><link>https://news.example/c</link>
<pubDate>Fri, 13 Nov 2026 10:00:00 GMT</pubDate></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/"
      xmlns:yt="http://www.youtube.com/xml/schemas/2015">
<title>Chaîne Test</title>
<entry><title>Vidéo 1</title><link rel="alternate" href="https://www.youtube.com/watch?v=1"/>
<author><name>Chaîne Test</name></author><published>2026-11-10T15:00:00+00:00</published>
<media:group><media:thumbnail url="https://i.ytimg.com/vi/1/hq.jpg"/></media:group></entry>
<entry><title>Short</title><link rel="alternate" href="https://www.youtube.com/shorts/2"/>
<author><name>Chaîne Test</name></author><published>2026-11-12T15:00:00+00:00</published></entry>
</feed>"""


class Parsers(unittest.TestCase):
    def test_rss(self):
        items = feeds.parse_rss(RSS)
        self.assertEqual([i["title"] for i in items], ["Titre A", "Titre B", "Titre C"])
        self.assertEqual(items[0]["source"], "Le Monde")
        self.assertEqual(items[0]["date"].hour, 16)

    def test_youtube_skips_shorts(self):
        items = feeds.parse_youtube(ATOM)
        self.assertEqual([i["title"] for i in items], ["Vidéo 1"])
        self.assertEqual(items[0]["thumb"], "https://i.ytimg.com/vi/1/hq.jpg")
        self.assertEqual(len(feeds.parse_youtube(ATOM, shorts=True)), 2)

    def test_google_news_url(self):
        self.assertEqual(feeds.google_news_url(), "https://news.google.com/rss?hl=fr&gl=FR&ceid=FR%3Afr")
        self.assertIn("/rss/search?q=Occitanie", feeds.google_news_url(q="Occitanie"))


class Channels(unittest.TestCase):
    def test_takeout_csv_and_ids(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as f:
            f.write("ID de la chaîne,URL de la chaîne,Titre de la chaîne\n"
                    "UCsXVk37bltHxD1rDPwtNM8Q,http://www.youtube.com/channel/UCsXVk37bltHxD1rDPwtNM8Q,Kurzgesagt\n"
                    "UCHnyfMqiRRG1u-2MsSQLbXA,http://www.youtube.com/channel/UCHnyfMqiRRG1u-2MsSQLbXA,Veritasium\n")
        self.addCleanup(os.unlink, f.name)
        ids = feeds.youtube_channels(f.name, "UCsXVk37bltHxD1rDPwtNM8Q, UCXuqSBlHAE6Xw-yeJA0Tunw")
        self.assertEqual(ids, ["UCsXVk37bltHxD1rDPwtNM8Q", "UCXuqSBlHAE6Xw-yeJA0Tunw",
                               "UCHnyfMqiRRG1u-2MsSQLbXA"])


class Merge(unittest.TestCase):
    def files(self):
        paths = []
        for text in (RSS, ATOM):
            f = tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-8")
            f.write(text)
            f.close()
            self.addCleanup(os.unlink, f.name)
            paths.append(f.name)
        return paths

    def test_interleave_two_news_one_video(self):
        rss, atom = self.files()
        items, errors = feeds.merged([(rss, feeds.parse_rss), (atom, feeds.parse_youtube),
                                      ("/nonexistent.xml", feeds.parse_rss)])
        self.assertEqual([i["title"] for i in items], ["Titre A", "Titre B", "Vidéo 1", "Titre C"])
        self.assertEqual(len(errors), 1)               # source en panne signalée, pas bloquante
        self.assertTrue(items[0]["date"].startswith("2026-11-14T16:00"))

    def test_by_date(self):
        rss, atom = self.files()
        items, _ = feeds.merged([(rss, feeds.parse_rss), (atom, feeds.parse_youtube)], ratio=0)
        self.assertEqual([i["title"] for i in items], ["Titre A", "Titre B", "Titre C", "Vidéo 1"])


class SourceCache(unittest.TestCase):
    def test_keeps_last_good_result_when_source_fails(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-8")
        f.write(RSS)
        f.close()
        feeds.SOURCE_CACHE.clear()
        items, err = feeds._fetch_source(f.name, feeds.parse_rss)
        self.assertEqual(len(items), 3)
        os.unlink(f.name)                       # la source disparaît...
        feeds.SOURCE_CACHE[f.name] = (0, feeds.SOURCE_CACHE[f.name][1])   # ...et le cache expire
        items, err = feeds._fetch_source(f.name, feeds.parse_rss)
        self.assertEqual(len(items), 3)         # on garde les anciens éléments
        self.assertIsNotNone(err)               # mais l'erreur est signalée
        merged, _ = feeds.merged([(f.name, feeds.parse_rss)])
        self.assertEqual(len(merged), 3)        # et le mélange marche toujours


PLAYLIST = """{"items": [
 {"snippet": {"title": "Vraie vidéo", "channelTitle": "Chaîne", "videoOwnerChannelTitle": "Chaîne Test",
   "resourceId": {"videoId": "AAA"}, "thumbnails": {"medium": {"url": "https://i.ytimg.com/vi/AAA/mq.jpg"}}},
  "contentDetails": {"videoId": "AAA", "videoPublishedAt": "2026-11-14T10:00:00Z"}},
 {"snippet": {"title": "Short", "resourceId": {"videoId": "BBB"}, "thumbnails": {}},
  "contentDetails": {"videoId": "BBB", "videoPublishedAt": "2026-11-14T11:00:00Z"}},
 {"snippet": {"title": "Private video", "resourceId": {"videoId": "CCC"}}, "contentDetails": {}}
]}"""


class YoutubeApi(unittest.TestCase):
    def test_parse_playlist_items(self):
        items = feeds.parse_playlist_items(PLAYLIST)
        self.assertEqual([i["title"] for i in items], ["Vraie vidéo", "Short"])  # vidéo privée ignorée
        self.assertEqual(items[0]["source"], "Chaîne Test")
        self.assertEqual(items[0]["url"], "https://www.youtube.com/watch?v=AAA")
        self.assertEqual(items[0]["thumb"], "https://i.ytimg.com/vi/AAA/mq.jpg")
        self.assertEqual(items[0]["date"].hour, 10)

    def test_api_error_raises(self):
        with self.assertRaises(ValueError):
            feeds.parse_playlist_items('{"error": {"message": "API key not valid"}}')

    def test_uploads_url_and_hidden_key(self):
        url = feeds.api_uploads_url("UCsXVk37bltHxD1rDPwtNM8Q", "SECRET123")
        self.assertIn("playlistId=UUsXVk37bltHxD1rDPwtNM8Q", url)
        self.assertNotIn("SECRET123", feeds.hide_key(f"{url}: HTTP 403"))

    def test_iso_duration(self):
        self.assertEqual(feeds.iso_duration("PT45S"), 45)
        self.assertEqual(feeds.iso_duration("PT1H2M3S"), 3723)
        self.assertEqual(feeds.iso_duration("P1DT1M"), 86460)

    def test_drop_shorts(self):
        items = feeds.parse_playlist_items(PLAYLIST)
        real_fetch = feeds.fetch
        feeds.DURATIONS.clear()
        feeds.fetch = lambda url, timeout=10: ('{"items": [{"id": "AAA", "contentDetails": {"duration": "PT12M"}},'
                                               ' {"id": "BBB", "contentDetails": {"duration": "PT30S"}}]}')
        try:
            kept = feeds.drop_shorts(items, "KEY")
        finally:
            feeds.fetch = real_fetch
        self.assertEqual([i["title"] for i in kept], ["Vraie vidéo"])

    def test_key_from_file(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "key")
        with open(path, "w") as f:
            f.write("  ABC  \n")
        old_file, old_env = feeds.KEY_FILE, os.environ.pop("YOUTUBE_API_KEY", None)
        feeds.KEY_FILE = path
        try:
            self.assertEqual(feeds.yt_api_key(), "ABC")
            self.assertEqual(feeds.yt_api_key("XYZ"), "XYZ")
        finally:
            feeds.KEY_FILE = old_file
            if old_env:
                os.environ["YOUTUBE_API_KEY"] = old_env

    def test_build_sources_uses_api_with_key(self):
        src = feeds.build_sources(news=False, yt_ids="UCsXVk37bltHxD1rDPwtNM8Q", yt_key="K")
        self.assertIn("googleapis.com/youtube/v3/playlistItems", src[0][0])
        src = feeds.build_sources(news=False, yt_ids="UCsXVk37bltHxD1rDPwtNM8Q")
        self.assertIn("youtube.com/feeds/videos.xml", src[0][0])


if __name__ == "__main__":
    unittest.main()
