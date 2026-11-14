# clearmint-welcome

Pages web superposées en **fond d'écran interactif** pour Linux Mint (Cinnamon, X11).
À l'ouverture de session, on arrive sur ses tableaux de bord : ils restent derrière
toutes les fenêtres, comme un fond d'écran, mais restent cliquables.

## Installation

```bash
sudo apt install python3-gi gir1.2-webkit2-4.1 gir1.2-secret-1
```

Désactive les icônes du bureau de Mint (**Paramètres → Bureau**), sinon elles
recouvrent les tableaux de bord.

## Utilisation

```bash
# une seule page
python3 desktop_dashboard.py https://nico@health.touplitoui.me

# plusieurs pages empilées : la première est le fond, les suivantes vont par-dessus
python3 desktop_dashboard.py \
  https://nico@health.touplitoui.me \
  "http://localhost:8080/hud.html|x=70%,y=5%,w=28%,h=40%,opacity=0.85" \
  "http://localhost:8080/fx.html|through"

# ou depuis un fichier
python3 desktop_dashboard.py --config layers.example.json

# test dans une fenêtre normale (déplaçable, redimensionnable)
python3 desktop_dashboard.py --config layers.example.json --window
```

### Options d'un calque

En ligne de commande : `"URL|option,option=valeur"`. Dans le fichier JSON : les mêmes
noms (voir `layers.example.json`).

| Option | Rôle | Défaut |
|---|---|---|
| `x`, `y` | position, en pixels (`300`) ou en % de l'écran (`70%`) | `0` |
| `w`, `h` | taille, en pixels ou en % | `100%` |
| `opacity` | opacité de 0 à 1 | `1` |
| `transparent` | fond de page transparent, pour voir les calques du dessous | oui, sauf pour le 1er calque |
| `through` | les clics traversent le calque vers ceux du dessous | non |
| `zoom` | zoom de la page | `1` |
| `refresh` | recharge la page toutes les N secondes | `0` (jamais) |

- Un calque n'est vraiment transparent que si **la page elle-même** a un fond
  transparent (`body { background: transparent }`). Les sites classiques ont un fond
  blanc : réduis-les avec `x/y/w/h` ou baisse leur `opacity`.
- Un calque plein écran qui n'a pas `through` capte tous les clics : les calques du
  dessous ne sont plus cliquables.

## Connexion

- **Authentification HTTP** : mets l'identifiant dans l'adresse (`https://nico@...`).
  Le mot de passe est demandé une fois, puis rangé dans le trousseau de Mint.
  `--forget` l'efface.
- **Page de connexion (formulaire)** : les cookies sont conservés entre deux démarrages.

## Démarrage automatique

`~/.config/autostart/desktop-dashboard.desktop` :

```ini
[Desktop Entry]
Type=Application
Name=Desktop Dashboard
Exec=python3 /home/nicks/dev/mint-overview/desktop_dashboard.py --config /home/nicks/dev/mint-overview/layers.json
X-GNOME-Autostart-delay=5
```

## Tests

```bash
python3 -m unittest discover -s tests
```
