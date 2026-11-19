#!/usr/bin/env python3
"""
Pige immobilière – secteur Monteux
Lit les alertes e-mail SeLoger, Bien'ici et LeBonCoin reçues sur la boîte Gmail (IMAP),
extrait chaque annonce (photo, prix, type, surface, ville, lien, vendeur si indiqué)
et met à jour docs/pige.json, lu par l'onglet « Piges » du back office.

Variables d'environnement (secrets GitHub) :
  IMAP_USER  adresse Gmail qui reçoit les alertes
  IMAP_PASS  mot de passe d'application Gmail (16 lettres)
Test local sans boîte mail :  python pige.py --eml chemin/vers/alerte.eml [autre.eml ...]
"""
import email, email.policy, hashlib, imaplib, json, os, re, sys, unicodedata
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlparse, parse_qs, unquote
from urllib.request import Request, build_opener, HTTPRedirectHandler

ICI = os.path.dirname(os.path.abspath(__file__))
SORTIE = os.path.join(ICI, "docs", "pige.json")
JOURS_SCAN = 30          # profondeur de lecture des mails à chaque passage
JOURS_HISTORIQUE = 180   # durée de conservation des annonces dans la pige

SOURCES = {"seloger": "SeLoger", "bienici": "Bien'ici", "leboncoin": "LeBonCoin"}

# ----------------------------------------------------------------------------
# communes suivies (les mêmes que la veille DPE) + codes postaux
# ----------------------------------------------------------------------------
def charger_communes():
    with open(os.path.join(ICI, "config.json"), encoding="utf-8") as f:
        return list(json.load(f)["communes"].values())

