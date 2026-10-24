# Open the Pantry

*[English](README.md) · Français*

Un gestionnaire de recettes auto-hébergé. On peut ajouter une recette à partir d'un lien, d'un PDF, d'une photo ou d'une capture d'écran, d'un courriel, ou en la tapant. On les parcourt par étiquette, on les cherche, on les note et on les partage. L'interface est en anglais ou en français (Québec). Le tout tient dans une seule image Docker, sans service d'IA.

> ## ⚠️ À utiliser sur votre propre réseau seulement
>
> **Il n'y a pas de connexion.** Quiconque peut joindre l'application peut
> lire, modifier et supprimer toutes les recettes. Les seules exceptions
> sont les parties qui utilisent votre compte de courriel ou votre accès
> DNS, et la restauration d'une sauvegarde. Elles demandent un
> [mot de passe des paramètres](#mot-de-passe-des-paramètres) créé dans
> l'application.
>
> **Ne l'exposez pas à Internet.** Utilisez-la sur votre réseau local, ou de
> l'extérieur par un RPV (WireGuard, Tailscale…) ou un mandataire inverse qui
> fait sa propre authentification. Une faille grave d'accès aux fichiers a
> été trouvée et corrigée tard dans le développement, après plusieurs revues
> qui jugeaient le code sain. D'autres peuvent exister. La frontière du
> réseau est la vraie protection.

> **Non maintenue.** Construite avec des outils de programmation par IA pour
> un usage personnel, et publiée telle quelle : aucun soutien, aucun
> engagement à corriger quoi que ce soit. Les copies (forks) sont les
> bienvenues.

## Ce qui la distingue

Par rapport à Mealie et Tandoor, d'après leur documentation en octobre
2026 :

- **Les photos, captures d'écran et PDF sont lus sans modèle d'IA** : OCR
  et règles sur votre serveur, sans clé d'API ni carte graphique.
  L'importation de photos de Mealie demande un fournisseur d'IA qui lit les
  images; Tandoor lit les photos et les PDF par un modèle d'IA que vous
  branchez. Un modèle peut mieux lire une photo difficile; le
  [corpus d'importation](backend/tests/README.md#import-corpus) mesure ce
  que fait cette application sur vos propres recettes.
- **Elle apprend de vos corrections** (Paramètres → Apprentissage) : la
  mise en page de vos importations et votre façon d'étiqueter, utilisées
  seulement là où c'est mieux que les règles intégrées sur vos recettes.
- **Anglais et français du Québec**, dans l'interface et dans ce qu'elle
  lit.
- **Recettes par courriel**, envoyées à une boîte que l'application
  vérifie.
- **On mange quoi?**, une page en lecture seule qui choisit une recette au
  hasard parmi vos choix.
- **Vos propres mesures** : goût, rythme, difficulté et le vrai temps de
  cuisson noté, tous utilisables comme filtres.
- **Un seul conteneur**, SQLite, aucun compte.

**Pas pour vous si** vous voulez planifier des repas, faire des listes
d'épicerie, calculer la valeur nutritive ou avoir plusieurs utilisateurs
avec leur propre compte. Mealie et Tandoor offrent tout cela.

## La lancer

Il faut Docker avec Compose 2.24 ou plus récent (`docker compose version`).

```bash
curl -O https://raw.githubusercontent.com/djerodek/open-the-pantry/main/docker-compose.yml
docker compose up -d
```

Ouvrez `http://<serveur>:8090`. Puis **créez tout de suite le mot de passe
des paramètres** : ouvrez Paramètres → Réception par courriel, HTTPS ou
Journal, et il vous en demande un. Tant qu'il n'existe pas, la première
personne de votre réseau à ouvrir une de ces sections le choisit.

- **Construire depuis les sources** plutôt : `docker compose -f docker-compose.build.yml up -d --build`.
- **Mises à jour** : `docker compose pull && docker compose up -d`. La base
  de données est mise à jour automatiquement au démarrage, sans toucher aux
  recettes.
  - `:main`, l'étiquette qu'utilise le fichier compose, suit chaque
    construction. Pour choisir quand mettre à jour, épinglez une empreinte :
    `image: djerodek/open-the-pantry@sha256:<empreinte>`.
    `docker image inspect --format '{{index .RepoDigests 0}}' djerodek/open-the-pantry:main`
    affiche celle de l'image qui tourne.
- **Images** : `linux/amd64` et `linux/arm64` (un NAS, un VPS, un Raspberry
  Pi). L'application tourne sous un utilisateur non root (UID 1000, ou
  `PUID`/`PGID`). Au démarrage, le conteneur corrige le propriétaire du
  dossier de données : aucun `chown` n'est nécessaire.
- **Mémoire** : environ 160 Mo au repos, jusqu'à environ 3,4 Go au pire
  (trois grosses numérisations à la fois). Les fichiers compose ont des
  lignes `mem_limit`/`cpus` en commentaire pour fixer un plafond.
- **Fuseau horaire** : réglez `TZ` dans le fichier compose (il est livré
  avec `UTC`). Il fixe l'heure de la vérification quotidienne du courriel
  et des sauvegardes automatiques.

### Vos données

Tout est dans un seul dossier, la partie gauche de la ligne `volumes:`
(`./data` par défaut) :
- `recipes.db` : la base de données;
- `uploads/` : les photos et les PDF;
- `encryption.key` : la clé du mot de passe de courriel et du jeton cPanel;
- `admin-password.json` : le mot de passe des paramètres;
- `https/` : le certificat et l'accès cPanel enregistré;
- `logs/` : les journaux;
- `ml/` : les modèles appris, reconstruits à partir des recettes;
- `backups/` : les sauvegardes automatiques, sauf si vous avez choisi un
  autre dossier.

**Pour le déplacer sur un autre disque :**
1. Lancez `docker compose down`. Ne copiez pas pendant que l'application
   tourne : la base pourrait être en pleine écriture.
2. Copiez le dossier : `cp -a ./data /nouveau/chemin`.
3. Changez seulement la partie gauche de la ligne du volume :
   `- /nouveau/chemin:/app/data`.
4. Lancez `docker compose up -d`. Vérifiez que vos recettes y sont avant de
   supprimer l'ancien dossier.

**Pour restaurer une sauvegarde complète :** utilisez Paramètres →
Sauvegarde et exportation → Restaurer ou importer (voir
[Sauvegarde et restauration](#sauvegarde-et-restauration)). Si
l'application ne démarre pas, faites-le à la main :
1. Lancez `docker compose down`.
2. Décompressez dans le dossier de données :
   `unzip -o open-the-pantry-backup-*.zip -d ./data`.
3. Lancez `docker compose up -d`.

Une sauvegarde ne contient ni `encryption.key`, ni `admin-password.json`, ni
`https/`. Sur une nouvelle installation, copiez-les depuis l'ancien dossier,
ou bien configurez à nouveau le courriel et HTTPS et créez un nouveau mot de
passe des paramètres.

### Facultatif : HTTPS sur votre réseau

HTTPS est désactivé par défaut. **Paramètres → HTTPS** donne à
l'application une adresse `https://` avec un vrai certificat Let's Encrypt,
pour votre réseau local et votre RPV seulement; rien n'est exposé à
Internet. Les navigateurs refusent certaines fonctions aux pages en
`http://` : ici, « garder l'écran allumé » et l'installation de
l'application sur Android.

Cela fonctionne avec un domaine dont le DNS est géré dans **cPanel**.
L'application crée un enregistrement pour un nouveau nom (p. ex.
`pantry.example.com`) qui pointe vers l'adresse locale de votre serveur.
Elle prouve que le nom vous appartient avec un enregistrement temporaire,
et elle renouvelle le certificat d'elle-même.

**Mise en place :**
1. Dans cPanel, créez un jeton d'API (Sécurité → Gérer les jetons d'API).
   Donnez-lui une date d'expiration. Si votre cPanel le permet,
   limitez-le au DNS (Éditeur de zone).
2. Dans Paramètres → HTTPS (qui demande le mot de passe des paramètres),
   entrez :
   - le nom d'utilisateur cPanel;
   - l'adresse de cPanel avec son port, p. ex.
     `https://cpanel.example.com:2083`;
   - le jeton;
   - le nom à utiliser.

   Appuyez sur **Enregistrer l'identifiant**. Le jeton est enregistré chiffré et
   n'est plus jamais affiché.
3. Entrez l'adresse locale du serveur et un courriel pour Let's Encrypt,
   puis appuyez sur **Configurer HTTPS**. Après une minute ou deux, le lien
   s'affiche, p. ex. `https://pantry.example.com:8443`. Sur un iPhone,
   ajoutez cette adresse à l'écran d'accueil de nouveau : les réglages comme
   la langue et le thème sont gardés par adresse.

Le fichier compose par défaut publie déjà le port 8443, et aucun
redémarrage n'est nécessaire. Pour `https://` sans port, publiez plutôt
`443:8443` et réglez le port à 443 dans les paramètres (seulement si rien
d'autre sur la machine n'utilise le 443). L'ancienne adresse
`http://…:8090` continue de fonctionner.

**Garde-fous :**
- L'application ne crée et ne modifie que l'enregistrement du nom
  enregistré. Un nom déjà utilisé est refusé : votre site Web ne peut pas
  être redirigé.
- Le nom ne peut pointer que vers une adresse privée ou de RPV : `10.x`,
  `172.16`–`172.31`, `192.168.x`, ou `100.64`–`100.127` pour Tailscale.
- Au plus 5 demandes de certificat par semaine, la limite de Let's Encrypt.
- Si vous changez le nom d'utilisateur ou l'adresse cPanel sans entrer le
  jeton de nouveau, le jeton enregistré est effacé.
- Si le nom ne se résout pas chez vous, votre routeur bloque peut-être les
  réponses DNS qui pointent vers des adresses privées (« protection contre
  le DNS rebinding »). Autorisez votre domaine là.

`CPANEL_USERNAME`, `CPANEL_TOKEN`, `CPANEL_BASE_URL` et `PANTRY_DOMAIN` dans
un fichier `.env` fonctionnent toujours, et ont priorité sur ce qui est
enregistré dans l'application. Un jeton dans `.env` est en clair : gardez ce
fichier privé.

**Autres hébergeurs DNS** (Cloudflare, OVH, Gandi…) : `docker-compose.https.yml`
fait la même chose avec Traefik. Ne l'utilisez pas en même temps que la voie
cPanel.
- Mettez les réglages dans `.env` (à partir de `https.env.example`) et le
  jeton de l'hébergeur DNS dans `traefik.env` (à partir de
  `traefik.env.example`); seul Traefik lit ce fichier. Les commentaires de
  ces fichiers expliquent chaque réglage.
