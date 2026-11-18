# Open the Pantry

*[English](README.md) · Français*

Un gestionnaire de recettes auto-hébergé. On ajoute une recette à partir
d'un lien, d'un PDF, d'une photo ou d'une capture d'écran, d'un courriel,
ou en la tapant; on les parcourt par étiquette, on les cherche, on les note
et on les partage. Anglais ou français (Québec). Une seule image Docker,
sans service d'IA.

> ## ⚠️ À utiliser sur votre propre réseau seulement
>
> **Il n'y a pas de connexion.** Quiconque peut joindre l'application peut
> lire, modifier et supprimer toutes les recettes. Seules les parties qui
> utilisent votre compte de courriel ou votre accès DNS, et la restauration
> ou l'horaire des sauvegardes, demandent le
> [mot de passe des paramètres](#mot-de-passe-des-paramètres).
>
> **Ne l'exposez pas à Internet.** Utilisez-la sur votre réseau local, ou de
> l'extérieur par un RPV (WireGuard, Tailscale…) ou un mandataire inverse qui
> fait sa propre authentification. Une faille grave d'accès aux fichiers a
> été trouvée et corrigée tard dans le développement, après plusieurs revues
> qui jugeaient le code sain. D'autres peuvent exister. La frontière du
> réseau est la vraie protection. [SECURITY.fr.md](SECURITY.fr.md) détaille
> ce contre quoi l'application se protège.

> **Non maintenue.** Construite avec des outils de programmation par IA pour
> un usage personnel, et publiée telle quelle : aucun soutien, aucun
> engagement à corriger quoi que ce soit. Les copies (forks) sont les
> bienvenues.

## Ce qui la distingue

Par rapport à Mealie et Tandoor, d'après leur documentation en novembre
2026 :

- **Les photos, captures d'écran et PDF sont lus sans modèle d'IA** : OCR
  et règles sur votre serveur, sans clé d'API ni carte graphique. Mealie et
  Tandoor lisent les photos par un modèle d'IA que vous branchez. Un modèle
  peut mieux lire une photo difficile; le
  [corpus d'importation](backend/tests/README.md#import-corpus) mesure ce
  que fait cette application sur vos propres recettes.
- **Elle apprend de vos corrections** : la mise en page de vos importations
  et votre façon d'étiqueter, utilisées seulement là où c'est mieux que les
  règles intégrées.
- **Anglais et français du Québec**, dans l'interface et dans ce qu'elle
  lit.
- **Recettes par courriel**, envoyées à une boîte que l'application
  vérifie.
- **On mange quoi?**, une page en lecture seule qui choisit une recette au
  hasard.
- **Vos propres mesures** : goût, rythme, difficulté et vrai temps de
  cuisson, tous utilisables comme filtres.
- **Un seul conteneur**, SQLite, aucun compte.

**Pas pour vous si** vous voulez planifier des repas, faire des listes
d'épicerie, calculer la valeur nutritive ou avoir plusieurs utilisateurs
avec leur propre compte. Mealie et Tandoor offrent tout cela.

## La lancer

Il faut Docker avec Compose 2.24 ou plus récent.

```bash
curl -O https://raw.githubusercontent.com/djerodek/open-the-pantry/main/docker-compose.yml
docker compose up -d
```

Ouvrez `http://<serveur>:8090`, puis **créez tout de suite le mot de passe
des paramètres** (Paramètres → Réception par courriel, HTTPS ou Journal le
demande) : tant qu'il n'existe pas, la première personne de votre réseau à
ouvrir une de ces sections le choisit.

- **Mise à jour** : `docker compose pull && docker compose up -d`; la base
  de données est mise à jour au démarrage. `:main` est reconstruite chaque
  dimanche : une tâche cron hebdomadaire qui lance cette commande la garde
  à jour. Pour choisir quand mettre à jour, épinglez une empreinte :
  `image: djerodek/open-the-pantry@sha256:<empreinte>`.
- **Construire depuis les sources** : `docker compose -f docker-compose.build.yml up -d --build`.
- **Images** pour `linux/amd64` et `linux/arm64`. L'application tourne sous
  l'UID 1000 (ou `PUID`/`PGID`). Les ports sont publiés sur toutes les
  adresses de la machine, et les ports publiés par Docker contournent ufw :
  sur une machine ayant une adresse publique, protégez-les par un pare-feu.
- **Mémoire** : environ 160 Mo au repos, jusqu'à environ 3,4 Go avec trois
  grosses numérisations à la fois (lignes `mem_limit`/`cpus` en commentaire
  dans le fichier compose).
- **Fuseau horaire** : `TZ` (`UTC` par défaut) fixe l'heure de la
  vérification du courriel et des sauvegardes automatiques.
- **Utilisez un disque local**, pas NFS : le journal de SQLite n'y est pas
  fiable.

### Vos données

Tout est dans un seul dossier, `./data` par défaut (la partie gauche de la
ligne `volumes:`) : la base `recipes.db`, les photos et PDF dans
`uploads/`, `encryption.key`, `admin-password.json` (le mot de passe des
paramètres), `https/`, `logs/`, `ml/` (modèles appris, reconstruits
automatiquement) et `backups/`.

**Pour le déplacer :** `docker compose down`, `cp -a ./data /nouveau/chemin`,
changez la partie gauche de la ligne du volume pour `/nouveau/chemin`,
`docker compose up -d`. Vérifiez vos recettes avant de supprimer l'ancien
dossier.

**Pour restaurer une sauvegarde à la main** (quand l'application ne démarre
pas; sinon, passez par les Paramètres) :
1. `docker compose down`
2. `rm -f ./data/recipes.db-wal ./data/recipes.db-shm`
3. `unzip -o open-the-pantry-backup-*.zip -d ./data`
4. `docker compose up -d`

Une sauvegarde ne contient ni `encryption.key`, ni `admin-password.json`, ni
`https/` : copiez-les depuis l'ancien dossier, ou configurez à nouveau le
courriel et HTTPS.

## Ce qu'elle fait

### Ajouter des recettes

- **À partir d'un lien**, un seul ou jusqu'à 50 à la fois, avec la photo
  de la page. Plus de 100 sites ont leur propre lecteur; les autres sont lus
  d'après les données de recette de la page, puis son texte.
- **À partir d'un document** (PDF, Word, texte ou Markdown), un seul ou
  jusqu'à 20 à la fois. Les pages numérisées d'un PDF passent par l'OCR, et
  sa plus grande photo devient celle de la recette.
- **À partir de photos ou de captures d'écran**, jusqu'à 10 réunies en une
  recette. Les pages sont découpées, remises à l'endroit et redressées; les
  titres en couleur et les listes d'ingrédients sur deux colonnes sont lus.
  Pas l'écriture à la main : tapez ces recettes.
- **Tapée à la main.** Une ligne courte qui se termine par « : » commence
  une section (« Sauce: »).
- **Partagée depuis une autre application** : sur Android une fois
  l'application installée (il faut HTTPS); sur iPhone, un raccourci qui
  reçoit des URL depuis la feuille de partage, les encode et ouvre
  `http://<serveur>:8090/?share_url=<encodé>`.
- **Par courriel** : voir plus bas.

Une recette seule ouvre un écran de vérification à côté du texte lu; les
lots, courriels et importations sont enregistrés *À vérifier*. Les recettes
incomplètes (sans étapes, ou sans ingrédients) sont refusées avec une autre
façon de faire, et un lien ou un titre déjà dans votre bibliothèque est
signalé. Les recettes en français sont lues comme celles en anglais.

### Réception par courriel (facultatif)

Un courriel avec un mot-clé dans l'objet (`[RECIPE]` par défaut) envoyé à
une boîte que vous configurez est récupéré une fois par jour, ou avec
**Vérifier la boîte de courriel** dans le menu +. L'application essaie un
PDF joint, puis un fichier Word, texte ou Markdown, puis une photo, puis
les 3 premiers liens, puis le texte, et répond par `[SUCCESS]`,
`[FAILURE]` ou `[PARTIAL]`. Une vérification manquée pendant que le
conteneur était arrêté se fait plus tard le même jour; les résultats qui
n'ont pas pu être envoyés attendent la prochaine.

- Le courriel sans le mot-clé n'est jamais lu; « Accepter les courriels
  seulement de » limite les expéditeurs (une ligne From peut être falsifiée :
  cela filtre, cela ne prouve rien).
- **Utilisez une adresse réservée** avec un mot de passe d'application.
  L'application le garde chiffré; **Configurer le chiffrement** dans les
  Paramètres crée la clé.

### Trouver et utiliser les recettes

- **Étiquettes** en trois groupes intégrés (type de repas, mode de
  cuisson, ingrédient principal) plus les vôtres, suggérées d'après des
  mots-clés en anglais et en français.
- **L'apprentissage** (Paramètres → Apprentissage) entraîne deux petits
  modèles sur vos recettes corrigées, pour lire les importations et pour
  les étiquettes; les Paramètres montrent le score de chacun face aux
  règles.
- **La recherche** couvre tous les champs, pardonne les fautes de frappe,
  accepte `-mot` pour exclure un mot, et met la meilleure correspondance en
  premier; le ⓘ à côté explique ce qu'elle comprend. Les filtres se
  combinent, et un filtre touché deux fois exclut ses recettes. **De
  saison au Québec**, à activer dans les Paramètres, garde les recettes avec
  des fruits et légumes du Québec en haute saison ce mois-ci (calendrier de
  Mangez Québec; ni produits de serre, ni pommes de terre, ail, oignons ou
  fines herbes).
- **Sur une recette** : évaluations, portions, temps de cuisson noté, notes
  personnelles;
  ajustement ½ / 1× / 2×; conversions **Poids** et **Volumes** d'après une
  table d'environ 220 ingrédients, qui laissent une ligne telle qu'écrite
  quand aucune conversion n'est sûre; garder l'écran allumé (il faut
  HTTPS); cases à cocher facultatives.
- **Bibliothèque** : modifier, dupliquer, fusionner deux recettes,
  étiqueter ou supprimer plusieurs à la fois; **Récemment supprimées** les
  garde 30 jours; **Vérification de la bibliothèque** liste les recettes
  incomplètes et les doublons probables.
- **Partager** : imprimer, PDF, HTML, JSON, une image JPG, du texte, ou
  envoyer le PDF par courriel.

### On mange quoi? (facultatif)

Une petite page en lecture seule sur le port 8091 qui choisit une recette
au hasard parmi vos choix. Activez-la dans Paramètres → On mange quoi?.
**Elle n'a pas de connexion** : quiconque peut joindre le port peut lire
toutes les recettes qu'elle peut choisir, notes comprises. Si le 8091 est
pris, publiez un autre port (p. ex. `"9000:8091"`) et réglez
`RECIPE_APP_PICKER_PUBLIC_PORT: "9000"`; derrière un mandataire, réglez
`RECIPE_APP_PICKER_PUBLIC_URL` et `RECIPE_APP_PUBLIC_URL`.

### Sauvegarde et restauration

Paramètres → Sauvegarde et exportation :
- **Sauvegarde complète** (`.zip`) : la base, toutes les photos et tous les
  PDF, et des instructions de restauration. C'est celle qu'on peut
  restaurer.
- **Recettes en PDF** ou **en JSON** (schema.org, pour d'autres
  applications de recettes).
- **Sauvegardes automatiques** : chaque jour à l'heure choisie, les 7 plus
  récentes gardées, dans `data/backups/`. Pour une vraie deuxième copie,
  montez un dossier sur un autre disque et réglez `RECIPE_APP_BACKUP_DIR`
  (lignes dans le fichier compose).
- **Les sauvegardes laissent de côté** l'identifiant de courriel, les
  adresses de notification et les réglages HTTPS
  ([liste complète](SECURITY.fr.md)), puisque n'importe qui sur
  votre réseau peut en télécharger une. Elles ne sont pas chiffrées :
  gardez-les là où vous seul pouvez les lire.
- **Restaurer ou importer** : une sauvegarde complète remplace la
  bibliothèque (mot de passe des paramètres; une copie de sécurité est
  d'abord écrite; ce serveur garde ses propres réglages HTTPS, d'horaire et
  de courriel). Si une restauration échoue en cours de route, l'application
  reste fermée jusqu'à son redémarrage et la termine au démarrage. Du JSON
  schema.org, un zip de ceux-ci ou une exportation Paprika ajoute plutôt
  ses recettes.

### HTTPS (facultatif)

Paramètres → HTTPS donne à l'application une adresse `https://` avec un
certificat Let's Encrypt, pour votre réseau local et votre RPV seulement,
sur un domaine dont le DNS est géré dans cPanel. Garder l'écran allumé et
l'installation sur Android en ont besoin. Les autres hébergeurs DNS peuvent
passer par Traefik. Configuration : [docs/https.fr.md](docs/https.fr.md).

### Interface

Anglais ou français par appareil; installable (PWA); thème clair ou
sombre; taille du texte; la liste des recettes en titres, en fiches avec
photos ou en mosaïque; zoom par pincement désactivé sur les téléphones,
exprès (il poussait les boutons hors de l'écran); prise en charge du
clavier et des lecteurs d'écran, vérifiée par un balayage automatique.

## Mot de passe des paramètres

Il protège la réception par courriel, HTTPS, le journal, l'envoi du PDF par
courriel, la restauration d'une sauvegarde et l'horaire des sauvegardes.
- Créé la première fois qu'une de ces sections est ouverte (8 caractères
  ou plus).
- Il déverrouille un navigateur pour 15 minutes. Redémarrer le conteneur
  verrouille tous les navigateurs et efface l'attente après des essais
  ratés.
- Une fois HTTPS activé, il ne fonctionne qu'à l'adresse `https://`.
- **Oublié?** Supprimez `admin-password.json` et créez-en un nouveau. Cela
  efface le mot de passe de courriel et le jeton cPanel enregistrés et
  désactive HTTPS; tout le reste est gardé.

## Limites connues

- **Certains sites refusent les requêtes automatisées** (vérification
  anti-robot de Cloudflare, accès payant) : enregistrez la page en PDF,
  faites des captures d'écran ou collez le texte.
- **Les longues captures enregistrées en PDF sur iOS sont coupées à
  200 pouces** : utilisez Imprimer → PDF.
- **Les importations suivent des règles** tant que l'apprentissage n'a pas
  vos corrections : vérifiez-les à l'écran de vérification ou sous *À
  vérifier*.
- **La photo de recette tirée d'un PDF** est sa plus grande image;
  remplacez-la au besoin.
- **La réception par courriel** demande un certificat reconnu par le
  système, et elle est lente sur une boîte qui a des centaines de messages
  non lus.

## Journal

Paramètres → Journal, `docker logs open-the-pantry`, ou
`data/logs/app.log`. Les mots de passe, clés, corps de courriels et contenus
de pages n'y sont jamais. `RECIPE_APP_LOG_LEVEL` règle le niveau de détail.

## Développement

Voir [CONTRIBUTING.fr.md](CONTRIBUTING.fr.md) : tests, intégration continue
et organisation des sources.
