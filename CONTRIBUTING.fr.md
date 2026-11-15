# Contribuer

*[English](CONTRIBUTING.md) · Français*

Le projet n'est pas maintenu (voir le [README](README.fr.md)), mais les
copies (forks) sont les bienvenues. Voici comment il est construit et
testé.

## Tests

```bash
cd backend
pip install -r requirements-test.txt
python -m pytest                       # tests du serveur
pip install -r requirements-browser.txt
python -m playwright install --with-deps chromium
python -m pytest tests_browser         # les pages, dans Chromium sans interface
```

Les tests demandent les mêmes paquets système que l'image Docker
(`tesseract-ocr` et les bibliothèques de WeasyPrint; voir le `Dockerfile`),
et chaque exécution utilise un dossier de données temporaire.

- [`backend/tests/README.md`](backend/tests/README.md) indique quels fichiers de test couvrent quelle
  fonction, et décrit le corpus d'importation (`python -m app.corpus`), qui
  relit de vraies sources après un changement et signale celles qui
  sortent moins bien.
- Chaque test de navigateur échoue aussi sur une erreur JavaScript ou une
  violation de la Content-Security-Policy. `test_accessibility.py` passe
  axe-core sur chaque écran (anglais/clair, français/sombre); la règle du
  zoom est désactivée, puisque le zoom l'est exprès.

## Intégration continue

L'intégration continue (`.github/workflows/ci.yml`) s'exécute à chaque
envoi, chaque dimanche et sur demande. Elle lance les deux suites de
tests, une vérification des noms non définis dans les scripts des pages
(ESLint), un audit des dépendances, et une construction Docker démarrée et
vérifiée. L'image n'est publiée qu'une fois ces étapes réussies;
l'exécution hebdomadaire la reconstruit donc avec l'image de base, les
racines et la version de Chrome les plus récentes (le nom de navigateur
qu'envoient les importations par lien). GitHub suspend les exécutions
planifiées après 60 jours sans commit.

## Organisation des sources

```
backend/app/        application FastAPI (main.py : démarrage, middleware), modèles, sauvegarde, HTTPS, On mange quoi?
  routes/           l'API par domaine : recettes, importation, courriel, paramètres, sauvegarde; common.py partagé
  ingestion/        lecture des liens, PDF (pdf_read), OCR (ocr), découpage en titre, ingrédients et étapes (segment) et courriels; étiquetage
  ml/               Paramètres -> Apprentissage (numpy seulement)
  templates/        le gabarit d'impression/PDF/HTML
backend/tests/      tests du serveur; corpus/ : cas du corpus d'importation
backend/tests_browser/  tests des pages (Playwright)
frontend/           l'application : index.html, js/app.js (un module JavaScript, sans compilation),
                    js/settings.js (les panneaux des Paramètres), js/add_recipe.js (la fenêtre Ajouter),
                    js/i18n.js, css/;
                    js/amounts.js + js/weights.js : ajustement, Poids, Volumes (aussi pour On mange quoi?)
frontend-picker/    la page On mange quoi? (port 8091)
docker-compose*.yml image publiée, construction depuis les sources, HTTPS par Traefik
```

## Le journal pendant le débogage

Chaque entrée porte l'étiquette de son domaine : `otp.scan`, `otp.email`,
`otp.url`, `otp.ocr` et `otp.client` (les erreurs des pages), p. ex.
`docker logs open-the-pantry 2>&1 | grep otp.scan`. `RECIPE_APP_LOG_LEVEL`
vaut `DEBUG` pour plus de détails, `WARNING` pour moins. Le fichier du
journal tourne à 1 Mo, et 3 anciens fichiers sont gardés.