- Sur cette voie, le mot de passe des paramètres ne fonctionne qu'à
  l'adresse `https://`. Si Traefik est arrêté, ces sections restent fermées
  exprès.
- Traefik lit le socket Docker, ce qui lui donne le contrôle de Docker. Un
  mandataire de socket qui ne permet que de lister les conteneurs règle
  cela, si c'est important sur votre réseau.

### Facultatif : On mange quoi? (une recette au hasard)

C'est désactivé par défaut. **On mange quoi?** est une petite page en
lecture seule, sur son propre port, le 8091. Vous choisissez des critères si
vous voulez (type de repas, mode de cuisson, ingrédient principal, rythme)
et vous appuyez sur **Choisis pour moi**. Des titres défilent et s'arrêtent sur une
recette qui correspond. **Autre chose** en choisit une autre, sans
répétition tant que toutes ne sont pas sorties; au-delà de 500
correspondances, seules les 500 dernières montrées sont retenues. Touchez la
carte pour lire la recette, ou l'imprimer. **Modifier dans Open the
Pantry** l'ouvre dans l'application principale.

- Dans un groupe, les choix sont des options (Souper ou Dîner). D'un groupe
  à l'autre, tous doivent correspondre.
- Les recettes mieux notées sortent un peu plus souvent (5★ a une fois et
  demie plus de chances que 1★). Les cocktails ne sont jamais choisis.
