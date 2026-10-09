# Open the Pantry

*[English](README.md) · Français*

Gestionnaire de recettes auto-hébergé, avec recherche. Ajoutez des recettes
à partir d'une URL, d'un PDF, d'une capture d'écran ou d'une photo, ou en
les saisissant à la main; parcourez-les par type de repas, mode de cuisson
ou ingrédient principal; filtrez et cherchez; notez et mettez en favori;
imprimez ou exportez une fiche propre, sans publicité. Interface en anglais
ou en français (Québec), choisie sur chaque appareil. Une seule image
Docker, et aucun modèle de langage (LLM) requis pour aucune méthode d'ajout.

> Cette traduction suit la version anglaise ([README.md](README.md)). En
> cas d'écart, c'est la version anglaise qui fait foi.

---

> ## ⚠️ À utiliser seulement sur votre propre réseau
>
> **Cette application n'a aucune authentification par défaut.** Quiconque
> peut joindre son adresse peut lire, modifier et supprimer toutes les
> recettes, et utiliser son API. Il n'y a pas d'écran de connexion, pas de
> comptes d'utilisateur et pas de gestion des permissions. Seule exception :
> les parties qui utilisent votre compte de courriel ou votre accès DNS
> (réception par courriel, HTTPS, journal et envoi du PDF par courriel)
> demandent un [mot de passe des paramètres](#notes-de-sécurité) créé dans
> l'application.
>
> **Ne l'exposez pas directement à Internet.** Utilisez-la sur votre réseau
> local, ou joignez-la à distance par un RPV (WireGuard, Tailscale, OpenVPN)
> ou derrière un proxy inverse qui gère lui-même l'authentification. Si elle
> doit être joignable au-delà d'un réseau de confiance, définir
> `RECIPE_APP_API_KEY` est un minimum, pas une solution complète — voir
> [Notes de sécurité](#notes-de-sécurité).
>
> Ce n'est pas une préoccupation théorique. Une vulnérabilité permettant à
> n'importe qui, sans authentification, de lire des fichiers arbitraires du
> conteneur *et de supprimer la base de données de l'application* a été
> trouvée et corrigée tard dans le développement — après que plusieurs
> révisions eurent déclaré le code sans problème. Elle est corrigée et
> couverte par des tests de régression, mais c'est une bonne indication que
> d'autres peuvent exister sans avoir encore été trouvées. **La frontière du
> réseau est la vraie protection.** Traitez-la comme telle.
>
> Si vous activez la réception par courriel (facultative), l'application
> conserve aussi des identifiants de courriel (chiffrés). À partir de là, le
> contrôle d'accès n'est plus facultatif en pratique — utilisez un compte de
> courriel dédié avec un mot de passe propre à l'application, jamais votre
> compte principal.

> ## 📋 État du projet : non maintenu
>
> Construit avec des outils de programmation par IA, pour un usage personnel
> à la maison. Publié au cas où il serait utile à quelqu'un d'autre.
>
> Fourni tel quel, sans soutien, sans suivi des signalements et sans
> engagement à corriger des bogues ou à publier des mises à jour de
> sécurité. Le développement continue seulement tant qu'il répond à mes
> propres besoins. Les copies (forks) sont bienvenues et encouragées — si
> vous avez besoin de changements, c'est la voie à suivre.

---

## Démarrer

Nécessite Docker et Docker Compose.

```bash
curl -O https://raw.githubusercontent.com/djerodek/open-the-pantry/main/docker-compose.yml
docker compose up -d
```

Ouvrez `http://localhost:8090`. Les données (base SQLite, images et PDF
téléversés) sont conservées dans `./data`, à côté du fichier compose.

**Créez ensuite le mot de passe des paramètres tout de suite :** Paramètres
→ Réception par courriel (ou HTTPS, ou Journal) le demande la première fois.
D'ici là, la première personne de votre réseau à ouvrir l'une de ces
sections le crée. Voir [Notes de sécurité](#notes-de-sécurité).

### Déplacer ou restaurer vos données

Tout ce que l'application conserve se trouve dans un seul dossier : ce qui
est à gauche de la ligne `volumes:` dans `docker-compose.yml` (`./data` par
défaut). Il contient `recipes.db`, `uploads/` (photos et PDF),
`encryption.key` si vous avez configuré la réception par courriel dans les
Paramètres, et `admin-password.json` (le mot de passe des paramètres) une
fois celui-ci créé. Déplacer l'application vers un autre disque, c'est déplacer ce
dossier et y faire pointer le fichier compose.

**Déplacer vers un autre disque ou chemin** (p. ex. de `./data` vers un
volume du NAS) :

1. Arrêtez l'application : `docker compose down`. Ne copiez pas pendant
   qu'elle fonctionne — SQLite peut garder des écritures récentes dans
   `recipes.db-wal`, et une copie faite au milieu d'une écriture peut être
   incohérente.
2. Copiez le dossier au complet, en gardant tout ce qu'il contient :
   `cp -a ./data /path/on/nas/open-the-pantry-data`
3. Modifiez la ligne de volume dans `docker-compose.yml`. Changez seulement
   la partie de gauche; `/app/data` à droite est le chemin dans le
   conteneur et doit rester tel quel :

   ```yaml
   volumes:
     - /path/on/nas/open-the-pantry-data:/app/data
   ```

4. Démarrez-la : `docker compose up -d`. Le conteneur corrige les
   propriétaires des fichiers au démarrage; aucun `chown` n'est nécessaire.
5. Vérifiez que vos recettes sont là avant de supprimer l'ancien dossier.

**Restaurer à partir d'une sauvegarde .zip** (Paramètres → Sauvegarde et
exportation → Sauvegarde complète) :

1. `docker compose down`
2. Décompressez dans le dossier visé par la ligne de volume, de façon que
   `recipes.db` et `uploads/` soient directement dedans (pas dans un
   sous-dossier) :
   `unzip -o open-the-pantry-backup-*.zip -d /path/to/data`
3. `docker compose up -d`

La restauration remplace la bibliothèque actuelle. La sauvegarde .zip
n'inclut volontairement **pas** `encryption.key`, pour qu'une sauvegarde
qui fuit n'expose aucun mot de passe de courriel. Après une restauration
sur une nouvelle installation, copiez `encryption.key` de l'ancien dossier
de données, ou configurez de nouveau le chiffrement dans les Paramètres et
entrez de nouveau le mot de passe du courriel. Rien d'autre ne dépend de la
clé. Le mot de passe des paramètres (`admin-password.json`) n'est pas dans
le .zip non plus : copiez-le aussi, ou créez-en un nouveau la première fois
que vous ouvrez la réception par courriel, HTTPS ou le journal. Si l'ancien
dossier avait un mot de passe et que son `admin-password.created` est copié
sans `admin-password.json`, créer le nouveau compte comme une
réinitialisation et efface le mot de passe de courriel et HTTPS.

Pour construire à partir du code source plutôt que de télécharger l'image
publiée :

```bash
docker compose -f docker-compose.build.yml up -d --build
```

### Facultatif : HTTPS sur votre réseau

Désactivé par défaut. Configuré dans **Paramètres → HTTPS**, il donne à
l'application une adresse `https://` avec un vrai certificat Let's
Encrypt, sur votre réseau local et par votre RPV, sans rien exposer à
Internet. Les navigateurs refusent certaines fonctions aux pages en
`http://`; pour cette application, « garder l'écran allumé » en a besoin.

Il fonctionne avec un domaine dont le DNS est géré dans **cPanel** (la
plupart des hébergeurs Web mutualisés). Votre site Web reste où il est :
l'application ajoute un enregistrement pour un nouveau nom, p. ex.
`pantry.example.com`, qui pointe vers l'adresse locale de ce serveur, et
prouve à Let's Encrypt que le nom vous appartient avec un enregistrement
temporaire qu'elle retire ensuite. Elle renouvelle le certificat d'elle-même.

Ce qui doit se faire hors de l'application, une seule fois :

1. **Créez un jeton d'API cPanel :** dans cPanel, Security → Manage API
   Tokens → Create.
2. **Mettez l'identifiant et le nom dans `.env`**, à côté de
   `docker-compose.yml` :

   ```
   CPANEL_USERNAME=your-cpanel-username
   CPANEL_TOKEN=paste-the-api-token
   CPANEL_BASE_URL=https://your-cpanel-address:2083
   PANTRY_DOMAIN=pantry.example.com
   ```

   `chmod 600 .env` — le jeton peut faire tout ce que votre connexion
   cPanel peut faire. Ni l'un ni l'autre ne se saisit dans les Paramètres
   de l'application : l'application n'a pas de connexion à elle, et
   quiconque peut ouvrir les Paramètres pourrait sinon les changer.
   `PANTRY_DOMAIN` est le seul nom que l'application créera ou modifiera
   avec le jeton.
3. **Redémarrez :** `docker compose up -d`. Le fichier compose lit déjà
   `.env` s'il existe et publie le port 8443 (si votre fichier compose est
   plus ancien, copiez les lignes `ports:` et `env_file:` de la version
   actuelle).

Ensuite, dans Paramètres → HTTPS, qui affiche le nom tiré de `.env` :
entrez l'adresse locale de ce serveur (déjà remplie si vous l'utilisez) et
un courriel pour Let's Encrypt, puis appuyez sur **Configurer HTTPS**. Ça prend une minute ou deux et chaque étape est
affichée. À la fin, le lien s'affiche : `https://pantry.example.com:8443`.
Sur un iPhone, ouvrez-le et ajoutez-le de nouveau à l'écran d'accueil : les
réglages comme la langue et le thème sont conservés par adresse.

