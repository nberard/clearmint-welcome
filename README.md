# clearmint-welcome

Pages web superposées en **fond d'écran interactif** pour Linux Mint (Cinnamon, X11).
À l'ouverture de session, on arrive sur ses tableaux de bord : ils restent derrière
toutes les fenêtres, comme un fond d'écran, mais restent cliquables.

## Installation

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-webkit2-4.1 gir1.2-secret-1
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
  "http://localhost:8080/hud.html|x=70%,y=5%,w=28%,h=40%,opacity=0.85,transparent" \
  "http://localhost:8080/fx.html|transparent,through"

# avec le panneau système intégré (météo, CPU, mémoire, réseau, disques)
python3 desktop_dashboard.py \
  https://nico@health.touplitoui.me \
  "stats?city=Nîmes|x=1%,y=2%,w=640,h=200"

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
| `transparent` | fond de page transparent, pour voir les calques du dessous | oui pour `stats`, `clock`, `procs` ; non pour les sites |
| `through` | les clics traversent le calque vers ceux du dessous | non |
| `zoom` | zoom de la page | `1` |
| `refresh` | recharge la page toutes les N secondes | `0` (jamais) |

- Un calque n'est vraiment transparent que si **la page elle-même** a un fond
  transparent (`body { background: transparent }`). Les sites classiques ont un fond
  blanc : réduis-les avec `x/y/w/h` ou baisse leur `opacity`.
- Les calques transparents sont réunis dans une seule page (dans des cadres) et
  affichés **au-dessus** des calques opaques. Certains sites refusent d'être affichés
  dans un cadre (en-tête `X-Frame-Options`, Grafana par défaut) : laisse-les opaques.
- Les clics vont au calque transparent sous la souris ; là où il n'y en a pas (ou
  sur un calque `through`), ils passent aux calques du dessous.

### Panneaux intégrés

| Nom | Contenu |
|---|---|
| `stats` | météo, CPU, mémoire, réseau, disques |
| `clock` | heure, date, salutation (`clock?name=Nico`), infos système, batterie |
| `procs` | programmes les plus gourmands en CPU et en mémoire |
| `grid` | fond animé (opaque) : à mettre en premier |

Exemple : fond animé, Grafana à droite, panneaux à gauche :

```bash
python3 desktop_dashboard.py grid \
  "https://nico@health.touplitoui.me|x=50%,w=50%" \
  "stats?city=Nîmes|x=1%,y=2%,w=47%,h=20%" \
  "clock?name=Nico|x=1%,y=24%,w=47%,h=22%" \
  "procs|x=1%,y=48%,w=47%,h=48%"
```

### Panneau système : `stats`

Météo, CPU (charge et température), mémoire, réseau, disques (activité et place
libre sur `/` et `/home`), en style HUD sur fond transparent, mis à jour chaque
seconde. Les mesures sont lues directement dans le système, aucun service extérieur
sauf la météo (Open-Meteo, gratuit, sans clé).

- `stats?city=Nîmes` : météo pour une ville ;
- `stats?lat=48.85&lon=2.35` : météo pour des coordonnées ;
- `stats` : sans météo.

Pour l'essayer seul dans un navigateur : `python3 stats.py`, puis
http://127.0.0.1:8765/stats.html

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