- Elle retient vos choix sur chaque appareil, et a son propre sélecteur de
  langue.

**Activez-la** dans Paramètres → On mange quoi?. Elle démarre aussitôt, et
un lien apparaît dans la barre du haut.

**Si le port 8091 est déjà pris sur votre machine**, publiez-en un autre,
p. ex. `"9000:8091"`, et réglez `RECIPE_APP_PICKER_PUBLIC_PORT: "9000"`. Les
commentaires de `docker-compose.yml` montrent où. Derrière un mandataire,
utilisez `RECIPE_APP_PICKER_PUBLIC_URL` et `RECIPE_APP_PUBLIC_URL`
(l'adresse de l'application principale) pour que les deux pages se lient
correctement.

**Elle n'a pas de connexion, et `RECIPE_APP_API_KEY` ne la couvre pas.**
Quiconque peut joindre le port peut lire toutes les recettes qu'elle peut
choisir, vos notes comprises. Elle est en `http://` seulement, et rien ne
peut y être modifié. Si c'est plus que vous ne voulez, laissez-la
désactivée.

## Ce qu'elle fait

### Ajouter des recettes

- **À partir d'un lien.** L'application essaie `recipe-scrapers` (plus de
  100 sites), puis les données schema.org de la page, puis la page
  elle-même. Elle garde aussi la photo de la page. On peut ajouter un lien,
  ou jusqu'à 50 à la fois.
- **À partir d'un PDF.** Les pages avec du texte sont lues directement. Les
  pages numérisées passent par l'OCR, d'abord redressées et remises à
  l'endroit. La plus grande photo du PDF devient la photo de la recette. On
  peut ajouter un fichier, ou jusqu'à 20 à la fois.
- **À partir d'une photo ou d'une capture d'écran.** L'OCR la lit, après
  l'avoir remise à l'endroit et redressée. **Combiner plusieurs** réunit
  jusqu'à 10 captures en une seule recette. L'OCR ne lit pas l'écriture à la
  main : tapez ces recettes.