Garde-fous et remarques :
- L'application ne crée jamais que l'enregistrement de `PANTRY_DOMAIN`, et
  ne modifie jamais un enregistrement qu'elle n'a pas créé. Un nom déjà
  utilisé (`www`, le domaine lui-même, tout ce qui a un enregistrement) est
  refusé, pour que la page ne puisse pas servir à rediriger votre site Web.
- Le nom ne peut pointer que vers une adresse de votre réseau domestique ou
  de votre RPV (`10.x`, `172.16`–`172.31`, `192.168.x`, ou `100.64`–`100.127`
  de Tailscale), jamais vers un serveur sur Internet.
- Au plus 5 demandes de certificat en 7 jours, la limite de Let's Encrypt
  pour un même nom; au-delà, la page indique quand elle pourra réessayer.
- Changer `PANTRY_DOMAIN` puis refaire la configuration retire
  l'enregistrement de l'ancien nom, s'il contient encore ce que
  l'application y a écrit. **Mise à jour à partir d'une version sans
  `PANTRY_DOMAIN` :** ajoutez-le à `.env` (le nom que vous avez configuré)
  et redémarrez; d'ici là, le renouvellement attend et Paramètres → HTTPS
  indique ce qui manque.
- L'ancienne adresse `http://<adresse>:8090` continue de fonctionner.
- Pour `https://pantry.example.com` sans port, changez la partie de gauche
  de `8443:8443` dans `docker-compose.yml` pour `443` (si la machine
  n'utilise pas déjà le 443 — l'interface Web d'un NAS le fait souvent) et
  mettez le port 443 dans Paramètres → HTTPS.
- Certains routeurs bloquent les réponses DNS qui pointent vers des
  adresses privées (« protection contre le DNS rebinding »). Si le nom ne
  se résout pas à la maison, autorisez votre domaine dans le routeur.
- Le nom apparaît dans les journaux publics de certificats, comme tout nom
  Let's Encrypt; l'adresse privée vers laquelle il pointe est inutile de
  l'extérieur.
- **Autres hébergeurs DNS** (Cloudflare, OVH, Gandi et environ 150 autres) :
  `docker-compose.https.yml` fait la même chose avec Traefik, configuré dans
  `.env` à partir de `https.env.example`; voir les commentaires dans ces
  fichiers. Cette méthode n'a pas de page dans les Paramètres.

## Ce que fait l'application

**Ajout de recettes**
- **URL** — essaie d'abord `recipe-scrapers` (plus de 100 analyseurs propres
  à des sites), puis le JSON-LD `schema.org`, puis une extraction
  heuristique du HTML. Une à la fois ou par lot (jusqu'à 50 URL d'un coup,
  enregistrées automatiquement sans révision une par une). Télécharge aussi
  l'image vedette de la page (`og:image` ou image JSON-LD) quand il y en a
  une, par la même chaîne de validation que toute autre image téléversée —
  une image brisée, trop grosse ou invalide ne fait jamais échouer l'ajout;
  elle est simplement ignorée.
- **Quand un lien ne donne pas une recette complète** (le site bloque
  l'application, la page n'a pas de recette lisible, ou elle revient sans
  ingrédients ou sans étapes), l'ajout compte comme un échec et
  l'application explique comment faire un PDF de la page à la place
  (Safari : Partager → Options → PDF), avec un bouton **Ajouter à partir
  d'un PDF** juste là. Une demi-recette peut quand même être ouverte avec
  « Continuer quand même ». Les ajouts par lot l'indiquent comme un échec,
  et un lien envoyé par courriel reçoit une réponse `[FAILURE]` qui suggère
  d'envoyer plutôt le PDF en pièce jointe. Le navigateur a la page au
  complet (au-delà de toute vérification anti-robots), donc son PDF se lit
  habituellement bien.
- **Quand un PDF ou une capture d'écran ne donne pas une recette
  complète** (des ingrédients mais pas d'étapes, ou l'inverse), l'ajout
  échoue de la même façon au lieu d'ouvrir l'écran de révision avec un
  champ vide. La cause habituelle : le PDF d'un article de blogue qui
  s'arrête avant la fiche recette, au bas de la page. L'application dit ce
  qui manque et propose les autres façons de l'ajouter : des captures
  d'écran de la fiche recette (ouvre « Combiner plusieurs »), un PDF (pour
  une capture d'écran) ou la saisie à la main, plus « Continuer quand
  même ». Un ajout de PDF par lot l'indique comme un échec.
- **PDF** — vérifie d'abord si chaque page a déjà une couche de texte;
  seules les pages qui n'en ont pas (images numérisées) passent par la
  reconnaissance de texte (OCR) de Tesseract, avec un passage d'orientation
  (les pages de côté ou à l'envers sont remises à l'endroit) et un
  redressement pour les pages légèrement inclinées. Les documents mixtes
  sont traités page par page. Un à la fois ou par lot (jusqu'à 20 fichiers
  et 100 Mo au total, enregistrés automatiquement). Extrait aussi la plus
  grande image incluse dans le document comme photo vedette, s'il y en a
  une assez grande pour être vraisemblablement une vraie photo plutôt
  qu'un logo ou une icône.
