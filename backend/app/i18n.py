"""Server-side half of the English/French interface.

The app sends X-App-Lang on every request (or ?lang= on a plain
navigation). The middleware in main.py stores it in current_lang for the
request, and translates the human-readable fields of JSON responses
("detail", "message", "messages", "error", "last_problem") on the way out.

The messages themselves stay English in the code, in logs and in the
database. Translation is a table of English patterns and their French
equivalents, applied in order, so a message assembled from several parts
(an SMTP failure plus its diagnosis, say) is translated part by part.
Anything the table doesn't know -- an error text from a mail server or
from Python -- passes through unchanged.

Notification emails have no request behind them, so they use the language
saved with the email settings (the language of whoever last saved them).
"""
import re
from contextvars import ContextVar

SUPPORTED = ("en", "fr")
current_lang: ContextVar[str] = ContextVar("current_lang", default="en")


def normalize(value) -> str:
    v = str(value or "").strip().lower()[:2]
    return v if v in SUPPORTED else "en"


# Exact phrases: export template labels, email subjects and bodies.
_FR_PHRASES = {
    "Servings": "Portions",
    "Prep": "Préparation",
    "Cook": "Cuisson",
    "Total": "Total",
    "Ingredients": "Ingrédients",
    "Instructions": "Préparation",
    "Notes": "Notes",
    "Source": "Source",
    "handwritten / manual entry": "saisie manuelle ou manuscrite",
    "url": "Web", "pdf": "PDF", "screenshot": "capture d'écran", "manual": "saisie manuelle", "email": "courriel",
    "Ingested:": "Reçues :",
    "Failed:": "En échec :",
    "(unknown subject)": "(objet inconnu)",
}


def t(text: str, lang: str | None = None) -> str:
    """An exact phrase in the request's (or the given) language."""
    if (lang or current_lang.get()) != "fr":
        return text
    return _FR_PHRASES.get(text, text)


def _p(english: str, french: str):
    return re.compile(english, re.DOTALL), french


