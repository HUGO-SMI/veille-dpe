"""Veille des nouveaux DPE ADEME sur un secteur de communes.

- Interroge l'API open data ADEME (dataset dpe03existant = DPE logements existants)
- Repère les DPE jamais vus (fichier data/vus.json)
- Construit une fiche par DPE : infos clés, Google Maps, Street View, fiche ADEME,
  estimation vente / location
- Envoie un mail récap (si SMTP configuré) et régénère la page web docs/index.html

Usage :
  python veille_dpe.py              # run normal (mail + page)
  python veille_dpe.py --no-mail    # sans envoi de mail
  python veille_dpe.py --apercu     # écrit apercu_mail.html au lieu d'envoyer
"""
import html
import json
import math
import os
import smtplib
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"
API = "https://data.ademe.fr/data-fair/api/v1/datasets/dpe03existant/lines"

CHAMPS = [
    "numero_dpe", "date_reception_dpe", "date_etablissement_dpe", "date_fin_validite_dpe",
    "type_batiment", "annee_construction", "periode_construction", "surface_habitable_logement",
    "nombre_niveau_logement", "numero_etage_appartement", "nom_residence",
    "complement_adresse_batiment", "complement_adresse_logement",
    "adresse_ban", "nom_commune_ban", "code_insee_ban", "code_postal_ban",
    "coordonnee_cartographique_x_ban", "coordonnee_cartographique_y_ban", "score_ban",
    "etiquette_dpe", "etiquette_ges", "conso_5_usages_par_m2_ep", "emission_ges_5_usages_par_m2",
    "type_energie_principale_chauffage", "type_generateur_chauffage_principal",
    "type_energie_principale_ecs", "cout_total_5_usages",
    "numero_rpls_logement", "numero_dpe_remplace", "numero_immatriculation_copropriete",
    "methode_application_dpe", "modele_dpe",
]

COULEURS = {"A": "#009c6d", "B": "#52b153", "C": "#a5cc74", "D": "#f4e70f",
            "E": "#f0b50f", "F": "#eb8235", "G": "#d7221f"}


# ---------------------------------------------------------------- données
def charger_config():
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def lire_json(path, defaut):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return defaut


