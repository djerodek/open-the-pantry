# Sécurité

*[English](SECURITY.md) · Français*

Ce contre quoi Open the Pantry se protège, et comment. La version courte
est dans le [README](README.fr.md) : **il n'y a pas de connexion, alors
utilisez-la sur votre propre réseau seulement.**

## Mot de passe des paramètres

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
  Quelqu'un sur votre réseau peut faire attendre les paramètres ainsi, même
  avec le bon mot de passe; le redémarrage est la solution.
- **Une fois HTTPS activé**, le mot de passe ne fonctionne qu'à l'adresse
  `https://`.
- **Stockage.** Il est gardé sous forme de hachage scrypt salé dans
  `admin-password.json`.
- **Oublié?** Supprimez `admin-password.json`, puis créez un nouveau mot de
  passe. Le créer efface le mot de passe de courriel et le jeton cPanel
  enregistrés, et désactive HTTPS. Tout le reste est gardé. Un fichier
  endommagé n'est pas pris pour « aucun mot de passe » : les paramètres le
  disent, et le supprimer permet de réinitialiser.

## Tout le reste

- **Le reste de l'application est ouvert à quiconque peut la joindre.**
  `RECIPE_APP_API_KEY` exige un en-tête `X-API-Key` sur chaque requête. Les
  pages n'envoient pas cet en-tête : il n'est donc utile que derrière un
  mandataire qui l'ajoute. Un mandataire qui authentifie, ou un RPV, est la
  meilleure solution.
- **On mange quoi?** (port 8091, désactivée par défaut) n'a pas de
  connexion non plus, et `RECIPE_APP_API_KEY` ne la couvre pas : quiconque
  peut joindre le port peut lire toutes les recettes qu'elle peut choisir,
  notes comprises. Elle est en `http://` seulement, et en lecture seule.
- **Les autres sites Web ne peuvent pas utiliser l'application par votre
  navigateur.**
  - Les requêtes doivent utiliser une adresse IP, `localhost`, ou un nom
    local (`.local`, `.lan`, `.home.arpa`, `.internal`, ou sans point). Tout
    autre nom (un domaine de mandataire, un nom Tailscale) doit être listé
    dans `RECIPE_APP_ALLOWED_HOSTS`. `RECIPE_APP_ALLOWED_HOSTS=*` accepte
    n'importe quel nom, ce qui désactive cette vérification : d'autres sites
    pourraient alors atteindre l'application par votre navigateur (DNS
    rebinding). Listez plutôt les noms; le journal en avertit au démarrage.
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
    32 Mo pour le reste) : rien de plus n'atteint le disque.
  - **Images** : elles sont réencodées, ce qui retire les métadonnées et
    tout ce qui y serait caché. Seuls les décodeurs JPEG, PNG, GIF et WEBP
    sont utilisés, et toute image de plus de 50 mégapixels est refusée. Une
    photo lue pour son texte est d'abord réduite à 35 mégapixels, et 3 au
    plus sont préparées à la fois. Jusqu'à 10 photos réunies en une recette
    partagent la même minuterie d'OCR (plus bas).
  - **PDF** : au plus 60 pages, au plus 3 tâches d'OCR à la fois, et seule
    la plus grande des photos d'un PDF est décodée. La couche texte d'une
    page est mesurée avant d'être lue : au plus 30 000 caractères par page
    et 400 000 en tout, 20 Mo d'instructions de dessin et de polices
    intégrées par page, et 4 Mo de tables de caractères des polices, chaque
    flux décodé seulement jusqu'à ce qui reste. Un PDF qui ne peut pas être
    mesuré, ou qui utilise une compression que la vérification ne connaît
    pas, est refusé.
  - **Fichiers Word, texte et Markdown** : la nature d'un fichier vient de
    son contenu, pas de son nom. D'un fichier Word, seul le texte du
    document est lu (au plus 20 Mo, parmi au plus 2 000 parties), et son
    XML ne va jamais rien chercher ailleurs. Un fichier texte fait au plus
    1 Mo, et une page Web ou un SVG est refusé. Aucun de ces fichiers n'est
    gardé, seulement le texte de la recette.
  - **Minuterie d'OCR** : l'OCR d'une importation s'arrête 10 minutes après
    son début, la page ou la photo en cours comprise. Pour un PDF, elle
    commence à la première page numérisée, donc la préparation de cette
    page compte; pour des photos réunies, au premier passage de Tesseract,
    donc leur enregistrement et l'attente de la première tâche d'OCR libre
    ne comptent pas (les attentes entre les photos ensuite, oui).
  - **Lots** (liens, PDF) : un à la fois, et 15 minutes pour tout le lot,
    envoi compris. Un deuxième lot est refusé avant que l'application lise
    son envoi, et un envoi inactif pendant 60 secondes met fin à la
    requête. Ce qui n'a pas été commencé à temps est indiqué comme pas
    essayé, à envoyer de nouveau.
  - **Courriel** : un courriel de plus de 30 Mo, trop imbriqué, ou que
    l'OCR ne peut pas lire de façon fiable est refusé et marqué lu; les
    expéditeurs non permis sont refusés d'après la ligne From, avant que le
    courriel soit téléchargé.
  - **Archives** : au plus 20 000 fichiers pour une importation, 50 000
    pour une sauvegarde complète, vérifié avant de lire la liste des
    fichiers, et taille limitée à la décompression.
  - **Pages téléchargées** : au plus 10 Mo et 30 secondes en tout, quelle
    que soit la lenteur du serveur, depuis des adresses publiques seulement; chaque redirection est vérifiée
    de nouveau.
  - **Connexions sortantes** (courriel, liens, cPanel, Let's Encrypt) : le
    certificat est vérifié d'après les racines à jour de l'image, et les
    chaînes passant par une clé d'autorité compromise connue sont refusées
    (eMudhra, octobre 2026).
  - **Exportations** : tout le texte des recettes est échappé; les titres
    dans `index.csv` ne peuvent pas s'exécuter comme formules de tableur.
  - **Noms de fichiers enregistrés** : ils doivent avoir exactement la forme
    que l'application génère.
  - Les pages de documentation automatique de l'API (`/docs`,
    `/openapi.json`) sont désactivées.