- **Capture d'écran / photo** — OCR avec prétraitement (niveaux de gris,
  orientation, redressement, agrandissement, seuillage). Une photo prise de
  côté ou à l'envers est détectée et remise à l'endroit avant l'OCR, et la
  photo enregistrée est tournée elle aussi pour s'afficher dans le bon
  sens; le redressement corrige ensuite une page photographiée légèrement
  de biais. Fonctionne bien sur du texte affiché à l'écran ou en capture;
  moins bien sur des polices stylisées ou du texte peu contrasté sur une
  image. Pas conçu pour l'écriture à la main — utilisez la saisie manuelle
  pour les fiches manuscrites. Le mode « Combiner plusieurs » gère les
  recettes qui tiennent sur plusieurs captures (jusqu'à 10) : chacune est
  lue séparément, dans l'ordre choisi, puis le texte est mis bout à bout et
  découpé comme une seule recette plutôt que comme des recettes séparées —
  contrairement au mode par lot des URL et des PDF, qui produit
  volontairement des recettes séparées. La première image devient la photo
  vedette par défaut (modifiable ensuite); un fichier invalide n'importe où
  dans l'ensemble annule toute la combinaison plutôt que de produire une
  recette partielle sans le dire.
- **Saisie manuelle** — champs de texte libre et photo facultative, pour
  les fiches manuscrites ou tout ce que l'OCR ne lit pas de façon fiable.
- **Toute recette enregistrée peut être modifiée ensuite** avec « Modifier
  la recette » sur sa page — titre, ingrédients, étapes, étiquettes,
  portions et temps. Le texte extrait d'origine (ce que l'extracteur ou
  l'OCR a réellement produit) est affiché en lecture seule à côté des
  champs modifiables, pour qu'une extraction partielle se corrige d'après
  la source plutôt que de mémoire. C'est surtout utile pour les méthodes
  qui enregistrent sans révision (URL par lot, PDF par lot, courriel) et
  pour un OCR peu fiable dont on ne remarque l'erreur que plus tard.
- L'ajout d'un seul élément (URL, PDF, capture) passe toujours par un écran
  de révision — le texte extrait à côté des champs modifiables, plus un
  aperçu de l'image vedette capturée, avec l'option « Ne pas utiliser
  cette image » — avant l'enregistrement. L'ajout par lot enregistre
  directement et indique le succès ou l'échec de chaque élément, puisque
  réviser plusieurs éléments un par un n'est pas vraiment un lot.
- **Les ingrédients sont conservés tels qu'écrits.** Chaque ligne est
  gardée exactement comme saisie ou extraite, et aussi décomposée en
  quantité, unité et nom (« 1½ cups flour » → 1½ / cup / flour), y compris
  les lignes tapées dans les écrans de révision, de modification et de
  saisie manuelle. La recette affiche la ligne telle qu'écrite.
- Les recettes qui regroupent leurs étapes en sections (« Pour la pâte »,
  « Pour la garniture ») gardent toutes les étapes, avec le nom des
  sections comme intertitres.
- **Comment le texte extrait devient une recette** (le texte des PDF, des
  photos et des courriels suit les mêmes règles) : le titre est la ligne
  au-dessus de la signature de l'auteur, ou une ligne courte que la page
  répète, jamais la date d'impression du navigateur ni un fil d'Ariane.
  Les lignes d'ingrédients coupées sont recollées, les sous-titres comme
  « PEPPERED BACON CURE » deviennent « For the peppered bacon cure: », et
  les boutons des fiches recettes (« 1X 2X 3X », « US Customary / Metric »)
  sont retirés. Les étapes sont reconstituées au complet à partir de leurs
  lignes imprimées, séparées aux numéros d'étape, et se terminent à
  « Notes » ou « Nutrition ». La mise en forme des courriels en texte brut
  (le `*gras*` de Gmail et les puces `-`) est comprise, et les étapes
  numérotées sont trouvées même sans intertitre « Instructions ».
- **Les recettes en français sont comprises aussi.** Les intertitres
  français (Ingrédients, Préparation, Étapes, Mode de préparation…), les
  numéros d'étape (« Étape 2 »), les unités (« 2 c. à soupe », « 250 ml »,
  « 1,5 tasse ») et les majuscules accentuées sont lus de la même façon que
  leurs équivalents anglais. L'OCR lit l'anglais et le français ensemble
  (l'image inclut les données françaises de Tesseract); les captures en
  anglais donnent le même résultat qu'avant, et l'OCR prend environ moitié
  plus de temps.
- Dans une recette sans aucun intertitre, une ligne sans quantité (« Salt
  and pepper to taste », « Sel et poivre au goût ») reste avec les
  ingrédients plutôt que de devenir l'étape 1.
- Les fichiers téléversés (PDF, image) sont limités à 20 Mo et validés par
  leur signature (magic bytes), pas seulement par leur extension, avant
  toute analyse ou OCR.

**Réception par courriel (facultative, désactivée par défaut)**
- Envoyez une recette par courriel à une boîte configurée avec un mot-clé
  dans l'objet (par défaut `[RECIPE]`), et Open the Pantry la récupère à sa
  prochaine vérification. Tout ce qui n'a pas ce mot-clé est entièrement
  ignoré — l'application n'essaie jamais d'analyser, ni même de lire au
  complet, un courriel sans le mot-clé.
- La vérification se fait une fois par jour à une heure configurable (par
  défaut 3 h, dans le fuseau horaire du conteneur — mettez le vôtre dans
  `TZ` dans `docker-compose.yml`, p. ex. `America/Toronto`; l'exemple est
  livré avec `UTC`; les heures affichées dans les Paramètres, comme la
  dernière vérification, sont dans le fuseau de votre appareil dans tous
  les cas), plus sur demande : « Vérifier la boîte de courriel » dans le
  menu + (affiché une fois la réception par courriel activée), ou
  « Vérifier la boîte maintenant » dans les Paramètres, pour ne pas
  attendre.
- Chaque courriel avec le mot-clé est essayé dans cet ordre : pièce jointe
  PDF → photo → liens dans le corps (jusqu'à trois, dans l'ordre, passés à
  la même chaîne d'ajout par URL) → le texte du corps lui-même. Chaque
  méthode réutilise le même code d'ajout et de validation que son
  équivalent manuel; un PDF reçu par courriel est donc traité exactement
  comme un PDF téléversé dans l'interface.
- Les photos et les PDF envoyés depuis Apple Mail comptent même si Apple
  les marque « en ligne ». Une image en ligne doit faire au moins 640 px
  sur son grand côté pour être traitée comme une photo de recette, ce qui
  garde les logos de signature hors de l'OCR. Les pieds de page des
  applications de courriel (« Sent from my iPhone », « Get Outlook for
  iOS » et son lien) et tout ce qui suit une ligne de signature `-- ` sont
  ignorés.
- Le courriel n'a pas d'écran de révision : une photo ou un PDF entièrement
  numérisé que la reconnaissance de texte ne peut pas lire de façon fiable
  (fiabilité sous 50 %) est refusé plutôt qu'enregistré comme une recette
  de mots déformés; le courriel `[FAILURE]` le dit et suggère une photo
  plus nette ou le texte tapé. Les noms des pièces jointes sont décodés,
  donc le `Screenshot … PM.pdf` d'Apple Mail apparaît tel quel. Une photo,
  un PDF, un lien ou un texte de courriel qui ne donne qu'une partie d'une
  recette (des ingrédients mais pas d'étapes, ou l'inverse) échoue plutôt
  que d'être enregistré à moitié, et le courriel `[FAILURE]` dit quelle
  partie manquait et quoi envoyer à la place.
- Si un courriel ne peut pas devenir une recette, la raison énumère chaque
  chose essayée et pourquoi elle a échoué. Elle est affichée sous
  « Vérifier la boîte maintenant » et envoyée dans le courriel `[FAILURE]`.
  Les courriels de plus de 30 Mo sont refusés avant le téléchargement.
- Le type de connexion SMTP/IMAP suit le port : 465 et 993 sont chiffrés
  dès le départ, les autres ports passent au chiffrement avec STARTTLS.
  « Envoyer un courriel de test » vérifie séparément l'envoi et la lecture,
  et quand une connexion échoue, il sonde le port et dit ce qu'il y a
  trouvé.
- Si la vérification de nuit ne peut pas lire la boîte, ou si son courriel
  de résultats ne peut pas être envoyé, Paramètres → Réception par courriel
  l'indique en haut, avec l'erreur et le nombre de résultats en attente
  d'envoi. (Une configuration d'envoi brisée ne peut pas vous envoyer un
  courriel pour dire qu'elle est brisée.) L'avertissement disparaît après
  la prochaine vérification ou le prochain test réussi. Les mêmes erreurs
  sont dans `data/logs/app.log`.
- Un lien dans un courriel ajoute la recette mais pas la photo de la page
  (« Ajouter à partir d'une URL » dans l'application, lui, la récupère).
  Ajoutez-en une à partir de la page de la recette.
- Les courriels de résultats sont rédigés dans la langue de la personne
  qui a enregistré les paramètres de courriel en dernier (anglais ou
  français). Les marqueurs `[SUCCESS]`, `[FAILURE]` et `[PARTIAL]` restent
  les mêmes dans les deux langues, pour que les filtres de courriel
  continuent de fonctionner.
- Les résultats sont envoyés par courriel : `[SUCCESS]`, `[FAILURE]`, ou
  `[PARTIAL]` quand une vérification a eu les deux. Les résultats à
  l'intérieur d'une période d'attente configurable (30 min par défaut)
  sont regroupés en un seul message plutôt qu'envoyés un par un. Si l'envoi
  échoue, les résultats restent en file d'attente et partent avec la
  prochaine vérification au lieu d'être perdus.
- Un bouton « Envoyer un courriel de test » fait l'aller-retour complet —
  envoie un message `[TEST]` par SMTP et confirme l'accès IMAP — et indique
  quelle étape précise a échoué si quelque chose est mal configuré.
- Les recettes reçues par courriel sont enregistrées automatiquement sans
  écran de révision (personne n'est au clavier à 3 h du matin) et portent
  une pastille de source `Email`, pour être faciles à trouver et à vérifier
  ensuite.
- **Utilisez une adresse de courriel dédiée qui ne sert à rien d'autre.**
  C'est la configuration recommandée, pas seulement une précaution : créez
  un compte ou un alias utilisé uniquement pour envoyer des recettes à
  cette application, avec un mot de passe propre à l'application.
  L'application a besoin des identifiants de la boîte que vous lui donnez;
  lui donner un compte qui contient votre vraie correspondance, c'est
  conserver des identifiants donnant accès à tout ça. Une adresse dédiée
  limite les dégâts possibles à une boîte qui ne contient que des recettes.
- Le mot de passe du courriel est conservé chiffré; une clé doit donc
  d'abord exister. Paramètres → Réception par courriel a un bouton
  **Configurer le chiffrement** qui en crée une (`encryption.key` dans le
  dossier de données); aucune modification du fichier compose n'est
  nécessaire. Définir `RECIPE_APP_ENCRYPTION_KEY` vous-même fonctionne
  encore et a priorité — voir les Notes de sécurité pour la différence.

**Image vedette**
- Chaque recette peut avoir une photo représentative : affichée sous le
  titre sur la page de la recette, et en vignette sur chaque fiche de la
  liste ou de la grille.
- Capturée automatiquement lors de l'ajout par URL ou par PDF (voir
  ci-dessus), ou téléversée, remplacée ou retirée à la main en tout temps à
  partir de la page de la recette — que la recette ait eu une image au
  départ ou non.
- Paramètres → Liste des recettes → « Afficher les photos sur les fiches
  de recettes » active ou désactive les vignettes pour toute la liste,
  mémorisé sur chaque appareil.
- Jamais incluse dans le PDF partagé (voir Partager / imprimer plus bas);
  le fichier HTML exporté et la vue d'impression de l'application
  l'incluent.

**Sauvegarde et exportation** (Paramètres → Sauvegarde et exportation)
- Choisissez un format, puis Télécharger. Le fichier est récupéré en
  arrière-plan et enregistré à partir de la page, pour qu'une application
  installée sur iPhone ne reste pas bloquée sur l'aperçu d'un .zip sans
  moyen de revenir. Une création qui échoue dit pourquoi au lieu de ne rien
  faire en silence.
- **Sauvegarde complète (`.zip`)** — celle qui se restaure. Contient un
  instantané cohérent de la base SQLite plus chaque photo et PDF
  téléversé, avec un `manifest.json` et un `RESTORE.txt` (ou
  `RESTAURER.txt` en français) qui explique comment la remettre en place.
  Pour restaurer : arrêter l'application, décompresser dans le dossier de
  données, la redémarrer (voir *Déplacer ou restaurer vos données*
  ci-dessus; les mêmes étapes sont dans l'application, sous le bouton
  Télécharger). L'application est arrêtée pour ça volontairement —
  remplacer un fichier SQLite sous un processus en marche, c'est la
  meilleure façon de le corrompre.
- L'instantané est pris avec l'**API de sauvegarde en ligne** de SQLite,
  pas en copiant `recipes.db`. C'est plus important qu'il n'y paraît :
  l'application fonctionne en mode WAL, donc à tout moment une quantité
  inconnue de données validées se trouve dans `recipes.db-wal` plutôt que
  dans le fichier principal. Une simple copie de fichier produit, sans
  rien dire, une sauvegarde qui manque vos recettes les plus récentes — de
  façon mesurable; un test de régression vérifie que l'instantané contient
  les lignes validées dans le WAL. La sauvegarde est aussi sortie du mode
  WAL avant la compression, pour que l'archive contienne un seul fichier
  autonome.
- Les photos dans l'archive sont exactement celles auxquelles l'instantané
  de la base fait référence, et la suppression d'une recette attend
  qu'elles aient été copiées, donc une sauvegarde ne pointe jamais vers une
  photo qu'elle ne contient pas. Les photos auxquelles rien ne fait
  référence sont laissées de côté; une photo référencée qui manque sur le
  disque (y compris une supprimée hors de l'application pendant la
  sauvegarde) est inscrite dans `manifest.json` et dans le journal plutôt
  qu'ignorée en silence.
- **Recettes en PDF (`.zip`)** — un PDF par recette, plus un `index.csv`.
  C'est l'archive qui survit à l'application : les PDF s'ouvrent partout,
  sans Docker et sans SQLite. Elle **ne peut pas** servir à restaurer —
  c'est une copie à lire, pas une sauvegarde. Les notes sont incluses par
  défaut (c'est votre propre archive, contrairement à l'exportation
  partagée) et une case permet de les exclure. Une recette dont le rendu
  échoue est inscrite dans l'index et sautée plutôt que de faire échouer
  toute l'archive.
- Les deux sont créées dans un fichier temporaire et transmises en continu
  par le serveur, jamais assemblées dans sa mémoire — une bibliothèque de
  quelques centaines de photos est trop grosse pour qu'il soit raisonnable
  de la garder en mémoire vive sur un NAS. (Le navigateur, lui, garde le
  fichier terminé en mémoire avant de l'enregistrer; c'est le prix pour ne
  pas quitter la page sur iOS, et ça va jusqu'à quelques centaines de Mo.)
  Les archives temporaires sont supprimées une fois envoyées, et nettoyées
  si un téléchargement meurt en cours de route.
- Il n'y a volontairement **pas de bouton de restauration** dans
  l'application. L'application n'a pas d'authentification, et un point
  d'accès non authentifié qui écrase toute la base de données est bien
  plus dangereux à exposer qu'un point d'accès qui la lit. La restauration
  est plutôt une étape manuelle documentée, en deux commandes.

**Organiser et trouver**
- Trois catégories d'étiquettes structurées (type de repas, mode de
  cuisson, ingrédient principal) plus des étiquettes libres. Suggestion
  automatique par mots-clés à l'ajout; toujours modifiable. Les étiquettes
  intégrées connaissent les mots français comme anglais (« au four »
  suggère Four, « poulet » Poulet, « souper » Souper), avec ou sans
  accents. Les mots de repas suivent l'usage québécois : déjeuner, c'est le
  matin, dîner le midi, souper le soir. Les bouillons, fonds et sauces
  (« bouillon de poulet », « sauce de poisson ») ne comptent pas comme
  ingrédient principal, mais excluent quand même Végétarien. Les
  suggestions s'appliquent à l'ajout d'une recette; les étiquettes des
  recettes existantes ne changent pas, sauf si vous utilisez Paramètres →
  Étiquettes → « Ajouter les étiquettes suggérées à toutes les recettes ».
  Cela liste les suggestions manquantes de chaque recette, avec une case
  par recette et par étiquette (toutes cochées au départ); seul ce qui
  reste coché est ajouté. Cela n'ajoute jamais une étiquette qu'une recette
  a déjà, ni Végétarien à côté d'une étiquette de viande ou de poisson, et
  ne retire rien. Les suggestions décochées ou retirées sont proposées de
  nouveau la fois suivante, sauf si la recette est réglée à « Exclure à
  l'avenir » (un bouton sur chaque ligne de la liste). L'écran de
  modification d'une recette exclue permet de l'inclure de nouveau, et son
  bouton « Suggérer des étiquettes » ajoute en tout temps les suggestions
  pour cette recette dans le champ des étiquettes — exclue ou non — pour
  que vous fassiez le tri avant d'enregistrer.
- Paramètres → Étiquettes → « Gérer les groupes d'étiquettes et les
  mots-clés » : ajoutez des groupes (p. ex. Cuisine du monde) et des
  étiquettes dans n'importe quel groupe, chacune avec les mots qui la font
  suggérer (« Thaï : citronnelle, galanga, sauce de poisson »). Les
  étiquettes intégrées peuvent aussi recevoir des mots de plus (Bœuf :
  bavette). Une étiquette d'ingrédient principal peut être marquée comme
  voulant dire que le plat n'est pas végétarien. Les nouveaux groupes ont
  leur propre bouton à côté de Type de repas / Mode de cuisson /
  Ingrédient principal dès qu'ils ont des étiquettes. Les groupes et
  étiquettes ajoutés là peuvent être supprimés, ce qui les retire des
  recettes; les groupes et étiquettes intégrés ne peuvent pas l'être. Une
  étiquette laissée dans un groupe qui n'existe plus (une base de données
  modifiée hors de l'application, par exemple) est déplacée dans
  Personnalisées au démarrage. Les modes de cuisson propres aux cocktails
  (Agité, Remué, Monté au verre, Mélangé) s'affichent dans un
  sous-onglet de Mode de cuisson.
- Recherche plein texte (SQLite FTS5) dans les titres, ingrédients, étapes,
  notes, texte source *et* noms d'étiquettes, chaque résultat indiquant
  s'il correspond à la recette ou à une étiquette. Les mots correspondent à
  partir de leur début (« pou » trouve poulet), dans n'importe quel ordre,
  et chaque mot doit correspondre. Le texte des ingrédients et des étapes
  est tenu à jour par des déclencheurs de la base de données, donc les
  modifications sont cherchables tout de suite.
- Tri par plus récentes ou plus anciennes, titre, note, temps de cuisson ou
  difficulté, dans un sens ou dans l'autre; les recettes sans valeur pour
  le champ choisi vont toujours à la fin. Le choix est mémorisé. Le tri
  s'applique aussi aux résultats de recherche.
- « Effacer les filtres » apparaît (avec un compte) chaque fois que des
  étiquettes, un rythme, une plage de temps de cuisson ou une recherche
  sont actifs : dans la barre d'outils, et en haut du panneau latéral, à
  l'écart des étiquettes elles-mêmes.
- Une petite icône de notes sur une fiche (vue photos ou liste) indique les
  recettes qui ont des notes.
- Les fiches sans photo n'affichent que le titre, sans espace vide. Les
  pastilles de source et de qualité de l'OCR sont sur la page de la
  recette, pas sur les fiches. Une recette lue à partir d'une adresse web
  (saisie, ou lien dans un courriel de recette) a aussi un lien **Voir
  l'original** à côté de la pastille de source. Il ouvre la page dans un
  nouvel onglet ou une nouvelle fenêtre. Dans une application installée,
  c'est le téléphone qui décide si c'est votre navigateur par défaut ou une
  visionneuse intégrée à l'application; une page web ne peut pas choisir.
  Une impression ou une exportation affiche l'adresse elle-même. Pour un
  lien reçu par courriel, l'adresse gardée est celle de la page où menait le
  lien, pas le lien de suivi de l'infolettre.
  Les recettes enregistrées avant cette version à partir d'un lien reçu par
  courriel n'ont pas d'adresse enregistrée, donc pas de lien.
- Le bouton ☰ ouvre « Parcourir par étiquette » : les groupes d'étiquettes,
  où toucher une étiquette filtre la liste. Sur un téléphone, c'est un
  tiroir qui se ferme quand on touche à côté.
- Filtres d'étiquettes cumulatifs (logique ET — ne garde que les recettes
  qui ont toutes les étiquettes choisies) plus un filtre imbriqué de temps
  de cuisson (tranches d'heures qui se précisent jusqu'à des intervalles de
  20 minutes), dont les options sont générées seulement à partir des temps
  de cuisson réellement inscrits — aucune option vide.
- Filtre de rythme (Rapide / Modéré / Long, d'après la note de rythme de
  chaque recette). En choisir plus d'un montre les recettes qui ont l'un ou
  l'autre; il se combine avec les étiquettes, le temps de cuisson et la
  recherche.
- Les filtres s'appliquent dès qu'on les touche. Terminé (toujours au bas
  du panneau), le × ou Échap ferme le panneau et les garde.

**Notes et suivi**
- Bascule de favori, plus trois notes indépendantes (goût de 1 à 5, rythme
  rapide/modéré/long, difficulté facile/moyen/difficile) réglables
  directement à partir de chaque fiche, sans ouvrir la recette. Sur une
  fiche, elles ouvrent une feuille (une fiche coupe ce qui dépasse, donc
  une fenêtre contextuelle serait tronquée et cacherait le titre); la page
  de la recette utilise une fenêtre contextuelle, puisqu'il y a la place.
  La feuille affiche aussi la valeur actuelle et permet de l'effacer.
- Temps de cuisson réel facultatif (`dd:hh:mm`), inscrit séparément de tout
  temps de préparation, de cuisson ou total indiqué par la source — c'est
  lui qui alimente le filtre de temps. Jusqu'à 60 jours (salaisons et
  fermentations longues); heures sous 24 et minutes sous 60. Le filtre
  propose des intervalles de 20 minutes jusqu'à une journée, puis des
  journées entières.