def norm(s):
    s = unicodedata.normalize("NFD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()

# ----------------------------------------------------------------------------
# mini-DOM à partir du HTML du mail (bibliothèque standard uniquement)
# ----------------------------------------------------------------------------
VIDES = {"img", "br", "hr", "meta", "link", "input", "col", "area", "base", "wbr", "source"}

class Noeud:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent, self.enfants = tag, dict(attrs), parent, []
    def texte(self):
        morceaux = []
        def go(n):
            for c in n.enfants:
                if isinstance(c, str): morceaux.append(c)
                elif c.tag not in ("style", "script", "head", "title"): go(c)
                if not isinstance(c, str) and c.tag in ("br", "p", "div", "td", "tr", "li", "h1", "h2", "h3", "h4", "table"): morceaux.append("\n")
        go(self)
        t = unescape("".join(morceaux)).replace("\xa0", " ").replace(" ", " ")
        return "\n".join(l.strip() for l in t.splitlines() if l.strip())
    def tous(self, tag):
        r = []
        def go(n):
            for c in n.enfants:
                if not isinstance(c, str):
                    if c.tag == tag: r.append(c)
                    go(c)
        go(self); return r

class Constructeur(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.racine = Noeud("racine", {}, None); self.cour = self.racine
    def handle_starttag(self, tag, attrs):
        n = Noeud(tag, attrs, self.cour); self.cour.enfants.append(n)
        if tag not in VIDES: self.cour = n
    def handle_endtag(self, tag):
        n = self.cour
        while n is not self.racine and n.tag != tag: n = n.parent
        if n is not self.racine: self.cour = n.parent
    def handle_data(self, data): self.cour.enfants.append(data)

def dom(html):
    p = Constructeur(); p.feed(html); return p.racine

# ----------------------------------------------------------------------------
# extraction des annonces d'un mail d'alerte
# ----------------------------------------------------------------------------
RE_PRIX = re.compile(r"(\d{1,3}(?:[ .]\d{3})+|\d{4,})\s*€(?!\s*/)")
RE_PRIX_M2 = re.compile(r"(\d{1,3}(?:[ .]\d{3})*|\d+)\s*€\s*/\s*m")
RE_PIECES = re.compile(r"(\d+)\s*pi[eè]ces?", re.I)
RE_CHAMBRES = re.compile(r"(\d+)\s*chambres?", re.I)
RE_M2 = re.compile(r"(\d{1,3}(?:[ .]\d{3})*(?:,\d+)?)\s*m²")
RE_CP_VILLE = re.compile(r"\b(\d{5})\s+([A-ZÀ-Ü][\w' \-À-ÿ]+)")
RE_VILLE_CP = re.compile(r"([A-ZÀ-Ü][\w' \-À-ÿ]+?)\s*\((\d{5})\)")
MOTS_LIEN = ("voir l'annonce", "voir l’annonce", "voir le bien", "découvrir", "voir l'offre")

def nombre(s):
    return int(re.sub(r"[^\d]", "", s)) if s else None

def est_logo(src, alt):
    s = (src + " " + (alt or "")).lower()
    return any(k in s for k in ("logo", "icon", "picto", "pixel", "tracking", "spacer", "social", "facebook", "instagram", "appstore", "googleplay", "arrow", "fleche", "badge", "1x1"))

def deviner_type(txt):
    t = norm(txt)
    if re.search(r"\b(appartement|appart|studio|duplex|triplex|loft|t[1-7]|f[1-7])\b", t): return "Appartement"
    if re.search(r"\b(maison|villa|mas|bastide|propriete|pavillon|longere|ferme|chalet|demeure|bergerie)\b", t): return "Maison"
    if re.search(r"\bterrain\b", t): return "Terrain"
    return "Autre"

def lien_reel(url):
    """Suit les redirections de suivi des mails pour retrouver l'adresse de l'annonce."""
    q = parse_qs(urlparse(url).query)
    for k in ("url", "u", "redirect", "target", "dest"):
        if k in q and q[k][0].startswith("http"): return unquote(q[k][0])
    if re.search(r"(seloger\.com/annonces|bienici\.com/annonce|leboncoin\.fr/(ad|ventes_immobilieres)/)", url): return url
    class Trouve(Exception): pass
    class Stop(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            self.dernier = newurl
            if re.search(r"(seloger\.com|bienici\.com|leboncoin\.fr)", newurl) and not re.search(r"(click|track|email|lnk)\.", urlparse(newurl).netloc):
                raise Trouve(newurl)
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    try:
        h = Stop(); build_opener(h).open(Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0 (pige)"}), timeout=12)
        return getattr(h, "dernier", url)
    except Trouve as e:
        return str(e.args[0])
    except Exception:
        return url

def identifiant(source, url, cle_secours):
    m = re.search(r"seloger\.com/annonces/.*?/(\d{6,})", url) or re.search(r"bienici\.com/annonce/[^/]+/[^/?]*?([\w-]{6,})(?:\?|$)", url) \
        or re.search(r"leboncoin\.fr/(?:ad/[^/]+|ventes_immobilieres)/(\d{6,})", url)
    base = m.group(1) if m else cle_secours
    return source[:2] + "-" + hashlib.sha1(base.encode()).hexdigest()[:12]

def carte_autour(a):
    """Remonte depuis le lien jusqu'au bloc qui contient une photo et un prix (la « carte » de l'annonce)."""
    n = a
    for _ in range(14):
        n = n.parent
        if n is None or n.tag == "racine": return None
        txt = n.texte()
        imgs = [i for i in n.tous("img") if not est_logo(i.attrs.get("src", ""), i.attrs.get("alt"))]
        if imgs and RE_PRIX.search(txt):
            # bloc trop grand (plusieurs annonces) : on s'arrête au niveau précédent
            if len(RE_PRIX.findall(txt)) > 2: return None
            return n
    return None

def extraire(html, source, recu_le):
    racine = dom(html)
    annonces, vus = [], set()
    for a in racine.tous("a"):
        href = a.attrs.get("href", "")
        lib = norm(a.texte()) + " " + norm(a.attrs.get("title", ""))
        if not href.startswith("http"): continue
        if not (any(norm(m) in lib for m in MOTS_LIEN) or a.tous("img")): continue
        carte = carte_autour(a)
        if carte is None or id(carte) in vus: continue
        vus.add(id(carte))
        txt = carte.texte()
        prix = RE_PRIX.search(txt)
        if not prix: continue
        imgs = [i.attrs.get("src", "") for i in carte.tous("img") if not est_logo(i.attrs.get("src", ""), i.attrs.get("alt"))]
        lignes = txt.split("\n")
        titre = next((l for l in lignes if len(l) > 12 and not RE_PRIX.search(l) and "€" not in l and not l.lower().startswith(("voir", "contacter", "localisation", "exclusivit", "nouveau"))), "")
        m2 = [nombre(x.split(",")[0]) for x in RE_M2.findall(txt)]
        cp = ville = quartier = ""
        mv = RE_VILLE_CP.search(txt)
        if mv:
            ville, cp = mv.group(1).strip(), mv.group(2)
            ligne = next((l for l in lignes if mv.group(0) in l), "")
            avant = ligne.split(mv.group(1))[0].strip(" ,–-")
            quartier = avant if 2 < len(avant) < 80 else ""
        else:
            mc = RE_CP_VILLE.search(txt)
            if mc: cp, ville = mc.group(1), mc.group(2).strip()
        ville = re.split(r"\s{2,}|\n| - ", ville)[0].strip()
        lien = lien_reel(href)
        vendeur = ""
        for l in lignes:
            if re.search(r"\b(agence|immobili[eè]re?|immo|notaire|particulier|properties|realty)\b", l, re.I) and len(l) < 60 and "€" not in l:
                vendeur = l; break
        pm2 = RE_PRIX_M2.search(txt)
        annonces.append({
            "id": identifiant(source, lien, (titre + prix.group(1) + ville).lower()),
            "source": SOURCES.get(source, source),
            "type": deviner_type(titre + " " + txt[:200]),
            "titre": titre[:140],
            "prix": nombre(prix.group(1)),
            "prix_m2": nombre(pm2.group(1)) if pm2 else None,
            "pieces": nombre(RE_PIECES.search(txt).group(1)) if RE_PIECES.search(txt) else None,
            "chambres": nombre(RE_CHAMBRES.search(txt).group(1)) if RE_CHAMBRES.search(txt) else None,
            "surface": m2[0] if m2 else None,
            "terrain": m2[1] if len(m2) > 1 else None,
            "ville": ville, "cp": cp, "quartier": quartier,
            "exclusivite": bool(re.search(r"exclusivit", txt, re.I)),
            "vendeur": vendeur,
            "photo": imgs[0] if imgs else "",
            "lien": lien,
            "recu_le": recu_le,
        })
        a = annonces[-1]
        if not a["prix_m2"] and a["prix"] and a["surface"]: a["prix_m2"] = round(a["prix"] / a["surface"])
    return annonces

def source_du_mail(msg):
    s = (msg.get("From", "") + " " + msg.get("Subject", "")).lower()
    for k in SOURCES:
        if k in s.replace("'", "").replace("’", ""): return k
    return None

def html_du_mail(msg):
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            return part.get_content()
    return ""

def date_du_mail(msg):
    try: return email.utils.parsedate_to_datetime(msg.get("Date")).astimezone(timezone.utc).isoformat()
    except Exception: return datetime.now(timezone.utc).isoformat()

# ----------------------------------------------------------------------------
# lecture de la boîte Gmail
# ----------------------------------------------------------------------------
def lire_boite(user, pwd):
    m = imaplib.IMAP4_SSL("imap.gmail.com")
    m.login(user, pwd)
    dossier = "INBOX"
    typ, lst = m.list()
    for l in (lst or []):
        l = l.decode(errors="ignore")
        if r"\All" in l:  # « Tous les messages » (nom variable selon la langue de Gmail)
            dossier = l.split(' "/" ')[-1]; break
    m.select(dossier, readonly=True)
    requete = f'from:(seloger OR bienici OR leboncoin) newer_than:{JOURS_SCAN}d'
    typ, data = m.search(None, "X-GM-RAW", f'"{requete}"')
    ids = data[0].split() if typ == "OK" else []
    print(f"{len(ids)} mails d'alerte trouvés ({JOURS_SCAN} derniers jours)")
    for i in ids:
        typ, d = m.fetch(i, "(RFC822)")
        if typ == "OK" and d and d[0]:
            yield email.message_from_bytes(d[0][1], policy=email.policy.default)
    m.logout()

# ----------------------------------------------------------------------------
def main():
    communes = charger_communes()
    zone = {norm(c) for c in communes}
    try:
        with open(SORTIE, encoding="utf-8") as f: ancien = {a["id"]: a for a in json.load(f).get("annonces", [])}
    except Exception:
        ancien = {}

    if "--eml" in sys.argv:
        fichiers = sys.argv[sys.argv.index("--eml") + 1:]
        mails = [email.message_from_binary_file(open(f, "rb"), policy=email.policy.default) for f in fichiers]
    else:
        user, pwd = os.environ.get("IMAP_USER"), os.environ.get("IMAP_PASS")
        if not user or not pwd:
            print("IMAP_USER / IMAP_PASS non configurés : rien à lire."); return
        mails = lire_boite(user, pwd)

    nouvelles = 0
    for msg in mails:
        src = source_du_mail(msg)
        if not src: continue
        for a in extraire(html_du_mail(msg), src, date_du_mail(msg)):
            a["zone"] = norm(a["ville"]) in zone
            if a["id"] in ancien:
                prix_avant = ancien[a["id"]].get("prix")
                garde = ancien[a["id"]]
                if a["prix"] and prix_avant and a["prix"] != prix_avant:
                    garde.setdefault("historique_prix", []).append({"date": a["recu_le"], "prix": a["prix"]})
                    garde["prix"] = a["prix"]
                for k, v in a.items():
                    if v and not garde.get(k): garde[k] = v
            else:
                a["vu_le"] = datetime.now(timezone.utc).isoformat()
                ancien[a["id"]] = a; nouvelles += 1

    limite = (datetime.now(timezone.utc) - timedelta(days=JOURS_HISTORIQUE)).isoformat()
    annonces = sorted((a for a in ancien.values() if a.get("recu_le", "") >= limite), key=lambda a: a.get("recu_le", ""), reverse=True)
    os.makedirs(os.path.dirname(SORTIE), exist_ok=True)
    with open(SORTIE, "w", encoding="utf-8") as f:
        json.dump({"maj": datetime.now(timezone.utc).isoformat(), "communes": communes, "annonces": annonces}, f, ensure_ascii=False, indent=1)
    print(f"{nouvelles} nouvelle(s) annonce(s) · {len(annonces)} au total dans la pige · {sum(1 for a in annonces if a.get('zone'))} dans la zone")

if __name__ == "__main__":
    main()