- **Identifiants.** Le mot de passe de courriel et le jeton cPanel sont
  chiffrés (Fernet) avec `encryption.key`, ou avec
  `RECIPE_APP_ENCRYPTION_KEY`, qui garde la clé hors du dossier de données
  (pour en générer une :
  `docker exec open-the-pantry python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).
  L'API ne renvoie jamais le mot de passe; changer le serveur ou le nom
  d'utilisateur de courriel l'efface, sauf s'il est entré de nouveau; les
  sauvegardes laissent la clé de côté.
- **Ce qu'une sauvegarde laisse de côté**, puisque n'importe qui sur votre
  réseau peut en télécharger une : l'identifiant et le mot de passe de
  courriel, le mot-clé d'objet, les expéditeurs permis, l'adresse de
  notification et les destinataires récents, le nom et les adresses HTTPS,
  ainsi que `encryption.key`, `admin-password.json` et `data/https/` (le
  certificat et l'identifiant cPanel). Une sauvegarde contient quand même
  chaque recette, note et photo, sans chiffrement.
- **Le jeton cPanel** ne peut gérer que le nom enregistré par
  l'application, mais du code qui tournerait dans le conteneur aurait le
  jeton lui-même : donnez-lui une date d'expiration et, là où cPanel le
  permet, un accès DNS seulement.
- **Limite de requêtes** : 120 requêtes d'API par minute par adresse
  (`RECIPE_APP_RATE_LIMIT` pour la changer). Le port d'On mange quoi?
  partage la même limite de requêtes par adresse. Derrière un mandataire,
  tous les clients partagent l'adresse du mandataire.
- **Une seule instance.** L'application verrouille son dossier de données :
  une deuxième copie qui pointe vers le même dossier refuse de démarrer.
- **Dossiers en réseau.** SQLite tourne en mode WAL, qui n'est pas fiable
  sur NFS. Utilisez un disque local, ou un volume local du NAS.