- **Tapée à la main.** Des champs pour tout, plus une photo. Dans la boîte
  Ingrédients ou Étapes, une ligne courte qui se termine par « : » commence
  une section, p. ex. « Sauce: ».
- **Partagée depuis une autre application.**
  - Android, une fois l'application installée depuis le navigateur (il faut
    HTTPS) : l'application apparaît dans le menu de partage.
  - iPhone : créez un raccourci qui reçoit des **URL** depuis la **feuille de
    partage**, avec deux actions :
    1. **Encoder l'URL** sur l'entrée du raccourci.
    2. **Ouvrir les URL** : `http://<serveur>:8090/?share_url=` suivi du
       texte encodé.

  Dans les deux cas, le lien est rempli, mais rien n'est importé avant que
  vous appuyiez sur **Récupérer la recette**.
- **Par courriel** : voir
  [Réception par courriel](#réception-par-courriel-facultatif).
- **Vérifier avant d'enregistrer.** Une recette seule ouvre un écran de
  vérification : les champs, à côté du texte lu dans la source. Les
  recettes enregistrées sans cet écran sont marquées *À vérifier* : lots,
  courriels et importations. Le menu latéral les liste. **Vérifier** ouvre
  la recette avec une liste de points à contrôler; **C'est correct** retire la
  marque.
- **Les recettes incomplètes sont refusées.** Si un lien, un PDF, une photo
  ou un courriel donne des ingrédients sans étapes (ou l'inverse),
  l'importation échoue. L'application dit ce qui manque et propose une
  autre façon de faire. Pour un lien, c'est souvent d'enregistrer la page en
  PDF (Safari : Partager → Options → PDF). **Continuer quand même** ouvre ce
  qui a été lu.
- **Déjà dans votre bibliothèque?** Quand vous ajoutez une recette dont le
  lien ou le titre correspond à une recette enregistrée, l'application vous
  le dit, avec un lien vers celle-ci. Appuyez de nouveau sur Enregistrer pour
  l'ajouter quand même. Les lots et les courriels sautent un lien déjà
  enregistré.
- **Les recettes en français** sont lues comme celles en anglais : titres de
  section, numéros d'étapes, unités (« 2 c. à soupe », « 1,5 tasse ») et OCR
  dans les deux langues.
- **Chaque ligne d'ingrédient est gardée telle qu'écrite**, et elle est
  aussi analysée en quantité, unité et nom.

### Réception par courriel (facultatif)

Envoyez une recette à une boîte que vous avez configurée, avec un mot-clé
dans l'objet (`[RECIPE]` par défaut), et l'application la récupère :
- une fois par jour, à l'heure choisie;
- ou sur demande, avec **Vérifier la boîte de courriel** dans le menu +.

Elle essaie, dans l'ordre :
1. un PDF joint;
2. une photo;
3. les liens du message;
4. le texte lui-même.

Le courriel sans le mot-clé n'est jamais lu.

- **Les résultats** sont renvoyés par courriel, en `[SUCCESS]`, `[FAILURE]`
  ou `[PARTIAL]`, regroupés sur une période d'attente. Un échec liste ce qui
  a été essayé, et pourquoi chaque essai a abandonné. Les résultats qui
  n'ont pas pu être envoyés attendent la vérification suivante.
- **Le courriel illisible est refusé.** Une photo ou un PDF numérisé que
  l'OCR ne lit pas de façon fiable est refusé plutôt qu'enregistré en texte
  brouillé. Tout courriel de plus de 30 Mo l'est aussi.
- **Expéditeurs acceptés.** « Accepter les courriels seulement de » limite les
  adresses ou les domaines qui peuvent ajouter des recettes.
- **Envoyer un courriel de test** vérifie l'envoi et la lecture séparément,
  et dit ce qu'il a trouvé sur le port si une connexion échoue.
- **Utilisez une adresse dédiée**, avec un mot de passe d'application, qui
  ne reçoit rien d'autre. L'application garde le mot de passe de ce compte,
  chiffré. **Configurer le chiffrement** dans les paramètres crée la clé.

### Trouver une recette

- **Les étiquettes** forment trois groupes intégrés (type de repas, mode de
  cuisson, ingrédient principal), plus vos propres groupes et les étiquettes
  Personnalisées.
  - Des étiquettes sont suggérées à partir de mots-clés quand une recette
    est ajoutée, en français et en anglais. Les repas suivent l'usage
    québécois : déjeuner, dîner, souper.
  - Paramètres → Étiquettes peut ajouter les étiquettes suggérées à toutes
    les recettes (vous cochez lesquelles), et gérer les groupes, les
    étiquettes et leurs mots-clés.
- **L'apprentissage** (Paramètres → Apprentissage, activé par défaut)
  entraîne deux petits modèles à partir de vos propres recettes corrigées :
  l'un lit les importations, l'autre suggère des étiquettes.
  - Chaque modèle n'est utilisé que s'il fait mieux que les règles
    intégrées. Les paramètres affichent les scores.
  - Chaque étiquette suggérée dit d'où elle vient (le mot-clé trouvé, ou
    *appris*).
  - Les recettes encore *À vérifier* ne servent pas à l'apprentissage.
  - Les modèles sont gardés dans `data/ml/`. Ils sont reconstruits
    automatiquement, et ne sont pas dans les sauvegardes.
- **La recherche** couvre les titres, les ingrédients, les étapes, les
  notes, le texte d'origine et les étiquettes. Les mots correspondent à
  partir du début (« poul » trouve poulet), et chaque mot doit correspondre.
  Les accents et les majuscules ne comptent pas.
  - **Les fautes de frappe sont pardonnées.** Un mot par lequel rien ne
    commence dans votre bibliothèque est associé aux mots les plus proches
    qui s'y trouvent : « pouelt » trouve poulet, « tomates » trouve tomate.
    La page indique ce qu'elle a cherché. Les mots de moins de quatre
    lettres ne sont pas corrigés.
  - **Excluez des mots** avec un moins : `poulet -cari`, ou `-noix` seul.
    Le mot et ses pluriels sont exclus.
  - **La pertinence d'abord** pendant une recherche : un mot dans le titre
    compte le plus, puis les étiquettes et les ingrédients. Choisissez un
    autre tri pour changer cela.
  - Quand la correspondance n'est pas dans le titre, la fiche montre où
    elle se trouve, avec les mots surlignés.
- **Les filtres** : cumulez les étiquettes (une recette doit les avoir
  toutes), le rythme et le temps de cuisson noté. **Triez** par date, titre,
  note, temps de cuisson ou difficulté. **Effacer les filtres** remet tout à
  zéro.

### Une recette

- **Les notes** se donnent directement sur la fiche : favori, goût de 1 à 5,
  rythme, difficulté.
- **Les portions** et le **temps réel** que vous avez noté (`jj:hh:mm`,
  jusqu'à 60 jours). Le temps réel sert au filtre de temps.
- **Les notes personnelles** : votre propre texte. Elles restent hors des
  partages, sauf si vous le demandez.
- **La photo** : prise de la source, téléversée, remplacée ou retirée.
- **Voir l'original** ouvre la page d'où vient la recette.
- **Ajuster avec ½ / 1× / 2×.** Cela réécrit la quantité au début de
  chaque ligne d'ingrédient, dans le même style que l'original (« 1 1/2
  tasse » → « 3 tasses », « 250 g » → « 125 g », « 2 à 3 » → « 4 à 6 »).
  L'unité suit la nouvelle quantité (« 1 gousse » → « 2 gousses »), et les
  portions sont ajustées aussi.
  - Les quantités dans les étapes ne changent pas.
  - Les lignes qu'elle ne peut pas ajuster sans risque sont marquées « (tel
    qu'écrit) » : « 1.000 g » (mille, ou un?), et deux quantités sur une
    ligne, comme « 1 lb 8 oz ».
  - Rien n'est enregistré : la recette s'ouvre à 1× la fois suivante.
- **Garder l'écran allumé** pendant qu'une recette est ouverte, sur toutes
  les recettes (Paramètres) ou sur une seule. Il faut HTTPS.

### Gérer la bibliothèque

- **Modifier** n'importe quelle recette. Le texte d'origine est affiché à
  côté des champs, pour comparer.
- **Dupliquer** fait une copie à transformer en variante. Elle a sa propre
  copie de la photo.
- **Le mode Sélectionner** (le bouton à droite de la barre d'outils) agit
  sur plusieurs recettes à la fois :
  - les **supprimer**;
  - ajouter ou retirer une étiquette (**Étiquettes…**);
  - avec exactement deux recettes sélectionnées, **Fusionner…**. Vous
    choisissez d'où vient chaque partie : titre, photo, ingrédients et
    étapes, portions et temps, lien, notes, notes personnelles. Les
    étiquettes des deux sont gardées. L'autre recette va dans Récemment
    supprimées. Une fusion qui donnerait plus de 50 étiquettes, ou des
    notes personnelles de plus de 50 000 caractères, est refusée avec la
    raison.
- **Récemment supprimées.** Une recette supprimée disparaît partout et
  attend 30 jours dans **Récemment supprimées**, au menu latéral. De là, on
  peut la remettre, ou la supprimer tout de suite. Après 30 jours, elle est
  supprimée pour de bon, avec sa photo.
- **Paramètres → Vérification de la bibliothèque** liste :
  - les recettes sans ingrédients, étapes, étiquettes ou photo;
  - les recettes dont le fichier de photo manque;
  - les recettes encore à vérifier;
  - les recettes qui semblent être la même.

  Chaque titre ouvre la recette.

### Partager

Depuis le menu **Partager** d'une recette, ou le bouton de partage d'une
fiche :
- **Imprimer**;
- **Télécharger le PDF** : il demande s'il faut inclure vos notes, et
  n'inclut jamais la photo;
- **Télécharger le HTML** : un seul fichier autonome, avec la photo;
- **Télécharger en JSON** : au format schema.org;
- **Image (JPG)** : la recette dessinée en une image de 1080 px dans votre
  navigateur, sans travail pour le serveur. Activez **Inclure mes notes**
  pour les ajouter. Sur un téléphone en HTTPS, **Partager…** ouvre la
  feuille de partage du téléphone;
- **Copier en texte**;
- **Envoyer le PDF par courriel** : il part du compte de réception par
  courriel, vers jusqu'à cinq adresses (au plus 10 courriels à l'heure). Il
  apparaît une fois ce compte configuré, et demande le mot de passe des
  paramètres. « Choisir dans les contacts » ne fonctionne que sur Android.

Les exportations utilisent la langue de l'interface pour leurs libellés. La
recette elle-même est exportée telle qu'écrite.

### Sauvegarde et restauration

Dans Paramètres → Sauvegarde et exportation :
- **Sauvegarde complète** (`.zip`) : la base de données et toutes les
  photos et tous les PDF, plus des instructions de restauration. C'est
  celle qu'on peut restaurer. Elle est faite avec l'API de sauvegarde de
  SQLite, donc elle est cohérente même pendant que l'application tourne.
- **Recettes en PDF** (`.zip`) : un PDF par recette, plus un index. C'est
  une copie de lecture qui s'ouvre partout, et qu'on ne peut pas restaurer.
  Les notes sont incluses, sauf si vous les décochez.
- **Recettes en JSON** (`.zip`) : des fichiers schema.org avec les photos,
  pour d'autres applications de recettes. Les champs propres à cette
  application sont gardés : réimporter le fichier ici ne perd rien.
- **Sauvegardes automatiques** : une sauvegarde complète chaque jour à
  l'heure choisie. Les plus récentes sont gardées (7 par défaut);
  **Sauvegarder maintenant** en fait une tout de suite.
  - Elles vont dans `data/backups/`. Pour une vraie deuxième copie, montez
    un dossier sur un autre disque et réglez `RECIPE_APP_BACKUP_DIR`; le
    fichier compose a les lignes, en commentaire.
  - Seuls les fichiers de sauvegarde de l'application y sont supprimés.
  - Changer l'horaire demande le mot de passe des paramètres.
- **Restaurer ou importer** prend un fichier et le traite selon son type.
  - **Une sauvegarde complète remplace la bibliothèque**, après une
    vérification et une confirmation. Elle demande le mot de passe des
    paramètres. Une copie de sécurité de la bibliothèque actuelle est
    d'abord écrite (`restore-safety-<date>.zip`), et aucun redémarrage
    n'est nécessaire. La configuration HTTPS et le mot de passe de courriel
    de ce serveur sont gardés.
  - **Tout autre fichier ajoute ses recettes** : du `.json` schema.org, un
    `.zip` de ceux-ci, ou une exportation Paprika (`.paprikarecipes`). Une
    recette dont le lien est déjà enregistré est sautée. Les archives sont
    lues avec des limites de taille.

### Interface

- **Anglais ou français (Québec)**, choisi par appareil. Cela couvre
  l'interface, les exportations, les courriels de résultats et les
  étiquettes intégrées. Vos recettes et vos propres étiquettes restent
  telles qu'écrites.
- **Installable** comme une application (PWA). Tirez la liste vers le bas
  pour l'actualiser et passer à une nouvelle version.
- **Thème clair ou sombre.** Le sombre est un vrai noir.
- **Taille du texte** jusqu'à 26 px.
  - Le zoom par pincement est désactivé sur les téléphones et tablettes,
    exprès : il poussait les boutons hors de l'écran.
  - Sur un ordinateur, le zoom du navigateur fonctionne comme d'habitude.
- Sur un grand écran, une recette affiche les ingrédients à côté des
  étapes.
- **Accessibilité** : commandes étiquetées, prise en charge du clavier et
  des lecteurs d'écran. Un balayage automatique vérifie chaque écran (voir
  [Développement](#développement)).

## Sécurité

### Mot de passe des paramètres

Il protège tout ce qui utilise des identifiants extérieurs :
- la réception par courriel, HTTPS et le journal;
- Partager → Envoyer le PDF par courriel;
- la restauration d'une sauvegarde, et les changements à l'horaire des
  sauvegardes automatiques.

Son fonctionnement :
- **Le créer.** La première fois qu'une de ces sections est ouverte, vous
  le créez (8 caractères ou plus). Faites-le juste après l'installation.
- **Déverrouiller.** L'entrer déverrouille ce navigateur pendant 15
  minutes, avec un témoin HttpOnly. **Verrouiller maintenant** et **Changer
  le mot de passe** sont dans les paramètres. Redémarrer le conteneur
  verrouille tous les navigateurs.
- **Mauvais mots de passe.** Après 5 mauvais essais depuis une adresse, ou
  20 depuis toutes les adresses ensemble, chaque nouvel essai doit attendre
  (jusqu'à 15 minutes). Redémarrer le conteneur remet cela à zéro.
- **Une fois HTTPS activé**, le mot de passe ne fonctionne qu'à l'adresse
  `https://`.
- **Stockage.** Il est gardé sous forme de hachage scrypt salé dans
  `admin-password.json`.
- **Oublié?** Supprimez `admin-password.json`, puis créez un nouveau mot de
  passe. Le créer efface le mot de passe de courriel et le jeton cPanel
  enregistrés, et désactive HTTPS. Tout le reste est gardé.

### Tout le reste

- **Le reste de l'application est ouvert à quiconque peut la joindre.**
  `RECIPE_APP_API_KEY` exige un en-tête `X-API-Key` sur chaque requête. Les
  pages n'envoient pas cet en-tête : il n'est donc utile que derrière un
  mandataire qui l'ajoute. Un mandataire qui authentifie, ou un RPV, est la
  meilleure solution.
- **Les autres sites Web ne peuvent pas utiliser l'application par votre
  navigateur.**
  - Les requêtes doivent utiliser une adresse IP, `localhost`, ou un nom
    local (`.local`, `.lan`, `.home.arpa`, `.internal`, ou sans point). Tout
    autre nom (un domaine de mandataire, un nom Tailscale) doit être listé
    dans `RECIPE_APP_ALLOWED_HOSTS`.
  - Les requêtes qui modifient des données doivent porter un en-tête
    `X-Requested-With`.
  - Les pages ne peuvent pas être affichées dans un autre site.
  - Une Content-Security-Policy ne permet que les scripts et connexions de
    l'application. HSTS n'est pas envoyé, parce qu'il briserait les ports
    en `http://`.
- **Tout ce qui entre est traité comme hostile.**
  - **Téléversements** : au plus 20 Mo, vérifiés par leur contenu plutôt
    que leur extension. Une requête est coupée dès qu'elle dépasse sa
    limite (20 Mo par fichier, 1 Go pour un fichier de recettes à ajouter,
    16 Go pour une sauvegarde complète avec le mot de passe des paramètres,
    32 Mo pour le reste, plus 1 Mo chaque fois pour le formulaire
    lui-même) : rien de plus n'atteint le disque.
  - **Images** : elles sont réencodées, ce qui retire les métadonnées et
    tout ce qui y serait caché. Seuls les décodeurs JPEG, PNG, GIF et WEBP
    sont utilisés, et toute image de plus de 50 mégapixels est refusée.
  - **PDF** : au plus 60 pages, et au plus 3 tâches d'OCR à la fois. Parmi
    les photos d'un PDF, seule la plus grande est décodée.
  - **Pages téléchargées** : au plus 10 Mo et 30 secondes, depuis des
    adresses publiques seulement. Chaque redirection est vérifiée de
    nouveau.
  - **Exportations** : tout le texte des recettes y est échappé.
  - **Noms de fichiers enregistrés** : ils doivent avoir exactement la forme
    que l'application génère.
- **Identifiants.** Le mot de passe de courriel et le jeton cPanel sont
  chiffrés (Fernet) avec `encryption.key`, ou avec
  `RECIPE_APP_ENCRYPTION_KEY` si vous la réglez. Le réglage garde la clé
  hors du dossier de données. Pour générer une clé :
  `docker exec open-the-pantry python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
  - L'API ne renvoie jamais le mot de passe.
  - Changer le serveur ou le nom d'utilisateur de courriel efface le mot de
    passe enregistré, sauf s'il est entré de nouveau dans le même
    enregistrement.
  - La sauvegarde laisse la clé de côté.
- **Le jeton cPanel ne peut faire qu'une chose par l'application** : gérer
  le seul nom enregistré. Mais quiconque ferait tourner du code dans le
  conteneur aurait le jeton lui-même. Donnez-lui donc une date
  d'expiration et, là où cPanel le permet, un accès DNS seulement.
- **Limite de requêtes** : 120 requêtes d'API par minute par adresse
  (`RECIPE_APP_RATE_LIMIT` pour la changer). Le port d'On mange quoi?
  partage la même limite de requêtes par adresse. Derrière un mandataire,
  tous les clients partagent l'adresse du mandataire.
- **Une seule instance.** L'application verrouille son dossier de données :
  une deuxième copie qui pointe vers le même dossier refuse de démarrer.
- **Dossiers en réseau.** SQLite tourne en mode WAL, qui n'est pas fiable
  sur NFS. Utilisez un disque local, ou un volume local du NAS.

## Limites connues

- **Paramètres → HTTPS ne prend en charge que cPanel.** Les autres
  hébergeurs DNS peuvent utiliser le fichier Traefik.
- **Certains sites refusent les requêtes automatisées.** C'est le cas des
  sites derrière la vérification anti-robots de Cloudflare, et des sites
  payants. Enregistrez plutôt la page en PDF, prenez des captures d'écran
  ou collez le texte.
- **Les longues captures d'écran enregistrées en PDF peuvent être
  coupées.** iOS arrête une page à 200 pouces. Utilisez Imprimer → PDF, ou
  le bouton d'impression du site.
- **La lecture des PDF et des photos suit des règles** jusqu'à ce que
  l'apprentissage ait assez de vos corrections. Vérifiez les recettes à
  l'écran de vérification, ou dans *À vérifier*.
- **La photo tirée d'un PDF** est sa plus grande image, qui n'est peut-être
  pas celle que vous voulez. Vous pouvez la remplacer.
- **Pas de reconnaissance de l'écriture à la main.** Tapez ces recettes.
- **La réception par courriel exige un certificat reconnu par le système.**
  Utilisez le nom d'hôte pour lequel le certificat de votre fournisseur est
  émis. Elle est aussi lente sur une boîte partagée avec des centaines de
  messages non lus : une raison de plus d'utiliser une adresse dédiée.

## Journaux

**Paramètres → Journal** (qui demande le mot de passe des paramètres)
affiche les entrées les plus récentes, toutes ou seulement les
avertissements. On peut télécharger tout le journal, ou le vider. Le
journal est aussi dans `docker logs open-the-pantry` et dans
`data/logs/app.log` (rotation à 1 Mo, 3 anciens fichiers gardés).

Il consigne :
- chaque étape des importations par lien et par courriel;
- chaque erreur, avec sa trace;
- les erreurs des pages.

Il ne contient jamais de mots de passe, de clés, de corps de courriels ni
de contenu de pages. Les objets, les expéditeurs et les URL des courriels
sont consignés.

Pour trouver un type d'entrée, filtrez avec `grep` :
`docker logs open-the-pantry 2>&1 | grep otp.scan`. Les autres étiquettes
sont `otp.email`, `otp.url`, `otp.ocr` et `otp.client`. Réglez
`RECIPE_APP_LOG_LEVEL` à `DEBUG` pour plus de détails, ou à `WARNING` pour
moins.

## Développement

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

- `tests/README.md` indique quels fichiers de test couvrent quelle
  fonction.
- Le corpus d'importation relit de vraies sources (vos recettes vérifiées,
  ou des fichiers que vous ajoutez) après un changement et signale celles
  qui sortent moins bien : `python -m app.corpus`, décrit dans
  `tests/README.md`.
- Chaque test de navigateur échoue aussi sur une erreur JavaScript ou une
  violation de la Content-Security-Policy.
- `test_accessibility.py` passe axe-core sur chaque écran, en
  anglais/clair et en français/sombre, et échoue sur toute constatation
  sérieuse ou critique. La règle du zoom est désactivée, puisque le zoom
  l'est exprès. Un balayage automatique trouve environ le tiers des
  problèmes d'accessibilité.

L'intégration continue (`.github/workflows/ci.yml`) s'exécute à chaque
envoi. Elle lance les deux suites de tests, un audit des dépendances (qui
échoue sur toute vulnérabilité connue) et une construction Docker. L'image
publiée n'est construite qu'une fois ces étapes réussies.

```
backend/app/        application FastAPI (main.py), modèles, sauvegarde, HTTPS, On mange quoi?
  ingestion/        lecture des liens, PDF, photos et courriels; étiquetage
  ml/               Paramètres -> Apprentissage (numpy seulement)
  templates/        le gabarit d'impression/PDF/HTML
backend/tests/      tests du serveur; corpus/ : cas du corpus d'importation; tests_browser/ : tests des pages
frontend/           l'application : index.html, js/app.js, js/i18n.js, css/
frontend-picker/    la page On mange quoi? (port 8091)
docker-compose*.yml image publiée, construction depuis les sources, HTTPS par Traefik
```