def ecrire_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def recuperer_dpe(codes_insee, depuis):
    qs = "code_insee_ban:(%s) AND date_reception_dpe:[%s TO *]" % (" OR ".join(codes_insee), depuis)
    params = {"qs": qs, "size": 500, "sort": "-date_reception_dpe", "select": ",".join(CHAMPS)}
    url = API + "?" + urllib.parse.urlencode(params)
    lignes = []
    while url:
        req = urllib.request.Request(url, headers={"User-Agent": "veille-dpe/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            page = json.load(r)
        lignes += page.get("results", [])
        url = page.get("next") if page.get("results") else None
    return lignes


# ---------------------------------------------------------------- géo
def lambert93_vers_wgs84(x, y):
    """Conversion Lambert-93 -> lat/lon (formules IGN, précision métrique)."""
    n = 0.7256077650532670
    c = 11754255.426096
    xs, ys = 700000.0, 12655612.049876
    e = 0.0818191910428158
    lon0 = math.radians(3)
    r = math.hypot(x - xs, y - ys)
    gamma = math.atan((x - xs) / (ys - y))
    lon = lon0 + gamma / n
    lat_iso = -1 / n * math.log(abs(r / c))
    lat = 2 * math.atan(math.exp(lat_iso)) - math.pi / 2
    for _ in range(10):
        lat = 2 * math.atan(((1 + e * math.sin(lat)) / (1 - e * math.sin(lat))) ** (e / 2)
                            * math.exp(lat_iso)) - math.pi / 2
    return math.degrees(lat), math.degrees(lon)


def liens(d):
    adresse = d.get("adresse_ban") or ""
    out = {
        "maps": "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(adresse),
        "ademe": "https://observatoire-dpe-audit.ademe.fr/afficher-dpe/" + d["numero_dpe"],
        "streetview": None,
    }
    x, y = d.get("coordonnee_cartographique_x_ban"), d.get("coordonnee_cartographique_y_ban")
    if x and y:
        lat, lon = lambert93_vers_wgs84(float(x), float(y))
        out["streetview"] = ("https://www.google.com/maps/@?api=1&map_action=pano&viewpoint="
                             f"{lat:.6f},{lon:.6f}")
        out["maps_gps"] = f"https://www.google.com/maps/search/?api=1&query={lat:.6f},{lon:.6f}"
    return out


# ---------------------------------------------------------------- vente / location
def estimer_projet(d, nb_meme_adresse):
    """Renvoie (verdict, confiance, raisons). Score > 0 = vente, < 0 = location."""
    raisons, score = [], 0
    if d.get("numero_rpls_logement"):
        return "Location (logement social)", "forte", ["N° RPLS présent : logement de bailleur social"]

    t = (d.get("type_batiment") or "").lower()
    s = d.get("surface_habitable_logement") or 0
    etiq = d.get("etiquette_dpe")

    if t == "maison":
        score += 2; raisons.append("Maison : majoritairement des ventes")
    elif t == "appartement":
        score -= 1; raisons.append("Appartement : souvent de la location")
    if s and s < 40:
        score -= 2; raisons.append(f"Petite surface ({s:.0f} m²) : profil locatif")
    elif s and s < 60:
        score -= 1; raisons.append(f"Surface moyenne ({s:.0f} m²)")
    elif s and s >= 100:
        score += 1; raisons.append(f"Grande surface ({s:.0f} m²)")
    if etiq == "G":
        score += 3; raisons.append("Étiquette G : location interdite depuis 2025 → plutôt vente")
    elif etiq == "F":
        score += 1; raisons.append("Étiquette F : location interdite en 2028")
    if nb_meme_adresse >= 3:
        score -= 2; raisons.append(f"{nb_meme_adresse} DPE récents à la même adresse : bailleur / immeuble")
    if d.get("numero_dpe_remplace"):
        raisons.append("Remplace un DPE antérieur (refait récemment)")

    if score >= 2:
        return "Vente probable", "moyenne" if score < 4 else "forte", raisons
    if score <= -2:
        return "Location probable", "moyenne" if score > -4 else "forte", raisons
    return "Indécis", "faible", raisons


# ---------------------------------------------------------------- fiches
def construire_fiche(d, nb_meme_adresse):
    verdict, confiance, raisons = estimer_projet(d, nb_meme_adresse)
    return {**d, "liens": liens(d), "verdict": verdict, "confiance": confiance, "raisons": raisons}


def fmt_date(s):
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except (TypeError, ValueError):
        return s or "?"


def badge(lettre):
    if not lettre:
        return "?"
    fg = "#000" if lettre in "CDE" else "#fff"
    return (f'<span style="display:inline-block;min-width:22px;text-align:center;font-weight:700;'
            f'border-radius:4px;padding:1px 6px;background:{COULEURS.get(lettre, "#999")};color:{fg}">'
            f"{lettre}</span>")


def fiche_html(f):
    e = html.escape
    l = f["liens"]
    surface = f.get("surface_habitable_logement")
    details = []
    if f.get("numero_etage_appartement") is not None and f.get("type_batiment") == "appartement":
        details.append(f"Étage {f['numero_etage_appartement']}")
    for k in ("nom_residence", "complement_adresse_batiment", "complement_adresse_logement"):
        if f.get(k):
            details.append(e(str(f[k])))
    couleur_v = {"Vente": "#0b6e4f", "Loca": "#1f4e9c"}.get(f["verdict"][:4], "#666")
    boutons = [f'<a href="{l["maps"]}" style="{BTN}">📍 Google Maps</a>']
    if l.get("streetview"):
        boutons.append(f'<a href="{l["streetview"]}" style="{BTN}">👁 Street View</a>')
    boutons.append(f'<a href="{l["ademe"]}" style="{BTN}">📄 Fiche ADEME</a>')
    return f"""
<div class="fiche" data-commune="{e(f.get('nom_commune_ban') or '')}" data-verdict="{e(f['verdict'])}"
 style="border:1px solid #ddd;border-radius:10px;padding:14px 16px;margin:0 0 14px;background:#fff;color:#222">
 <div style="font-size:12px;color:#777">DPE n°{e(f['numero_dpe'])} · reçu le {fmt_date(f.get('date_reception_dpe'))}</div>
 <div style="font-size:17px;font-weight:700;margin:4px 0 2px">{e(f.get('adresse_ban') or '?')}</div>
 <div style="font-size:13px;color:#555">{' · '.join(details)}</div>
 <table style="font-size:14px;margin:10px 0;border-collapse:collapse">
  <tr><td style="padding:2px 12px 2px 0;color:#777">Type</td><td>{e((f.get('type_batiment') or '?').capitalize())}</td>
      <td style="padding:2px 12px 2px 24px;color:#777">Surface</td><td>{f'{surface:.1f} m²' if surface else '?'}</td></tr>
  <tr><td style="padding:2px 12px 2px 0;color:#777">Construction</td><td>{e(str(f.get('annee_construction') or f.get('periode_construction') or '?'))}</td>
      <td style="padding:2px 12px 2px 24px;color:#777">DPE / GES</td><td>{badge(f.get('etiquette_dpe'))} {badge(f.get('etiquette_ges'))}</td></tr>
  <tr><td style="padding:2px 12px 2px 0;color:#777">Conso</td><td>{f.get('conso_5_usages_par_m2_ep') or '?'} kWhEP/m²/an</td>
      <td style="padding:2px 12px 2px 24px;color:#777">GES</td><td>{f.get('emission_ges_5_usages_par_m2') or '?'} kgCO₂/m²/an</td></tr>
  <tr><td style="padding:2px 12px 2px 0;color:#777">Chauffage</td><td colspan="3">{e(f.get('type_generateur_chauffage_principal') or f.get('type_energie_principale_chauffage') or '?')}</td></tr>
  <tr><td style="padding:2px 12px 2px 0;color:#777">Établi le</td><td>{fmt_date(f.get('date_etablissement_dpe'))}</td>
      <td style="padding:2px 12px 2px 24px;color:#777">Valable</td><td>jusqu'au {fmt_date(f.get('date_fin_validite_dpe'))}</td></tr>
 </table>
 <div style="margin:8px 0;padding:8px 10px;border-radius:6px;background:#f4f6f8">
  <b style="color:{couleur_v}">🔎 {e(f['verdict'])}</b> <span style="font-size:12px;color:#777">(confiance {f['confiance']})</span>
  <div style="font-size:12px;color:#555;margin-top:3px">{'<br>'.join('• ' + e(r) for r in f['raisons']) or '• Pas assez d’indices'}</div>
 </div>
 <div>{' '.join(boutons)}</div>
</div>"""


BTN = ("display:inline-block;margin:4px 6px 0 0;padding:6px 10px;border-radius:6px;background:#1a3d6d;"
       "color:#fff;text-decoration:none;font-size:13px")


def mail_html(fiches, communes):
    par_commune = {}
    for f in fiches:
        par_commune.setdefault(f.get("nom_commune_ban") or "?", []).append(f)
    corps = "".join(
        f'<h3 style="margin:22px 0 8px;color:#1a3d6d">{html.escape(c)} ({len(l)})</h3>' + "".join(fiche_html(f) for f in l)
        for c, l in sorted(par_commune.items(), key=lambda x: -len(x[1])))
    return f"""<html><body style="font-family:Arial,sans-serif;background:#f0f2f5;padding:16px;margin:0">
<div style="max-width:680px;margin:auto">
<h2 style="color:#1a3d6d;margin:0 0 4px">🏠 {len(fiches)} nouveau(x) DPE sur votre secteur</h2>
<div style="font-size:13px;color:#666;margin-bottom:10px">{', '.join(communes)} · {date.today().strftime('%d/%m/%Y')}</div>
{corps}
<div style="font-size:11px;color:#999;margin-top:20px">Source : ADEME open data (DPE logements existants). L'estimation vente / location est un indice, pas une certitude.</div>
</div></body></html>"""


def page_html(fiches, communes):
    options = "".join(f"<option>{html.escape(c)}</option>" for c in communes)
    cartes = "".join(fiche_html(f) for f in fiches)
    maj = datetime.now().strftime("%d/%m/%Y %H:%M")
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>Veille DPE secteur</title></head>
<body style="font-family:Arial,sans-serif;background:#f0f2f5;margin:0;padding:16px">
<div style="max-width:720px;margin:auto">
<h1 style="color:#1a3d6d;font-size:22px;margin:0">🏠 Veille DPE – secteur Monteux</h1>
<div style="font-size:13px;color:#666;margin:4px 0 12px">{len(fiches)} DPE · mise à jour {maj}</div>
<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px">
 <select id="c" style="padding:6px"><option value="">Toutes les communes</option>{options}</select>
 <select id="v" style="padding:6px"><option value="">Vente + location</option><option>Vente probable</option>
  <option>Location probable</option><option>Location (logement social)</option><option>Indécis</option></select>
 <input id="q" placeholder="Rechercher une rue…" style="padding:6px;flex:1;min-width:140px">
</div>
<div id="liste">{cartes}</div></div>
<script>
const f=()=>{{const c=document.getElementById('c').value,v=document.getElementById('v').value,q=document.getElementById('q').value.toLowerCase();
document.querySelectorAll('.fiche').forEach(e=>{{e.style.display=(!c||e.dataset.commune===c)&&(!v||e.dataset.verdict===v)&&(!q||e.textContent.toLowerCase().includes(q))?'':'none'}})}};
['c','v','q'].forEach(i=>document.getElementById(i).addEventListener('input',f));
</script></body></html>"""


# ---------------------------------------------------------------- mail
def envoyer_mail(sujet, corps_html):
    user, pwd, dest = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS"), os.environ.get("MAIL_TO")
    if not (user and pwd and dest):
        print("SMTP non configuré : mail non envoyé.")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = sujet, user, dest
    msg.attach(MIMEText(corps_html, "html", "utf-8"))
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    with smtplib.SMTP_SSL(host, int(os.environ.get("SMTP_PORT", 465))) as s:
        s.login(user, pwd)
        s.sendmail(user, [a.strip() for a in dest.split(",")], msg.as_string())
    print(f"Mail envoyé à {dest}")


# ---------------------------------------------------------------- main
def main():
    args = set(sys.argv[1:])
    cfg = charger_config()
    communes = cfg["communes"]
    depuis = (date.today() - timedelta(days=cfg["jours_a_scanner"])).isoformat()

    lignes = recuperer_dpe(list(communes), depuis)
    print(f"{len(lignes)} DPE reçus depuis le {depuis}")

    historique = {d["numero_dpe"]: d for d in lire_json(DATA / "historique.json", [])}
    premier_run = not historique
    nouveaux_bruts = [d for d in lignes if d["numero_dpe"] not in historique]
    for d in lignes:
        historique[d["numero_dpe"]] = d

    limite = (date.today() - timedelta(days=cfg["jours_historique_page"])).isoformat()
    tous = sorted((d for d in historique.values() if (d.get("date_reception_dpe") or "") >= limite),
                  key=lambda d: d.get("date_reception_dpe") or "", reverse=True)
    compte_adresse = {}
    for d in tous:
        compte_adresse[d.get("adresse_ban")] = compte_adresse.get(d.get("adresse_ban"), 0) + 1

    fiches = [construire_fiche(d, compte_adresse.get(d.get("adresse_ban"), 1)) for d in tous]
    nouveaux_ids = {d["numero_dpe"] for d in nouveaux_bruts}
    nouvelles = [f for f in fiches if f["numero_dpe"] in nouveaux_ids]
    print(f"{len(nouvelles)} nouveau(x) DPE" + (" (premier lancement : initialisation)" if premier_run else ""))

    ecrire_json(DATA / "historique.json", tous)
    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(page_html(fiches, communes.values()), encoding="utf-8")

    if "--apercu" in args:
        (ROOT / "apercu_mail.html").write_text(mail_html(nouvelles, communes.values()), encoding="utf-8")
        print("Aperçu écrit dans apercu_mail.html")
    elif nouvelles and "--no-mail" not in args and not premier_run:
        envoyer_mail(f"🏠 {len(nouvelles)} nouveau(x) DPE – secteur Monteux", mail_html(nouvelles, communes.values()))


if __name__ == "__main__":
    main()