- Notes libres pour chaque recette (substitutions, ajustements de temps,
  résultat) avec un bouton sur la page de la recette — visuellement
  différent quand des notes existent. Gérées uniquement par `PATCH /notes`;
  volontairement absentes du corps de `PUT /api/recipes/{id}` (qui remplace
  tout), pour qu'un client qui les omet ne puisse pas les effacer sans le
  savoir.

**Gestion en lot**
- Mode Sélectionner (le bouton à droite de la barre d'outils) pour
  supprimer plusieurs recettes d'un coup, en plus de la suppression d'une
  seule recette (« Supprimer la recette », à côté de « Modifier la
  recette », au bas d'une recette).
- La suppression demande d'abord une confirmation. Si elle échoue,
  l'application dit pourquoi (erreur du serveur, serveur injoignable, déjà
  supprimée) et abandonne après 10 secondes plutôt que de laisser la boîte
  de dialogue bloquée. Une suppression en lot indique combien ont été
  supprimées, introuvables ou en échec, et garde les échecs sélectionnés
  pour réessayer.
- La suppression est totale : la ligne de la base et tout fichier sur le
  disque (image, PDF) sont retirés. Les fichiers d'une session d'ajout
  abandonnée (téléversés mais jamais enregistrés) sont supprimés à la
  fermeture de la boîte de dialogue, avec un nettoyage au démarrage en
  filet de sécurité.

**Partager / imprimer**
- Un seul gabarit HTML sert à la vue d'impression de l'application, à un
  PDF téléchargeable (WeasyPrint) et à un fichier HTML autonome (images
  incluses en base64) pour partager hors de votre réseau.
- Si une recette a des notes, le téléchargement du PDF demande s'il faut
  les inclure; incluses, elles sont ajoutées à la fin dans une section
  « Notes » bien identifiée. Les notes sont des annotations personnelles,
  pas une partie de la recette, donc elles sont à inclure au cas par cas
  plutôt que toujours présentes — l'impression et l'exportation HTML ne les
  incluent jamais.
- L'exportation PDF n'inclut jamais l'image vedette (pas un choix —
  toujours exclue). Le fichier HTML exporté et la vue d'impression
  l'incluent.
