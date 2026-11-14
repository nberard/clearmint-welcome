import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import layers as L  # noqa: E402


class ParseSpec(unittest.TestCase):
    def test_url_only(self):
        layer = L.parse_spec("https://nico@health.touplitoui.me")
        self.assertEqual(layer.url, "https://health.touplitoui.me")
        self.assertEqual(layer.user, "nico")
        self.assertEqual(layer.host, "health.touplitoui.me")
        self.assertEqual((layer.x, layer.y, layer.w, layer.h), ("0", "0", "100%", "100%"))

    def test_options(self):
        layer = L.parse_spec("http://localhost:8080/hud.html|x=70%,y=5%,w=28%,h=40%,"
                             "opacity=0.85,through,zoom=1.2,refresh=60")
        self.assertEqual(layer.url, "http://localhost:8080/hud.html")
        self.assertIsNone(layer.user)
        self.assertEqual((layer.x, layer.y, layer.w, layer.h), ("70%", "5%", "28%", "40%"))
        self.assertEqual(layer.opacity, 0.85)
        self.assertTrue(layer.through)
        self.assertEqual(layer.zoom, 1.2)
        self.assertEqual(layer.refresh, 60)

    def test_port_query_and_fragment_kept(self):
        layer = L.parse_spec("https://nico@host.example:8443/a/b?x=1#top|w=50%")
        self.assertEqual(layer.url, "https://host.example:8443/a/b?x=1#top")
        self.assertEqual(layer.user, "nico")
        self.assertEqual(layer.w, "50%")

    def test_bool_values(self):
        self.assertFalse(L.parse_spec("http://a.example|transparent=0").transparent)
        self.assertTrue(L.parse_spec("http://a.example|transparent=oui").transparent)
        self.assertFalse(L.parse_spec("http://a.example|through=non").through)

    def test_errors(self):
        for spec in ("health.touplitoui.me",             # pas de http://
                     "http://a.example|foo=1",           # option inconnue
                     "http://a.example|opacity=2",       # hors limites
                     "http://a.example|w=abc",           # taille invalide
                     "http://a.example|through=peut-etre",
                     "http://a.example|zoom=0"):
            with self.subTest(spec=spec), self.assertRaises(L.LayerError):
                L.parse_spec(spec)


class Builtin(unittest.TestCase):
    def test_stats_page(self):
        layer = L.parse_spec("stats|x=1%,w=460")
        self.assertTrue(layer.builtin)
        self.assertEqual(layer.url, "builtin:/stats.html")
        self.assertIsNone(layer.user)
        self.assertEqual(layer.w, "460")

    def test_stats_with_query(self):
        layer = L.parse_spec("stats?city=Paris|h=230")
        self.assertEqual(layer.url, "builtin:/stats.html?city=Paris")
        self.assertEqual(L.from_dict({"url": "stats?lat=48.8&lon=2.3"}).url,
                         "builtin:/stats.html?lat=48.8&lon=2.3")

    def test_normal_urls_are_not_builtin(self):
        self.assertFalse(L.parse_spec("https://stats.example.com").builtin)


class Finalize(unittest.TestCase):
    def test_only_first_layer_opaque_by_default(self):
        layers = L.finalize([L.parse_spec("http://a.example"),
                             L.parse_spec("http://b.example"),
                             L.parse_spec("http://c.example|transparent=0")])
        self.assertEqual([l.transparent for l in layers], [False, True, False])


class Place(unittest.TestCase):
    def test_percent_and_pixels(self):
        layer = L.parse_spec("http://a.example|x=70%,y=5%,w=28%,h=40%")
        self.assertEqual(L.place(layer, 1920, 1080), (1344, 54, 538, 432))
        layer = L.parse_spec("http://a.example|x=100,y=50,w=400,h=300")
        self.assertEqual(L.place(layer, 1920, 1080), (100, 50, 400, 300))

    def test_clamped_to_screen(self):
        layer = L.parse_spec("http://a.example|x=1800,y=-20,w=500,h=2000")
        self.assertEqual(L.place(layer, 1920, 1080), (1800, 0, 120, 1080))

    def test_fullscreen(self):
        self.assertTrue(L.is_fullscreen(L.parse_spec("http://a.example"), 1920, 1080))
        self.assertFalse(L.is_fullscreen(L.parse_spec("http://a.example|w=50%"), 1920, 1080))


class Composer(unittest.TestCase):
    def test_frames_in_order_with_styles(self):
        layers = L.finalize([
            L.parse_spec("http://a.example"),
            L.parse_spec("http://b.example/hud.html|x=70%,y=10,w=400,h=30%,opacity=0.8"),
            L.parse_spec('http://c.example/?q=1&x="y"|through,zoom=2,refresh=30'),
        ])
        page = L.composer_html(layers[1:])
        self.assertLess(page.index("b.example"), page.index("c.example"))
        self.assertIn("left:70%;top:10px;width:calc(400px / 1);height:calc(30% / 1);z-index:1;"
                      "opacity:0.8;", page)
        self.assertIn("transform:scale(2);pointer-events:none;", page)
        self.assertIn('data-refresh="30"', page)
        self.assertIn("q=1&amp;x=&quot;y&quot;", page)   # adresse échappée
        self.assertNotIn('x="y"', page)

    def test_css_size(self):
        self.assertEqual(L.css_size("50%"), "50%")
        self.assertEqual(L.css_size("300"), "300px")
        self.assertEqual(L.css_size("12.5"), "12.5px")


class Config(unittest.TestCase):
    def write(self, data):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(data, f)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_list_and_object_forms(self):
        items = [{"url": "https://nico@health.touplitoui.me"},
                 {"url": "http://localhost:8080/hud.html", "x": "70%", "w": "28%",
                  "opacity": 0.8, "through": True}]
        for data in (items, {"layers": items}):
            layers = L.load_config(self.write(data))
            self.assertEqual(len(layers), 2)
            self.assertEqual(layers[0].user, "nico")
            self.assertEqual(layers[1].opacity, 0.8)
            self.assertTrue(layers[1].through)

    def test_example_file_is_valid(self):
        path = os.path.join(os.path.dirname(__file__), "..", "layers.example.json")
        self.assertGreaterEqual(len(L.load_config(path)), 2)

    def test_bad_config(self):
        for data in ([], {"layers": "x"}, [{"x": "10%"}], [{"url": "http://a.example", "bad": 1}]):
            with self.subTest(data=data), self.assertRaises(L.LayerError):
                L.load_config(self.write(data))
        with self.assertRaises(L.LayerError):
            L.load_config("/nonexistent/layers.json")


if __name__ == "__main__":
    unittest.main()
