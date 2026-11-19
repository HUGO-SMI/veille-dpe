# Veille DPE – secteur Monteux

Chaque matin, GitHub Actions interroge l'open data ADEME (DPE logements existants),
repère les nouveaux DPE sur le secteur, envoie un mail récap et met à jour la page web.

- Communes suivies : `config.json` (codes INSEE) – Monteux, Althen-des-Paluds,
  Loriol-du-Comtat, Pernes-les-Fontaines, Entraigues-sur-la-Sorgue.
- Historique : `data/historique.json` · Page web : `docs/index.html`
- Lancer à la main : onglet **Actions** > **Veille DPE** > **Run workflow**.
- Test local : `python veille_dpe.py --apercu` (écrit `apercu_mail.html`, n'envoie rien).

## Secrets GitHub à créer (Settings > Secrets and variables > Actions)
| Nom | Valeur |
|---|---|
| `SMTP_USER` | l'adresse Gmail qui envoie |
| `SMTP_PASS` | le mot de passe d'application Gmail (16 lettres) |
| `MAIL_TO` | l'adresse qui reçoit (plusieurs : séparées par une virgule) |

## Vente ou location ?
L'ADEME n'enregistre pas le motif du DPE : le verdict est une estimation
(type de bien, surface, étiquette F/G, n° RPLS logement social, plusieurs DPE à la même adresse).