- Les exportations utilisent la langue de l'interface pour leurs
  étiquettes (Ingrédients, Préparation, Portions…); la recette elle-même
  est exportée telle qu'écrite.

**Envoyer une recette par courriel (Partager → Envoyer le PDF par
courriel)**
- Envoie le même PDF que Télécharger le PDF, en pièce jointe, par le compte
  de courriel configuré dans Paramètres → Réception par courriel. L'option
  apparaît dans le menu Partager dès que ce compte a un serveur SMTP, un
  nom d'utilisateur et un mot de passe enregistré; la vérification
  quotidienne de la boîte n'a pas besoin d'être activée.
- L'expéditeur est le compte de réception par courriel (affiché, non
  modifiable). Le champ À accepte jusqu'à cinq adresses séparées par des
  virgules, avec un message facultatif et une case pour inclure vos notes
  quand la recette en a. L'objet est « Recette : <titre> » (« Recipe:
  <title> » en anglais).
- « Choisir dans les contacts » ouvre le sélecteur de contacts du
  téléphone là où le navigateur en a un (Chrome sur Android). Partout
  ailleurs, les adresses auxquelles vous avez déjà envoyé sont proposées
  en suggestions d'un seul toucher; Paramètres → Réception par courriel →
  Destinataires récents les liste, et vous pouvez en retirer ou en ajouter.
- Si l'envoi échoue, le message dit pourquoi, avec le même diagnostic de
  connexion que « Envoyer un courriel de test ».
- Au plus 10 courriels de recettes par heure pour toute l'application (voir
  les Notes de sécurité). Les envois qui échouent ne comptent pas.

**Interface**
- **Anglais ou français.** Paramètres → Langue. Par défaut, la langue de
  l'appareil (le français s'il est réglé à n'importe quel français, sinon
  l'anglais), mémorisée sur chaque appareil, donc deux téléphones de la
  même maison peuvent être différents. Tout est traduit : menus, messages
  du serveur, dates, PDF et HTML exportés, instructions de restauration de
  la sauvegarde et courriels de résultats (voir Réception par courriel).
  Le français est celui du Québec. Les étiquettes et groupes intégrés sont
  affichés en français (Four, Souper, Mode de cuisson) mais conservés sous
  leur nom anglais, donc changer de langue ne change rien à la
  bibliothèque; les étiquettes que vous créez sont affichées telles que
  tapées.
- PWA : installable, avec mise en cache de l'interface pour le hors-ligne
  par un service worker.
- **Les mises à jour apparaissent à la prochaine ouverture.** Les fichiers
  de l'application sont servis avec `Cache-Control: no-cache`, donc le
  navigateur vérifie chaque fois s'il y a une copie plus récente au lieu de
  garder l'ancienne pendant des heures (comme iOS le faisait). Tirer vers
  le bas en haut de la liste recharge les recettes et, si le serveur a une
  version plus récente de l'application (`GET /api/version`), recharge la
  page dans cette version.
- Thème clair / sombre / système (menu des Paramètres). Le mode sombre est
  un vrai noir (`#000000`), pas un gris foncé — les surfaces sont séparées
  par de fines bordures, pas par un fond plus pâle.
- Taille du texte réglable dans l'application (Paramètres), en plus du
  réglage de taille du texte du système. Le zoom de la page est désactivé —
  pincer, toucher deux fois, et le zoom automatique d'iOS quand on touche
  un petit champ de texte — parce qu'il poussait des boutons hors de
  l'écran; les champs de texte font au moins 16 px pour qu'iOS n'ait pas de
  raison de zoomer.
- **Garder l'écran allumé pendant la lecture d'une recette** — pratique
  quand on a les mains occupées et le téléphone appuyé sur le comptoir.
  Facultatif : activez une fois Paramètres → Écran → « Garder l'écran
  allumé pendant la consultation d'une recette », et chaque recette ouverte
  garde l'écran allumé jusqu'à sa fermeture. Ou laissez-le désactivé et
  utilisez l'interrupteur « Garder l'écran allumé » sur les recettes où vous
  le voulez. Il se remet en marche si vous changez d'application et revenez,
  et l'interrupteur montre l'état réel : si le téléphone refuse (mode
  économie d'énergie, par exemple), il s'affiche éteint. Utilise l'API
  Screen Wake Lock du navigateur : Chrome/Edge, Firefox 126+, Safari 16.4+,
  et les applications d'écran d'accueil de l'iPhone à partir d'iOS 18.4.
  Les navigateurs ne l'offrent que sur une page sécurisée, donc il faut
  ouvrir l'application en `https://` (voir *Facultatif : HTTPS sur votre
  réseau*) ou en `localhost`; en simple `http://`, le réglage et
  l'interrupteur de chaque recette n'apparaissent pas. (Un contournement
  par vidéo silencieuse pour `http://` a été essayé et n'a pas gardé un
  iPhone allumé.)
- HTML sémantique, étiquettes ARIA sur les contrôles à icône seule et les
  boîtes de dialogue, focus visible, focus retenu et rendu dans les
  fenêtres modales.

## Notes de sécurité

- **Aucune authentification intégrée par défaut.** Chaque point d'accès de
  l'API est joignable par quiconque peut joindre l'adresse réseau du
  conteneur, sauf si vous activez la clé d'API ci-dessous — pas de
  session, pas d'écran de connexion. Ça correspond à l'usage prévu (un
  outil personnel sur votre propre réseau local ou derrière votre propre
  RPV) — voir l'avertissement au début de ce fichier. Le contrôle d'accès
  est votre responsabilité, et il n'est pas facultatif si quelque chose que
  vous ne contrôlez pas peut joindre l'application.
- **Un mot de passe des paramètres pour les parties qui utilisent des
  identifiants.** Paramètres → Réception par courriel, Paramètres → HTTPS,
  Paramètres → Journal et Partager → Envoyer le PDF par courriel utilisent
  votre compte de courriel ou le jeton DNS de cPanel (le journal contient
  des objets et des expéditeurs de courriels); ils demandent donc un mot de
  passe. Le reste de l'application n'en a pas. La vérification de la boîte
  dans le menu + reste ouverte (elle ne lit que la boîte des recettes).
  - Le mot de passe se crée dans l'application la première fois qu'on
    ouvre l'une de ces parties (au moins 8 caractères). D'ici là, la
    première personne du réseau à en ouvrir une le créerait; ouvrez-en donc
    une peu après l'installation.
  - L'entrer déverrouille ce navigateur pendant 15 minutes. **Verrouiller
    maintenant** et **Changer le mot de passe** sont dans Paramètres → Mot
    de passe des paramètres; le changer verrouille tous les autres
    appareils. Un redémarrage verrouille tout.
  - Il est conservé sous forme de hachage scrypt salé dans
    `admin-password.json`, dans le dossier de données, jamais en clair. Il
    n'est pas dans la sauvegarde .zip; après une restauration, vous en créez
    un nouveau.
  - **Mot de passe oublié :** supprimez `admin-password.json` du dossier de
    données (sans redémarrer); la visite suivante en demande un nouveau. Le
    créer **efface le mot de passe de courriel enregistré et désactive
    HTTPS**, en supprimant le certificat; la personne qui crée le nouveau
    mot de passe entre donc ceux-ci de nouveau. Le serveur de courriel, le
    nom d'utilisateur et la liste des destinataires, le nom et l'adresse
    HTTPS, les recettes et tout le reste sont conservés. Le jeton cPanel
    dans `.env` est hors de portée de l'application et reste. Le premier
    mot de passe d'une installation mise à jour depuis une version
    antérieure à 0052 n'efface rien : l'application garde
    `admin-password.created` pour distinguer une réinitialisation d'une
    première configuration.
  - Après 5 mauvais mots de passe de suite à partir d'une même adresse,
    chaque essai suivant doit attendre (30 secondes, puis le double à
    chaque fois, jusqu'à 15 minutes).
  - Le déverrouillage est un témoin HttpOnly et SameSite=Strict : les
    autres sites ne peuvent pas s'en servir, et les scripts de la page ne
    peuvent pas le lire.
- **Clé d'API facultative** : définissez `RECIPE_APP_API_KEY` pour exiger un
  en-tête `X-API-Key` correspondant sur chaque requête sauf `/healthz`.
  C'est un contrôle au niveau de l'API seulement — il n'y a pas d'écran de
  connexion dans cette version, donc l'utiliser suppose un client d'API qui
  envoie l'en-tête lui-même, ou un proxy inverse configuré pour l'ajouter.
  L'interface de l'application ne fonctionne pas avec la clé seule;
  l'application l'indique dans le journal au démarrage. Une couche
  d'authentification au proxy inverse (authentification de base,
  Tailscale, etc.) reste l'option la plus complète pour exposer
  l'application au-delà de votre réseau local.
