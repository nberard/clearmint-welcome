#!/usr/bin/env python3
"""Affiche une page web comme fond d'écran interactif (une fenêtre par écran)."""
import sys
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk, Gdk, WebKit2

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"

def make_window(monitor):
    geo = monitor.get_geometry()
    win = Gtk.Window()
    win.set_type_hint(Gdk.WindowTypeHint.DESKTOP)  # se comporte comme le bureau
    win.set_decorated(False)
    win.set_skip_taskbar_hint(True)
    win.set_skip_pager_hint(True)
    win.set_keep_below(True)
    win.move(geo.x, geo.y)
    win.set_default_size(geo.width, geo.height)
    view = WebKit2.WebView()
    view.load_uri(URL)
    win.add(view)
    win.show_all()
    win.stick()  # visible sur tous les espaces de travail
    return win

display = Gdk.Display.get_default()
windows = [make_window(display.get_monitor(i)) for i in range(display.get_n_monitors())]
Gtk.main()
