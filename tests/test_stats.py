import json
import os
import sys
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import stats  # noqa: E402


class Readers(unittest.TestCase):
    def test_counters(self):
        busy, total = stats.cpu_times()
        self.assertGreater(total, 0)
        self.assertLessEqual(busy, total)
        mem = stats.memory()
        self.assertGreater(mem["total"], 0)
        self.assertTrue(0 <= mem["percent"] <= 100)
        self.assertEqual(len(stats.net_bytes()), 2)
        self.assertEqual(len(stats.disk_bytes()), 2)
        self.assertTrue(stats.mounts())
        temp = stats.cpu_temp()
        self.assertTrue(temp is None or 0 < temp < 150)


class Server(unittest.TestCase):
    def test_serves_page_and_stats(self):
        base = stats.start_server(port=18765)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        page = opener.open(base + "/stats.html").read().decode()
        self.assertIn("/api/stats", page)
        time.sleep(1.5)
        data = json.loads(opener.open(base + "/api/stats").read())
        for key in ("cpu", "mem", "net", "disk"):
            self.assertIn(key, data)
        self.assertTrue(0 <= data["cpu"]["percent"] <= 100)
        with self.assertRaises(urllib.error.HTTPError):
            opener.open(base + "/../stats.py")


if __name__ == "__main__":
    unittest.main()