- **D'autres sites Web ne peuvent pas utiliser l'application par votre
  navigateur.** « Seul mon réseau peut la joindre » ne couvre pas les pages
  Web que les gens de votre réseau visitent : une telle page peut envoyer
  des requêtes au NAS à partir de leur navigateur. Deux vérifications
  bloquent ça :
  - **Noms d'hôte.** Les requêtes sont acceptées pour les adresses IP,
    `localhost` et les noms de style local (sans point, ou se terminant par
    `.local`, `.lan`, `.home.arpa`, `.internal`). Tout autre nom — p. ex.
    `pantry.example.com` derrière un proxy inverse, ou un nom MagicDNS de
    Tailscale comme `nas.tail1234.ts.net` — doit être inscrit dans
    `RECIPE_APP_ALLOWED_HOSTS` (séparés par des virgules) dans le fichier
    compose, sinon ses requêtes reçoivent une erreur 400 qui le dit. Ça
    bloque le DNS rebinding, où un site fait pointer son propre domaine
    vers l'adresse de votre NAS.
  - **Les écritures exigent un en-tête `X-Requested-With`.** Chaque requête
    qui modifie des données doit le porter; les pages de l'application
    l'ajoutent automatiquement. Une page d'un autre site ne peut pas
    ajouter un en-tête personnalisé sans que le navigateur demande d'abord
    à l'application, et l'application ne l'accepte jamais. Les scripts qui
    appellent l'API directement doivent l'envoyer aussi (n'importe quelle
    valeur).
- **Partager → Envoyer le PDF par courriel envoie à partir de votre compte
  de courriel**; il demande donc le mot de passe des paramètres. C'est
  aussi plafonné : au plus cinq destinataires par courriel et 10 courriels
  par heure. L'option n'est offerte qu'une fois qu'un mot de passe est
  enregistré pour le compte de réception par courriel.
- **Paramètres → HTTPS utilise un jeton d'API cPanel tiré de `.env`.** Le
  jeton ne passe jamais par le navigateur ni par la base de données, et
  l'application ne peut pas l'afficher. La page HTTPS demande le mot de
  passe des paramètres, et même avec lui, elle ne vaut que pour le nom de
  `PANTRY_DOMAIN` : refaire la configuration (au plus 5 demandes
  de certificat par semaine), faire pointer le nom vers une autre adresse de
  votre réseau, ou désactiver HTTPS. Elle ne peut créer aucun autre nom,
  faire pointer le nom vers Internet, ni modifier ou s'approprier un
  enregistrement qu'elle n'a pas créé (donc pas celui de votre site Web), et
  la clé du certificat reste sur le serveur. Le jeton lui-même peut faire tout ce que votre connexion cPanel
  peut faire, donc gardez `.env` privé (`chmod 600`), et donnez une date
  d'expiration au jeton si votre hébergeur le permet (le renouvellement
  s'arrête alors à l'expiration).
- **Changer le serveur de courriel ou le nom d'utilisateur efface le mot de
  passe enregistré**, sauf si un nouveau est saisi dans le même
  enregistrement. Sinon, quiconque peut ouvrir les paramètres du courriel
  pourrait les faire pointer vers son propre serveur et appuyer sur « Envoyer
  un courriel de test » pour recevoir le mot de passe.
- **Les courriels d'inconnus peuvent être ignorés.** Paramètres → Réception
  par courriel → « Accepter les courriels seulement de » accepte des
  adresses et des domaines, avec ou sans le `@` du début (`example.com` et
  `@example.com` sont la même entrée). Les courriels avec le mot-clé venant
  d'autres expéditeurs sont marqués comme lus et listés comme IGNORED dans
  les résultats de vérification. Vide veut dire tout le monde, comme avant.
  L'en-tête From peut être falsifié, donc ça tient à l'écart les gens qui
  tombent par hasard sur l'adresse et le mot-clé — une adresse dédiée et
  impossible à deviner reste importante.
- **Le journal se lit dans Paramètres → Journal** avec le mot de passe des
  paramètres (et depuis l'hôte : `docker logs`, `data/logs/app.log`). Il contient des objets et des
  expéditeurs de courriels et les URL des recettes ajoutées, jamais de mots
  de passe, de clés, de corps de courriels ni de contenu de pages. Le cache
  hors ligne de l'application n'en garde jamais de copie.
- **Les téléchargements de pages sont limités :** 10 Mo par page (20 Mo
  pour les photos), 30 secondes au total, redirections comprises, même
  contre un serveur qui envoie ses données au compte-gouttes plutôt que de
  se taire complètement, et seulement des adresses publiques d'Internet
  (tout ce qui n'est pas routable mondialement est refusé, y compris la
  plage `100.64.0.0/10` qu'utilise Tailscale). Les pages PDF numérisées et
  les photos téléversées sont rendues ou agrandies pour l'OCR dans une
  limite de 35 mégapixels, pour qu'une page ou une image très haute ou très
  étroite ne puisse pas épuiser la mémoire.
- **Les identifiants de courriel (si vous utilisez la réception par
  courriel)** sont chiffrés avec Fernet (AES-128-CBC + HMAC). La clé vient
  de l'un de deux endroits :

  1. **Le bouton Configurer le chiffrement** dans Paramètres → Réception par
     courriel (le plus simple). Il écrit une clé aléatoire dans
     `encryption.key`, dans le dossier de données, lisible seulement par
     l'utilisateur de l'application. Le compromis : la clé est à côté de la
     base de données, donc quiconque peut copier tout le dossier de données
     obtient les deux. La sauvegarde .zip exclut la clé, donc une
     sauvegarde seule n'expose toujours rien.
  2. **`RECIPE_APP_ENCRYPTION_KEY`** dans le fichier compose, si vous voulez
     garder la clé hors du dossier de données. Générez-en une avec le
     Python de l'image, puisque le NAS n'a habituellement pas la
     bibliothèque installée :

     ```bash
     docker exec open-the-pantry python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
     ```

     Si cette variable est définie, elle est utilisée et tout
     `encryption.key` est ignoré. Une valeur définie mais invalide est
     signalée comme une erreur et ne se rabat **pas** sur le fichier de clé.
     Changer de clé en silence ferait cesser de fonctionner les
     identifiants enregistrés sans cause visible.

  Dans les deux cas, perdre la clé veut dire entrer de nouveau le mot de
  passe du courriel; rien d'autre n'est touché. L'application échoue de
  façon sûre : sans clé valide, elle refuse d'enregistrer un identifiant
  plutôt que de le garder en clair, et le champ du mot de passe reste
  désactivé tant qu'une clé n'existe pas. Le mot de passe n'est jamais
  renvoyé par l'API sous aucune forme, chiffrée ou non — seulement un
  booléen qui indique s'il est défini.
- **Deux avertissements propres à la réception par courriel**, à lire avant
  de l'activer :
  - Les paramètres du courriel sont protégés par le mot de passe des
    paramètres, mais le reste de l'application n'a **aucune
    authentification** (voir ci-dessus), et quiconque peut la joindre peut
    essayer des mots de passe, lentement. Choisissez un mot de passe qui ne
    sert nulle part ailleurs. Si des gens en qui vous n'avez pas confiance
    peuvent joindre l'application, placez-la quand même derrière un RPV ou
    un proxy inverse avec authentification.
  - Utilisez une **adresse de courriel dédiée qui ne sert à rien d'autre** —
    un compte ou un alias créé seulement pour ça, avec un mot de passe
    propre à l'application. Pas votre compte personnel principal, ni un
    compte familial partagé. L'application n'a besoin que de lire une boîte
    et d'envoyer des avis; lui donner les identifiants d'un compte qui
    contient autre chose est une exposition inutile. La vérification se
    limite strictement aux messages dont l'objet contient le mot-clé
    configuré — les autres ne sont jamais analysés — mais l'identifiant
    lui-même donnerait un accès complet à cette boîte s'il était compromis;
    la bonne approche est donc de s'assurer que cette boîte ne contient
    rien qui vaille la peine d'être pris.
- **La limitation du débit** est active par défaut (120 requêtes d'API par
  minute par adresse IP, réglable avec `RECIPE_APP_RATE_LIMIT`) comme
  protection de base contre les abus. Les photos et les fichiers de
  l'application ne sont pas comptés — ils l'étaient, et faire défiler une
  grosse bibliothèque atteignait la limite. Derrière un proxy inverse, tous
  les clients partagent l'adresse du proxy. Elle est en mémoire et propre à
  chaque processus — utile pour la conception à instance unique de cette
  application, mais pas un substitut à une vraie limitation au niveau de
  l'infrastructure si vous attendez du vrai trafic.
- **Une seule instance à la fois** : l'application prend un verrou
  exclusif sur son dossier de données au démarrage et refuse de démarrer
  une deuxième instance sur le même `./data` — SQLite ne gère pas de façon
  sûre plusieurs écrivains en même temps, et ça transforme un risque de
  corruption silencieuse (p. ex. un déploiement en plusieurs répliques par
  erreur) en un échec clair au démarrage.
- **Pas de jetons CSRF; les requêtes intersites sont bloquées autrement.**
  Les jetons CSRF empêchent un site malveillant de profiter de la session
  ouverte d'une victime, et cette application n'a ni session ni témoin
  (cookie) dont profiter. Le risque qui s'applique — une page d'un autre
  site qui envoie des requêtes au NAS à partir d'un navigateur de votre
  réseau — est réglé par la vérification des noms d'hôte et l'en-tête
  `X-Requested-With` obligatoire décrits ci-dessus, pas par des jetons.
- **Traitement des téléversements** : taille plafonnée (20 Mo) et signature
  validée avant que l'analyse ou l'OCR touche le fichier; les fichiers sont
  écrits sur le disque au fur et à mesure plutôt que gardés au complet en
  mémoire; les noms de fichiers enregistrés utilisent une extension tirée
  du contenu validé, jamais du nom envoyé par le client. Les images
  téléversées sont en plus réencodées par Pillow après la validation, ce
  qui élimine tout ce qui n'est pas de vraies données de pixels (contenu
  EXIF, octets en trop à la fin, autre contenu « polyglotte ») — passer la
  vérification de signature prouve seulement qu'un fichier *commence* par
  une signature valide, pas que rien n'y est ajouté. L'orientation EXIF est
  appliquée aux pixels avant que ces métadonnées soient retirées, pour
  qu'une photo en portrait ne sorte pas de côté. Les PDF sont plafonnés à
  60 pages pour borner le pire temps d'OCR, et les traitements OCR/PDF
  simultanés sont plafonnés à 3 pour éviter que des requêtes lourdes se
  disputent le processeur. Les images vedettes capturées automatiquement
  par l'ajout d'URL (téléchargées) ou de PDF (extraites du document) passent
  par la même chaîne de plafond de taille, de vérification de signature et
  de réencodage — elles sont exactement aussi peu fiables qu'un
  téléversement direct, peu importe leur source.
- **Le texte de recherche** est traité comme du texte littéral, pas comme
  une syntaxe de requête — les termes de recherche ne peuvent pas changer
  les colonnes ou les champs cherchés.
- **Les champs de noms de fichiers enregistrés sont sur liste blanche.**
  Tout champ de l'API qui nomme une image enregistrée (`image_path` à la
  création ou à la mise à jour d'une recette) doit avoir exactement la
  forme que l'application génère (`<préfixe>-<uuid32>.<ext>`); les chemins
  absolus, les remontées de dossiers et les composants de dossier sont
  refusés à la couche du schéma, puis de nouveau dans les fonctions qui
  manipulent les fichiers. C'est important parce que `os.path.join()`
  ignore sans rien dire son dossier de base quand on lui donne un chemin
  absolu — sans cette vérification, un champ d'image permettrait de lire
  un fichier arbitraire (l'exportation inclut la cible comme URI de
  données) et d'en supprimer un (la suppression d'une recette efface le
  fichier), y compris la base de données de l'application. Trouvé en
  révision et corrigé; couvert par des tests de régression dans
  `test_security.py`.