# Ordered: longer and more specific patterns before the fragments they
# contain. French uses a (regular) space before ":"; Quebec style.
_FR_PATTERNS = [
    # Request guards
    _p(r"This host name isn't allowed\. Add it to RECIPE_APP_ALLOWED_HOSTS in docker-compose\.yml\.",
       "Ce nom d'hôte n'est pas autorisé. Ajoutez-le à RECIPE_APP_ALLOWED_HOSTS dans docker-compose.yml."),
    _p(r"Missing X-Requested-With header\. Requests that change data must send it\.",
       "En-tête X-Requested-With manquant. Les requêtes qui modifient des données doivent l'envoyer."),
    _p(r"Missing or invalid X-API-Key header\.", "En-tête X-API-Key manquant ou invalide."),
    _p(r"Too many requests, slow down\.", "Trop de requêtes; ralentissez."),

    # Uploads and ingest
    _p(r"File too large \(max (\d+) MB\)\.", r"Fichier trop volumineux (maximum \1 Mo)."),
    _p(r"Uploaded file is empty\.", "Le fichier envoyé est vide."),
    _p(r"File does not look like a valid PDF\.", "Le fichier ne semble pas être un PDF valide."),
    _p(r"File does not look like a valid image\.", "Le fichier ne semble pas être une image valide."),
    _p(r"File could not be decoded as a valid image\.", "Le fichier n'a pas pu être lu comme une image valide."),
    _p(r"Unknown expected file type\.", "Type de fichier attendu inconnu."),
    _p(r"Invalid image reference\.", "Référence d'image invalide."),
    _p(r"That photo isn't a pending upload; upload it again\.",
       "Cette photo n'est pas un envoi en attente; envoyez-la de nouveau."),
    _p(r"Unknown image reference -- upload the image first\.",
       "Référence d'image inconnue : envoyez d'abord l'image."),
    _p(r"Too many links in one batch \(max (\d+)\)\. Split the list and send the rest separately\.",
       r"Trop de liens dans un même lot (maximum \1). Divisez la liste et envoyez le reste séparément."),
    _p(r"Too many PDFs in one batch \(max (\d+)\)\. Send the rest separately\.",
       r"Trop de PDF dans un même lot (maximum \1). Envoyez le reste séparément."),
    _p(r"These PDFs total (\d+) MB; one batch can be at most (\d+) MB\. Send them in smaller groups\.",
       r"Ces PDF totalisent \1 Mo; un lot peut faire au plus \2 Mo. Envoyez-les en plus petits groupes."),
    _p(r"PDF has (\d+) pages \(limit (\d+)\) -- too large to process\. Try splitting it first\.",
       r"Le PDF compte \1 pages (limite de \2) : trop volumineux à traiter. Essayez d'abord de le diviser."),
    _p(r"Could not read this PDF\.", "Impossible de lire ce PDF."),
    _p(r"Could not read text from this image\.", "Impossible de lire le texte de cette image."),
    _p(r"Could not read text from one of these images\.", "Impossible de lire le texte d'une de ces images."),
    _p(r"No images provided\.", "Aucune image fournie."),
    _p(r"Too many images \(max (\d+)\)\.", r"Trop d'images (maximum \1)."),
    _p(r"Only http/https URLs are supported\.", "Seules les URL http et https sont prises en charge."),
    _p(r"URL has no hostname\.", "L'URL n'a pas de nom d'hôte."),
    _p(r"Could not resolve host\.", "Impossible de trouver ce nom d'hôte."),
    _p(r"URLs resolving to private/internal network addresses aren't allowed\.",
       "Les URL qui mènent à des adresses de réseau privé ou interne ne sont pas autorisées."),
    _p(r"(\S+) took longer than ([\d.]+) seconds to download\.",
       r"Le téléchargement de \1 a pris plus de \2 secondes."),
    _p(r"(\S+) is (\d+) MB; the limit is (\d+) MB\.", r"\1 fait \2 Mo; la limite est de \3 Mo."),
    _p(r"(\S+) is larger than (\d+) MB\.", r"\1 dépasse \2 Mo."),
    _p(r"Too many redirects, or a redirect with no Location header\.",
       "Trop de redirections, ou une redirection sans en-tête Location."),
    _p(r"Could not extract a recipe from this URL automatically\. Use manual entry instead, pasting from the page\.",
       "Impossible d'extraire automatiquement une recette de cette URL. Utilisez plutôt la saisie manuelle "
       "en collant le texte de la page."),

    # Recipes, tags, groups, export
    _p(r"Recipe not found", "Recette introuvable"),
    _p(r'There is already a group called "(.+?)"\.', r"Il existe déjà un groupe nommé « \1 »."),
    _p(r"Group not found", "Groupe introuvable"),
    _p(r"Built-in groups can't be deleted\.", "Les groupes intégrés ne peuvent pas être supprimés."),
    _p(r"No such group\.", "Ce groupe n'existe pas."),
    _p(r"Tag not found", "Étiquette introuvable"),
    _p(r"Only tags added in Settings can be deleted here\.",
       "Seules les étiquettes ajoutées dans les Paramètres peuvent être supprimées ici."),
    _p(r"Could not build the backup archive\.", "Impossible de créer l'archive de sauvegarde."),
    _p(r"There are no recipes to export\.", "Il n'y a aucune recette à exporter."),
    _p(r"Could not build the PDF archive\.", "Impossible de créer l'archive PDF."),

    # Encryption and email settings
    _p(r"An encryption key is already set up\.", "Une clé de chiffrement est déjà configurée."),
    _p(r"Couldn't write the key file to the data folder\. Check that the folder is writable\.",
       "Impossible d'écrire le fichier de clé dans le dossier de données. Vérifiez que le dossier est "
       "accessible en écriture."),
    _p(r"The password wasn't saved\. (\w+) is set in your compose file but isn't a valid key\. "
       r"Fix or remove that line, then restart the container\.",
       r"Le mot de passe n'a pas été enregistré. \1 est défini dans votre fichier compose, mais ce n'est "
       r"pas une clé valide. Corrigez ou supprimez cette ligne, puis redémarrez le conteneur."),
    _p(r"The password wasn't saved\. Email passwords are stored encrypted, and no encryption key is set up "
       r"yet\. Use \"Set up encryption\" at the top of the email settings, then save again\.",
       "Le mot de passe n'a pas été enregistré. Les mots de passe de courriel sont conservés chiffrés, et "
       "aucune clé de chiffrement n'est encore configurée. Utilisez « Configurer le chiffrement » en haut "
       "des paramètres de courriel, puis enregistrez de nouveau."),
    _p(r"No encryption key is set up, so the saved email password can't be read\. If you moved or restored "
       r"the data folder, bring encryption\.key with it \(or set (\w+) to the same key as before\)\.",
       r"Aucune clé de chiffrement n'est configurée; le mot de passe de courriel enregistré ne peut donc pas "
       r"être lu. Si vous avez déplacé ou restauré le dossier de données, apportez aussi encryption.key "
       r"(ou donnez à \1 la même clé qu'avant)."),
    _p(r"encryption\.key in the data folder doesn't contain a valid key \(it may be empty or damaged\)\. "
       r"Put back the copy it came from\. If that's not possible, delete the file, then use "
       r"\"Set up encryption\" and enter the password again\.",
       "encryption.key, dans le dossier de données, ne contient pas de clé valide (le fichier est peut-être "
       "vide ou endommagé). Remettez la copie d'origine. Si c'est impossible, supprimez le fichier, "
       "puis utilisez « Configurer le chiffrement » et entrez le mot de passe à nouveau."),
    _p(r"The password wasn't saved\. (?=encryption\.key)", "Le mot de passe n'a pas été enregistré. "),
    _p(r"The saved email password can't be read\. ", "Le mot de passe de courriel enregistré ne peut pas être lu. "),
    _p(r"Stored credential could not be decrypted -- the encryption key may have changed since it was "
       r"saved\. Re-enter email credentials in Settings\.",
       "Le mot de passe conservé n'a pas pu être déchiffré : la clé de chiffrement a peut-être changé "
       "depuis. Entrez de nouveau les identifiants de courriel dans les Paramètres."),
    _p(r"Changing the mail server or username clears the saved password\. Enter the password again to keep "
       r"email ingest enabled\.",
       "Changer le serveur de courriel ou le nom d'utilisateur efface le mot de passe enregistré. Entrez de "
       "nouveau le mot de passe pour garder la réception par courriel activée."),
    _p(r"IMAP host, username, and a password are all required before enabling email ingest\.",
       "Le serveur IMAP, le nom d'utilisateur et un mot de passe sont requis avant d'activer la réception "
       "par courriel."),
    _p(r"Fill in IMAP host, SMTP host, username, and password first\.",
       "Remplissez d'abord le serveur IMAP, le serveur SMTP, le nom d'utilisateur et le mot de passe."),
    _p(r"Set a notification email address first\.", "Indiquez d'abord une adresse de courriel pour les avis."),

    # Email test
    _p(r"Sending: OK -- a \[TEST\] email went to (\S+)\.", r"Envoi : OK. Un courriel [TEST] a été envoyé à \1."),
    _p(r"Sending \(SMTP\) failed: ", "L'envoi (SMTP) a échoué : "),
    _p(r"Reading: OK -- logged in to the inbox\.", "Lecture : OK. Connexion à la boîte de réception réussie."),
    _p(r"Reading \(IMAP\) failed: ", "La lecture (IMAP) a échoué : "),
    _p(r"Check that the test email arrived\.", "Vérifiez que le courriel de test est bien arrivé."),
    _p(r"Recipes can still be ingested; only the result notifications can't be sent until sending works\.",
       "Les recettes peuvent quand même être reçues; seuls les avis de résultat ne peuvent pas être envoyés "
       "tant que l'envoi ne fonctionne pas."),

    _p(r"Could not connect/login to (SMTP|IMAP) \((\S+), implicit TLS\): ",
       r"Connexion ou authentification \1 impossible (\2, TLS implicite) : "),
    _p(r"Could not connect/login to (SMTP|IMAP) \((\S+), STARTTLS\): ",
       r"Connexion ou authentification \1 impossible (\2, STARTTLS) : "),

    # Share -> Email PDF
    _p(r"Sending email isn't set up\. Fill in the SMTP host, username and password under Settings → Email ingest\.",
       "L'envoi de courriels n'est pas configuré. Remplissez le serveur SMTP, le nom d'utilisateur et le mot "
       "de passe dans Paramètres → Réception par courriel."),
    _p(r"Add at least one recipient\.", "Ajoutez au moins un destinataire."),
    _p(r"At most (\d+) recipients at a time\.", r"Au plus \1 destinataires à la fois."),
    _p(r'"(.*)" isn\'t an email address\.', r"« \1 » n'est pas une adresse courriel."),
    _p(r"That's (\d+) recipe emails in the last hour; the limit is (\d+)\. Try again later\.",
       r"Ça fait \1 courriels de recettes dans la dernière heure; la limite est de \2. Réessayez plus tard."),
    _p(r"The email wasn't sent: ", "Le courriel n'a pas été envoyé : "),
    _p(r"Couldn't build the PDF\.", "Impossible de créer le PDF."),

    # Connection diagnosis (email_client._diagnose_suffix)
    _p(r" -- Diagnosis: can't open a connection to (\S+) on port (\d+) from the container at all\. The app's "
       r"settings aren't the problem; a firewall, your ISP, or the mail host is blocking that port from your "
       r"network\. Try the other port your host lists \(587 for SMTP, 143 for IMAP\), or check the host's "
       r"docs for IP restrictions\.",
       r" -- Diagnostic : impossible d'ouvrir une connexion vers \1 sur le port \2 depuis le conteneur. Les "
       r"paramètres de l'application ne sont pas en cause : un pare-feu, votre fournisseur Internet ou "
       r"l'hébergeur de courriel bloque ce port depuis votre réseau. Essayez l'autre port indiqué par votre "
       r"hébergeur (587 pour SMTP, 143 pour IMAP), ou consultez sa documentation sur les restrictions "
       r"d'adresses IP."),
    _p(r" -- Diagnosis: port (\d+) expects TLS immediately, but the app used STARTTLS\.",
       r" -- Diagnostic : le port \1 attend TLS dès la connexion, mais l'application a utilisé STARTTLS."),
    _p(r" -- Diagnosis: port (\d+) is a STARTTLS port \(the server greets in plain text first\), but the app "
       r"expected TLS immediately\.",
       r" -- Diagnostic : le port \1 est un port STARTTLS (le serveur répond d'abord en clair), mais "
       r"l'application attendait TLS dès la connexion."),
    _p(r" -- Diagnosis: (\S+) has TLS, but its certificate doesn't verify for that hostname\. Use the exact "
       r"server name from the certificate \(shared hosts often want their own name, e\.g\. the server's "
       r"hostname rather than mail\.yourdomain\), or ask the host\.",
       r" -- Diagnostic : \1 utilise TLS, mais son certificat ne correspond pas à ce nom d'hôte. Utilisez "
       r"le nom de serveur exact du certificat (les hébergements partagés veulent souvent leur propre nom, "
       r"p. ex. le nom du serveur plutôt que mail.votredomaine), ou renseignez-vous auprès de l'hébergeur."),
    _p(r" -- Diagnosis: (\S+) speaks TLS, but the handshake failed for a reason other than the certificate "
       r"\(often a very old server with no TLS version in common\)\. Try the host's other port, or check its "
       r"TLS settings\.",
       r" -- Diagnostic : \1 utilise TLS, mais la négociation a échoué pour une autre raison que le "
       r"certificat (souvent un serveur très ancien sans version de TLS en commun). Essayez l'autre port de "
       r"l'hébergeur, ou vérifiez ses réglages TLS."),
    _p(r" -- Diagnosis: (\S+) accepts the connection but never answers\. Usually a firewall or proxy in "
       r"between, or the wrong port\.",
       r" -- Diagnostic : \1 accepte la connexion mais ne répond jamais. Habituellement un pare-feu ou un "
       r"proxy entre les deux, ou le mauvais port."),

    # Inbox scan
    _p(r"A scan is already running\. Try again in a moment\.",
       "Une vérification est déjà en cours. Réessayez dans un moment."),
    _p(r"Email ingest is disabled\.", "La réception par courriel est désactivée."),
    _p(r"Email ingest is not fully configured\.", "La réception par courriel n'est pas entièrement configurée."),
    _p(r"The saved password couldn't be decrypted: ", "Le mot de passe enregistré n'a pas pu être déchiffré : "),
    _p(r"Couldn't connect to the inbox: ", "Impossible de se connecter à la boîte de réception : "),
    _p(r"^Could not connect: ", "Connexion impossible : "),
    _p(r"The result email couldn't be sent \(sending isn't working -- use Send test email to see why\)\. "
       r"It stays queued and goes out once sending works\.",
       "Le courriel de résultat n'a pas pu être envoyé (l'envoi ne fonctionne pas; utilisez « Envoyer un "
       "courriel de test » pour savoir pourquoi). Il reste en attente et partira dès que l'envoi fonctionnera."),
    _p(r"The result email couldn't be sent: ", "Le courriel de résultat n'a pas pu être envoyé : "),
    _p(r'^IGNORED: "(.*)" from (.*): sender isn\'t on the allowed list\.$',
       r"IGNORÉ : « \1 » de \2 : l'expéditeur n'est pas dans la liste autorisée."),
    _p(r"^FAILED: ", "ÉCHEC : "),
    _p(r"^OK: ", "OK : "),
    _p(r' \(from "(.*)", via PDF attachment\)$', r" (de « \1 », par la pièce jointe PDF)"),
    _p(r' \(from "(.*)", via image attachment\)$', r" (de « \1 », par la pièce jointe image)"),
    _p(r' \(from "(.*)", via URL in body \((\S+)\)\)$', r" (de « \1 », par l'URL dans le corps (\2))"),
    _p(r' \(from "(.*)", via email body text\)$', r" (de « \1 », par le texte du corps du courriel)"),
    _p(r" \(left unread; the next scan will retry it\)",
       " (laissé non lu; la prochaine vérification réessaiera)"),
    _p(r"\(unknown subject\)", "(objet inconnu)"),

    # Why an email produced no recipe (email_processing)
    _p(r"Could not read email content: ", "Impossible de lire le contenu du courriel : "),
    _p(r"Could not read PDF attachment: ", "Impossible de lire la pièce jointe PDF : "),
    _p(r"Could not read image attachment: ", "Impossible de lire la pièce jointe image : "),
    _p(r"No recipe found\. Tried: ", "Aucune recette trouvée. Essais : "),
    _p(r": not a valid PDF, or over the size limit", " : PDF invalide, ou trop volumineux"),
    _p(r": no ingredient or step lines found in its text", " : aucune ligne d'ingrédient ou d'étape dans son texte"),
    _p(r": not a supported image \((.+?)\), or over the size limit",
       r" : image non prise en charge (\1), ou trop volumineuse"),
    _p(r": text recognition found no ingredient or step lines",
       " : la reconnaissance de texte n'a trouvé aucune ligne d'ingrédient ou d'étape"),
    _p(r"(\d+) links in the body; at most (\d+) are followed",
       r"\1 liens dans le corps; au plus \2 sont suivis"),
    _p(r"\blink (\S+): ", r"lien \1 : "),
    _p(r"body text: no ingredient or step lines", "texte du corps : aucune ligne d'ingrédient ou d'étape"),
    _p(r"body: empty", "corps : vide"),
]


def translate(text, lang: str | None = None):
    """A message in the request's (or the given) language. Non-strings and
    English pass through unchanged."""
    if not isinstance(text, str) or (lang or current_lang.get()) != "fr":
        return text
    for pattern, french in _FR_PATTERNS:
        text = pattern.sub(french, text)
    return text


# Fields of a JSON response that hold messages for people to read.
MESSAGE_FIELDS = {"detail", "message", "messages", "error", "last_problem"}


def translate_payload(obj, lang: str):
    """Walks a decoded JSON body and translates the MESSAGE_FIELDS values
    (a string, or a list of strings) wherever they appear."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in MESSAGE_FIELDS and isinstance(v, str):
                out[k] = translate(v, lang)
            elif k in MESSAGE_FIELDS and isinstance(v, list) and all(isinstance(x, str) for x in v):
                out[k] = [translate(x, lang) for x in v]
            else:
                out[k] = translate_payload(v, lang)
        return out
    if isinstance(obj, list):
        return [translate_payload(x, lang) for x in obj]
    return obj
