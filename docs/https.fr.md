# HTTPS sur votre réseau

*[English](https.md) · Français*

HTTPS est désactivé par défaut. **Paramètres → HTTPS** donne à
l'application une adresse `https://` avec un vrai certificat Let's Encrypt,
pour votre réseau local et votre RPV seulement; rien n'est exposé à
Internet. Garder l'écran allumé, l'installation sur Android et le partage
vers l'application depuis Android en ont besoin.

Cela fonctionne avec un domaine dont le DNS est géré dans **cPanel** :
l'application crée un enregistrement pour un nouveau nom (p. ex.
`pantry.example.com`) qui pointe vers l'adresse locale du serveur, prouve
que le nom vous appartient avec un enregistrement temporaire, et renouvelle
le certificat d'elle-même.

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

   Appuyez sur **Enregistrer l'identifiant** (le jeton est enregistré
   chiffré et n'est plus jamais affiché).
3. Entrez l'adresse locale du serveur et un courriel pour Let's Encrypt,
   puis **Configurer HTTPS**. Après une minute ou deux, le lien s'affiche,
   p. ex. `https://pantry.example.com:8443`. Sur un iPhone, ajoutez-le de
   nouveau à l'écran d'accueil : la langue et le thème sont gardés par
   adresse.

Le port 8443 est déjà publié et aucun redémarrage n'est nécessaire. Pour
`https://` sans port, publiez `443:8443` et réglez le port à 443 dans les
paramètres (si rien d'autre n'utilise le 443). `http://…:8090` continue de
fonctionner.

**Garde-fous :**
- Seul l'enregistrement du nom enregistré est créé ou modifié, et un nom
  déjà utilisé est refusé : votre site Web ne peut pas être redirigé.
- Le nom ne peut pointer que vers une adresse privée ou de RPV (`10.x`,
  `172.16`–`172.31`, `192.168.x`, `100.64`–`100.127` pour Tailscale).
- Au plus 5 demandes de certificat par semaine (la limite de Let's Encrypt).
- Changer le nom d'utilisateur ou l'adresse cPanel sans entrer le jeton de
  nouveau l'efface.
- Si le nom ne se résout pas chez vous, votre routeur bloque peut-être les
  réponses DNS vers des adresses privées (« protection contre le DNS
  rebinding ») : autorisez votre domaine là.

`CPANEL_USERNAME`, `CPANEL_TOKEN`, `CPANEL_BASE_URL` et `PANTRY_DOMAIN` dans
un fichier `.env` fonctionnent toujours, et ont priorité sur ce qui est
enregistré dans l'application. Un jeton dans `.env` est en clair : gardez ce
fichier privé. Il ne sert qu'avec `CPANEL_BASE_URL` dans `.env` aussi.

## Autres hébergeurs DNS

Cloudflare, OVH, Gandi… : `docker-compose.https.yml`
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