- **Une route `/tmp-preview` sert les fichiers brouillons** (l'aperçu
  d'image de l'écran de révision avant l'enregistrement d'une recette), à
  côté de la route `/uploads` habituelle pour les recettes enregistrées.
  Les noms de fichiers sont des UUID impossibles à deviner dans les deux
  cas, et le contenu d'un brouillon a déjà passé la même validation que
  tout ce qui est dans `/uploads` au moment où il peut être servi — pas une
  frontière de confiance différente, juste un deuxième dossier. Ça veut
  aussi dire qu'un PDF en cours d'ajout peut techniquement être récupéré à
  ce chemin (signature validée, servi comme `application/pdf`) même si
  l'interface n'y fait jamais de lien — seule l'image extraite est
  référencée par l'interface.
- **L'ajout par URL est protégé contre le SSRF** : l'adresse résolue d'une
  URL soumise est comparée aux plages d'adresses privées, de bouclage,
  locales au lien et réservées avant toute requête, et les redirections
  sont suivies à la main avec la même vérification à chaque saut (plutôt
  que laissées au suivi automatique de `requests`, qui contournerait la
  vérification initiale par une 302 vers une adresse interne). C'est une
  vérification au moment de la résolution DNS, pas au moment de la
  connexion, donc elle ne protège pas contre le DNS rebinding. Chaque
  récupération de page passe par ce client protégé. `recipe_scrapers`
  récupérait les pages lui-même avec `scrape_me(url)`, hors de la
  protection; il ne fait maintenant qu'analyser le HTML que l'application a
  déjà récupéré (`scrape_html`), et un test vérifie qu'il ne fait jamais sa
  propre requête. Le DNS rebinding reste une lacune acceptée pour un outil
  de réseau local.
- **Le contenu des recettes est échappé en HTML à l'exportation.** Les
  titres, ingrédients et étapes peuvent venir de pages Web quelconques ou
  de l'OCR — le gabarit d'exportation HTML/PDF échappe tout (échappement
  automatique de Jinja2), donc du balisage injecté dans une page source ne
  peut pas s'exécuter quand vous ouvrez le fichier téléchargé.

- **SQLite fonctionne en mode WAL**, donc les lectures (liste, recherche,
  consultation) ne sont pas bloquées pendant une écriture. Deux fichiers
  auxiliaires (`recipes.db-wal`, `recipes.db-shm`) se trouvent donc à côté
  de `recipes.db` dans `./data` — ils font partie du même montage, donc les
  sauvegardes habituelles du volume les incluent déjà, mais un simple
  `cp recipes.db` pendant que l'application fonctionne peut manquer des
  données pas encore reportées dans le fichier principal; arrêtez le
  conteneur (ou utilisez `sqlite3 recipes.db "PRAGMA wal_checkpoint(FULL);"`)
  avant de copier ce seul fichier. Le WAL repose sur un verrouillage de
  fichiers en mémoire partagée qui n'est pas fiable sur certains systèmes
  de fichiers réseau (notamment NFS) — sans problème sur un disque local,
  la plupart des configurations de NAS (SMB, montages de blocs locaux) et
  les montages Docker habituels, ce qui couvre les déploiements documentés
  de cette application.

## Limites connues

- **Paramètres → HTTPS ne gère que cPanel.** Les autres hébergeurs DNS
  peuvent utiliser le fichier Traefik à la place (voir *Facultatif : HTTPS
  sur votre réseau*).
- **Le réglage de langue traduit l'application, pas vos recettes.** Une
  recette ajoutée en anglais reste en anglais dans l'interface française,
  et l'inverse. Les noms d'étiquettes et de groupes que vous créez sont
  affichés tels que tapés.
- **« Choisir dans les contacts » ne fonctionne que sur Android pour
  l'instant.** L'API Contact Picker existe dans Chrome sur Android; Safari
  sur iOS ne l'offre pas aux applications Web, donc sur un iPhone, vous
  tapez l'adresse une fois puis la choisissez ensuite dans la liste des
  destinataires récents.

- **La réception par courriel exige un certificat reconnu par le
  système.** Les connexions vérifient le certificat et le nom d'hôte du
  serveur de courriel. Un certificat autosigné, ou un nom d'hôte qui ne
  correspond pas au certificat (fréquent en hébergement mutualisé, où
  `mail.votredomaine` pointe vers le serveur du fournisseur), échoue avec
  `CERTIFICATE_VERIFY_FAILED`. Utilisez le nom d'hôte pour lequel le
  certificat de votre fournisseur est émis.
- **Les vérifications de courriel récupèrent l'en-tête de chaque message
  non lu séparément.** Ça va pour une boîte dédiée; c'est lent pour une
  boîte partagée avec des centaines de messages non lus. C'est pour ça que
  ce document recommande une adresse dédiée.

- **Certains sites de recettes refusent les requêtes automatisées.** Les
  sites derrière la vérification anti-robots de Cloudflare (et les sites
  payants comme NYT Cooking) répondent à l'application par une erreur HTTP
  403 et une page « Just a moment... »; l'application dit que le site a
  refusé et quoi faire à la place, et le journal l'inscrit en une ligne
  (`otp.url`). L'application n'essaie pas de contourner ça. Enregistrez la
  page en PDF (Safari sur iPhone : Partager → Options → PDF; ailleurs
  Imprimer → Enregistrer en PDF), faites des captures d'écran ou collez le
  texte, et ajoutez ou envoyez ça par courriel à la place.
- **Les captures pleine page enregistrées en PDF peuvent être coupées.**
  Une page PDF ne peut pas dépasser 200 pouces de haut, et iOS arrête une
  longue page là, donc la fin de la fiche recette d'un long article de
  blogue peut tout simplement ne pas être dans le fichier. Utilisez
  Imprimer → PDF, qui découpe la page, ou le bouton d'impression du site.
  Un PDF coupé avant les étapes est refusé avec ce conseil (voir Ajout de
  recettes).
- L'analyse heuristique (URL sans JSON-LD, découpage des PDF et de l'OCR)
  repose sur des expressions régulières et des règles, pas sur
  l'apprentissage automatique — attendez-vous à corriger des champs sur des
  mises en page désordonnées ou inhabituelles, avec l'écran de révision
  affiché après l'ajout d'un seul élément. L'ajout par lot saute cette
  révision, donc vérifiez les résultats ensuite.
- La fiabilité de l'OCR des captures est affichée par une pastille
  (« Qualité de la reconnaissance : faible », etc.) d'après la fiabilité mot par mot de
  Tesseract; traitez une extraction peu fiable comme un brouillon de
  départ, pas comme une recette finie.
- Pas de reconnaissance de l'écriture à la main. C'est une limite connue
  de Tesseract, pas un bogue — utilisez plutôt la saisie manuelle.
- L'extraction de l'image vedette d'un PDF choisit la plus grande image
  incluse au-dessus d'une taille minimale — une heuristique, pas une
  compréhension de la mise en page. Un PDF avec plusieurs photos de taille
  semblable peut ne pas choisir celle que vous attendez; utilisez l'option
  « Ne pas utiliser cette image » de l'écran de révision, ou remplacez-la
  ensuite à partir de la page de la recette.

## Journal

**Paramètres → Journal** (demande le mot de passe des paramètres) affiche le journal dans l'application : les 500
entrées les plus récentes, tout ou seulement les avertissements et les
erreurs (une trace d'appels reste avec son entrée), plus **Télécharger**
(tout le journal, tous les fichiers en rotation, en un seul `.txt`) et
**Vider le journal** (le vide, après confirmation). Gardé à environ 4 Mo :
les entrées les plus anciennes disparaissent d'elles-mêmes, donc le vider
n'est jamais nécessaire.

L'application journalise chaque étape de l'ajout par courriel et par lien,
chaque erreur interceptée n'importe où dans le serveur (avec la trace
d'appels), chaque requête qui échoue, et les erreurs non interceptées du
navigateur. Elle écrit à deux endroits :

```bash
docker logs open-the-pantry                 # since the container started
tail -f /path/to/data/logs/app.log          # survives container restarts
```

(`docker logs` couvre depuis le démarrage du conteneur; `app.log` survit
aux redémarrages.) `app.log` est dans le dossier de données à côté de
`recipes.db`, en rotation à 1 Mo avec trois anciens fichiers conservés. Il
n'est pas inclus dans la sauvegarde .zip.

Filtres utiles :

```bash
docker logs open-the-pantry 2>&1 | grep otp.scan    # what each scan found and did
docker logs open-the-pantry 2>&1 | grep otp.email   # how each email was read
docker logs open-the-pantry 2>&1 | grep otp.url     # page fetches: status, redirects, blocks
docker logs open-the-pantry 2>&1 | grep otp.ocr     # photo orientation, OCR confidence
docker logs open-the-pantry 2>&1 | grep otp.client  # errors from the app in your browser
docker logs open-the-pantry 2>&1 | grep -A20 WARNING   # failures, with tracebacks
```

Dans l'ordre : ce que chaque vérification a trouvé et fait; comment chaque
courriel a été lu; les récupérations de pages (statut, redirections,
blocages); l'orientation des photos et la fiabilité de l'OCR; les erreurs
de l'application dans votre navigateur; les échecs, avec leur trace
d'appels.

Mettez `RECIPE_APP_LOG_LEVEL: DEBUG` dans la section `environment:` du
fichier compose pour plus de détails, ou `WARNING` pour moins. Au niveau
par défaut, les erreurs dont l'application se remet couramment (nettoyage
d'un fichier déjà disparu, champ facultatif absent d'une page de recette)
ne sont pas affichées; DEBUG les montre aussi. Les mots de passe, la clé de
chiffrement, le corps des courriels et le HTML des pages ne sont jamais
journalisés. Les objets, les expéditeurs, les noms et tailles des pièces
jointes et les URL le sont.

## Notes de déploiement

**Limites de ressources.** L'application limite son propre travail (au
plus trois traitements OCR/PDF à la fois, 35 Mpx par page, plafonds de
taille des lots), mais Docker ne plafonne pas le conteneur. Le pire cas
mesuré est d'environ 3,4 Go (trois grosses numérisations à la fois); au
repos, environ 160 Mo. Les fichiers compose ont des lignes `mem_limit`,
`cpus` et `pids_limit` en commentaire si vous voulez un plafond strict.

- L'image publiée est une image Docker ordinaire — elle fonctionne partout
  où Docker fonctionne : un NAS à la maison, un serveur privé virtuel, un
  Raspberry Pi, derrière un proxy inverse (Apache, Nginx, Caddy) avec un
  domaine, ou par un RPV vers un réseau domestique. Elle ne fonctionne
  **pas** sur un hébergement PHP mutualisé sans Docker ni accès root — ça
  demanderait une réécriture, pas un changement de configuration.
- Image multi-architecture (`linux/amd64`, `linux/arm64`) produite par le
  flux GitHub Actions inclus, donc elle fonctionne aussi bien sur des
  serveurs x86 que sur des cartes ARM (p. ex. Raspberry Pi).
- Les migrations de la base se font automatiquement au démarrage
  (`app/init_db.py`) et ne font qu'ajouter — mettre l'image à jour sur un
  volume `./data` existant ajoute les nouvelles colonnes et index sans
  toucher aux recettes existantes.
- Le processus de l'application tourne sous un utilisateur non root dans
  le conteneur (UID 1000 fixe par défaut, ou les variables `PUID`/`PGID`
  pour correspondre à un utilisateur existant de l'hôte). Le conteneur
  démarre quand même brièvement en root pour corriger les propriétaires de
  `./data` au premier démarrage — nécessaire parce que c'est un dossier de
  l'hôte monté dans le conteneur, donc rien dans l'image ne peut en régler
  les permissions à l'avance — puis abandonne ses privilèges avant de
  lancer l'application. Aucun `chown` manuel sur l'hôte n'est nécessaire
  dans le cas par défaut.
- `docker-compose.yml` tel quel suit `:main`, qui suit la branche
  principale et est l'étiquette toujours publiée. `:latest` suit les mêmes
  versions et est interchangeable. Les deux sont pratiques, mais vous
  obtenez ce vers quoi l'étiquette pointe au prochain `docker compose
  pull`. Pour des mises à jour reproductibles (et pour décider exactement
  quand une nouvelle version s'applique), fixez plutôt une étiquette de
  version, p. ex. `image: djerodek/open-the-pantry:1.2.0`, et changez-la
  volontairement. Les étiquettes de version sont publiées en poussant une
  étiquette git `vX.Y.Z` (`git tag v1.0.0 && git push origin v1.0.0` donne
  `:1.0.0` et `:1.0`). L'image de base (`python:3.12-slim`) n'est
  volontairement pas fixée : chaque construction intègre les mises à jour
  de sécurité de Debian, et l'audit des dépendances de l'intégration
  continue fait échouer la construction en cas de vulnérabilité connue.
- Un `HEALTHCHECK` est inclus (sur un point d'accès `/healthz` dédié, pas
  sur des données réelles), pour que `docker ps` et les outils
  d'orchestration distinguent un processus bloqué qui écoute encore d'un
  processus vraiment en bonne santé.
- **À la publication d'une nouvelle version** : augmentez `CACHE_NAME` dans
  `frontend/service-worker.js`. La stratégie de récupération passe d'abord
  par le réseau (le cache ne sert qu'hors ligne), et les fichiers de
  l'application sont revalidés, donc ça sert surtout à éliminer
  rapidement les anciennes entrées du cache plutôt qu'à la justesse — mais
  c'est une bonne habitude à chaque version.

## Tests

```bash
cd backend
pip install -r requirements-test.txt
python -m pytest
```

`pytest.ini` définit `pythonpath = .`, donc les modules de test importent
le paquet de l'application directement, sans manipuler `sys.path` dans
chaque fichier.

Nécessite les mêmes paquets système que l'image Docker (`tesseract-ocr`
plus les bibliothèques de polices et de rendu de WeasyPrint — voir le
Dockerfile), puisque les tests de PDF et d'OCR utilisent les vraies
bibliothèques, pas des simulations. `requirements-test.txt` est séparé de
`requirements.txt` pour que l'image déployée ne contienne pas de
dépendances propres aux tests.

La suite s'exécute sur un dossier de données temporaire isolé pour chaque
session (pas votre vrai `./data`), et couvre : la gestion de base des
recettes, le filtrage cumulatif par étiquettes (logique ET), la recherche
avec l'indication de la source de la correspondance, le filtre de temps
imbriqué, les contraintes des notes, la validation des téléversements
(taille, signature, extension falsifiée, retrait du contenu polyglotte),
l'échappement XSS à l'exportation, la protection SSRF (y compris le cas
des redirections), le cycle de vie des fichiers brouillons, les
heuristiques d'ajout (détection de la couche de texte des PDF, découpage
en sections, analyse des ingrédients, suggestion d'étiquettes), la
couverture du français (chaque texte de l'interface a une version
française) et Paramètres → HTTPS de bout en bout contre une fausse API
cPanel et une autorité de certification de test.

Les tests de la réception par courriel simulent la couche IMAP/SMTP
(`unittest.mock`) plutôt que de monter un serveur de courriel — ils
couvrent l'aller-retour du chiffrement des identifiants et l'échec sûr, le
fait que le mot de passe n'est jamais renvoyé par l'API, chaque méthode
d'extraction (pièce jointe PDF, pièce jointe image, lien dans le corps,
texte du corps), le refus des pièces jointes malveillantes, une
vérification complète, le marquage des courriels impossibles à analyser
comme lus pour ne pas les réessayer sans fin, et les avis qui restent en
file d'attente quand l'envoi échoue. La livraison réelle chez un vrai
fournisseur est la seule chose que ça ne peut pas vérifier — utilisez le
bouton « Envoyer un courriel de test » pour ça.

L'intégration continue (`.github/workflows/ci.yml`) exécute la suite plus
un audit des dépendances et une vérification de la construction Docker à
chaque push ou pull request.

## Publier votre propre version sur Docker Hub

1. Créez un dépôt Docker Hub, p. ex. `djerodek/open-the-pantry`.
2. Générez un jeton d'accès Docker Hub (Account Settings → Security).
3. Dans votre dépôt GitHub, ajoutez les secrets `DOCKERHUB_USERNAME` et
   `DOCKERHUB_TOKEN`.
4. Poussez sur `main` (publie `latest`) ou poussez une étiquette comme
   `v1.0.0` (publie aussi cette étiquette de version) —
   `.github/workflows/docker-publish.yml` s'occupe du reste.
5. Le même flux copie `DOCKERHUB.md` dans la présentation (Overview) du
   dépôt Docker Hub à chaque push sur `main`. Ça demande un jeton avec la
   portée **Read, Write, Delete** (Docker Hub refuse les mises à jour de
   description d'un jeton Read & Write). Avec un jeton plus restreint,
   l'étape échoue seule et l'image est quand même publiée; collez alors
   `DOCKERHUB.md` à la main dans Repository → Overview.

## Organisation du projet

Voir la section *Project layout* de [README.md](README.md) : les noms de
fichiers et de dossiers sont les mêmes, et les descriptions courtes y sont
en anglais, comme le code.
