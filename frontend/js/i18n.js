// Interface language: English or French (Quebec). Loaded before app.js.
//
// The English text is the key: tx("Deleted {1}.", { 1: name }). A key
// with no French entry falls back to English, so a missed string shows up
// in English rather than breaking anything. {1}, {2}... are filled from
// vars; txn() also fills {n} and picks the singular or plural key.
//
// The choice is stored per device (localStorage). With nothing stored, the
// device language decides: French if it starts with "fr", else English.
(() => {
  "use strict";

  const STORAGE_KEY = "otp-lang";
  const SUPPORTED = ["en", "fr"];

  function storedLang() {
    try { return localStorage.getItem(STORAGE_KEY); } catch { return null; }
  }

  function deviceLang() {
    const langs = navigator.languages && navigator.languages.length ? navigator.languages : [navigator.language || "en"];
    for (const l of langs) {
      const base = String(l).toLowerCase().slice(0, 2);
      if (SUPPORTED.includes(base)) return base;
    }
    return "en";
  }

  const stored = storedLang();
  const lang = SUPPORTED.includes(stored) ? stored : deviceLang();

  function setLang(next) {
    try { localStorage.setItem(STORAGE_KEY, next); } catch { /* private mode: lasts this page only */ }
  }

  // ---------------------------------------------------------------------
  // French catalog. Quebec usage: courriel, déjeuner/dîner/souper for
  // breakfast/lunch/supper, a space before ":" (made non-breaking below).
  // ---------------------------------------------------------------------
  const FR = {
    // General
    "Dismiss": "Fermer",
    "Cancel": "Annuler",
    "Save": "Enregistrer",
    "Saving...": "Enregistrement…",
    "Saved.": "Enregistré.",
    "Delete": "Supprimer",
    "Done": "Terminé",
    "Loading...": "Chargement…",
    "Working...": "Traitement…",
    "Preparing…": "Préparation…",
    "Try again": "Réessayer",
    "error {1}": "erreur {1}",
    "Download": "Télécharger",
    "Download started.": "Téléchargement lancé.",
    "Skip to content": "Aller au contenu",
    "Search": "Rechercher",
    "Clear search": "Effacer la recherche",
    "Search recipes": "Rechercher des recettes",
    "Search recipes...": "Rechercher des recettes…",
    "Settings": "Paramètres",
    "Close dialog": "Fermer la fenêtre",
    "Select": "Sélectionner",
    "Couldn't reach Open the Pantry. Check your connection and try again.":
      "Impossible de joindre Open the Pantry. Vérifiez votre connexion et réessayez.",
    "Not authorised — the API key is missing or wrong.": "Non autorisé : la clé d'API est absente ou incorrecte.",
    "Too many requests just now. Wait a moment and try again.": "Trop de requêtes en ce moment. Attendez un peu et réessayez.",
    "The server is busy with too many requests. Wait a moment and try again.":
      "Le serveur reçoit trop de requêtes. Attendez un peu et réessayez.",
    "The server returned an error ({1}). Nothing was changed.": "Le serveur a renvoyé une erreur ({1}). Rien n'a été modifié.",
    "That didn't work (status {1}).": "Ça n'a pas fonctionné (statut {1}).",
    "Couldn't save that change.": "Impossible d'enregistrer cette modification.",
    "The server didn't respond within {1} seconds. {2} may or may not have gone through — the list has been refreshed so you can check.":
      "Le serveur n'a pas répondu en {1} secondes. {2} a peut-être été traité, peut-être pas; la liste a été actualisée pour que vous puissiez vérifier.",
    "{1} was already gone. The list has been refreshed.": "{1} n'existait déjà plus. La liste a été actualisée.",

    // Pull to refresh
    "Pull to refresh": "Tirez pour actualiser",
    "Release to refresh": "Relâchez pour actualiser",
    "Checking for updates…": "Recherche de mises à jour…",
    "Updating…": "Mise à jour…",
    "Refreshed.": "Actualisé.",

    // Top bar, drawer, sort and group
    "Browse by tag": "Parcourir par étiquette",
    "Tap tags to filter the list.": "Touchez des étiquettes pour filtrer la liste.",
    "Group recipes by": "Regrouper les recettes par",
    "Sort recipes by": "Trier les recettes par",
    "Newest first": "Plus récentes d'abord",
    "Oldest first": "Plus anciennes d'abord",
    "Title A–Z": "Titre A–Z",
    "Title Z–A": "Titre Z–A",
    "Rating: high to low": "Note : de la plus haute à la plus basse",
    "Rating: low to high": "Note : de la plus basse à la plus haute",
    "Cook time: shortest": "Temps de cuisson : le plus court",
    "Cook time: longest": "Temps de cuisson : le plus long",
    "Difficulty: easiest": "Difficulté : la plus facile",
    "Difficulty: hardest": "Difficulté : la plus difficile",
    "Filter recipes": "Filtrer les recettes",
    "0 selected": "0 sélectionnée",
    "{1} selected": "{1} sélectionnée(s)",
    "Delete selected": "Supprimer la sélection",
    "Add recipe": "Ajouter une recette",
    "Add Recipe": "Ajouter une recette",

    // Tag groups and built-in values
    "Meal Type": "Type de repas",
    "Cooking Style": "Mode de cuisson",
    "Main Ingredient": "Ingrédient principal",
    "Custom": "Personnalisées",
    "Cocktail Prep": "Préparation des cocktails",
    "All": "Toutes",
    "Uncategorized": "Sans catégorie",
    "Quick": "Rapide",
    "Moderate": "Modéré",
    "Long": "Long",
    "Easy": "Facile",
    "Medium": "Moyen",
    "Hard": "Difficile",

    // Filters
    "Filters": "Filtres",
    "Close filters": "Fermer les filtres",
    "Filters cleared.": "Filtres effacés.",
    "Clear filters": "Effacer les filtres",
    "Clear filters ({1})": "Effacer les filtres ({1})",
    "Collapse all": "Tout réduire",
    "This group stays open while one of its filters is on.": "Ce groupe reste ouvert tant qu'un de ses filtres est actif.",
    "Expand all": "Tout développer",
    "Pace": "Rythme",
    "Cook Time": "Temps de cuisson",
    "No logged cook times yet — this filter appears once recipes have a real-world time logged.":
      "Aucun temps de cuisson noté pour l'instant. Ce filtre apparaît dès qu'une recette a un temps réel noté.",
    "pace filter": "filtre de rythme",
    "time filter": "filtre de temps",
    "search": "recherche",
    "{1} active": "{1} actif(s)",
    "No filters active": "Aucun filtre actif",
    "{n} tag filter": "{n} filtre d'étiquette",
    "{n} tag filters": "{n} filtres d'étiquette",

    // Recipe list
    "Couldn't load recipes ({1}).": "Impossible de charger les recettes ({1}).",
    "{n} recipe found": "{n} recette trouvée",
    "{n} recipes found": "{n} recettes trouvées",
    "No recipes match. Tap + to add one, or adjust your filters.":
      "Aucune recette ne correspond. Touchez + pour en ajouter une, ou modifiez vos filtres.",
    "Web": "Web",
    "Screenshot": "Capture d'écran",
    "Handwritten/Manual": "Manuscrite/manuelle",
    "Email": "Courriel",
    "OCR quality: low": "Qualité de la reconnaissance : faible",
    "OCR quality: fair": "Qualité de la reconnaissance : passable",
    "OCR quality: good": "Qualité de la reconnaissance : bonne",
    "matched: {1}": "correspondance : {1}",
    "Has notes": "Contient des notes",
    "Remove from favorites": "Retirer des favoris",
    "Add to favorites": "Ajouter aux favoris",
    "Share {1}": "Partager {1}",
    "Share": "Partager",
    "Select {1}": "Sélectionner {1}",
    "Delete {1} recipe(s)? This cannot be undone.": "Supprimer {1} recette(s)? Cette action est irréversible.",
    "Deleting…": "Suppression…",
    "The selected recipes": "Les recettes sélectionnées",
    "{1} deleted": "{1} supprimée(s)",
    "{1} already gone": "{1} déjà absente(s)",
    "{1} failed": "{1} en échec",

    // Ratings
    "How good was it?": "C'était bon comment?",
    "How long does it take?": "Ça prend combien de temps?",
    "How hard is it?": "C'est difficile comment?",
    "Rating": "Note",
    "Cook time": "Temps de cuisson",
    "Difficulty": "Difficulté",
    "Could not save rating.": "Impossible d'enregistrer la note.",
    "Clear rating": "Effacer la note",
    "Could not clear rating.": "Impossible d'effacer la note.",
    "Set {1}": "Définir : {1}",

    // Recipe detail
    "Could not load recipe.": "Impossible de charger la recette.",
    "Days": "Jours",
    "Hours": "Heures",
    "Minutes": "Minutes",
    "d": "j",
    "h": "h",
    "m": "min",
    "Keep screen on": "Garder l'écran allumé",
    "The browser wouldn't keep the screen awake — it may be blocked on low battery.":
      "Le navigateur n'a pas pu garder l'écran allumé; c'est peut-être bloqué quand la pile est faible.",
    "Substitutions, timing tweaks, how it turned out...": "Substitutions, ajustements de temps, le résultat…",
    "View or edit notes": "Voir ou modifier les notes",
    "View original": "Voir l'original",
    "Add notes": "Ajouter des notes",
    "Notes": "Notes",
    "Save notes": "Enregistrer les notes",
    "Notes saved.": "Notes enregistrées.",
    "Could not save notes.": "Impossible d'enregistrer les notes.",
    "Uploading photo...": "Envoi de la photo…",
    "Could not upload photo.": "Impossible d'envoyer la photo.",
    "Photo updated.": "Photo mise à jour.",
    "Could not save photo.": "Impossible d'enregistrer la photo.",
    "Change photo": "Changer la photo",
    "Add photo": "Ajouter une photo",
    "Remove photo": "Retirer la photo",
    "Remove this recipe's photo?": "Retirer la photo de cette recette?",
    "Photo removed.": "Photo retirée.",
    "Could not remove photo.": "Impossible de retirer la photo.",
    "Servings: {1}": "Portions : {1}",
    "Prep: {1}": "Préparation : {1}",
    "Cook: {1}": "Cuisson : {1}",
    "Total: {1}": "Total : {1}",
    "Real-world cook time": "Temps de cuisson réel",
    "Save time": "Enregistrer le temps",
    "Cook time updated.": "Temps de cuisson mis à jour.",
    "Ingredients": "Ingrédients",
    "Instructions": "Préparation",
    "✎ Edit recipe": "✎ Modifier la recette",
    "Delete recipe": "Supprimer la recette",
    "Delete this recipe? This cannot be undone.": "Supprimer cette recette? Cette action est irréversible.",
    "Recipe deleted.": "Recette supprimée.",
    "That recipe": "Cette recette",
    "The recipe": "La recette",

    // Share menu
    "Share recipe": "Partager la recette",
    "Print": "Imprimer",
    "Download PDF": "Télécharger le PDF",
    "Include your notes in the PDF?": "Inclure vos notes dans le PDF?",
    "Download HTML": "Télécharger le HTML",
    "Copy as text": "Copier en texte",
    "Ingredients:": "Ingrédients :",
    "Instructions:": "Préparation :",
    "Recipe copied as text.": "Recette copiée en texte.",
    "Could not copy to clipboard.": "Impossible de copier dans le presse-papiers.",
    "Could not prepare the download; opening it instead.": "Impossible de préparer le téléchargement; ouverture à la place.",

    // Share -> Email PDF
    "Email PDF": "Envoyer le PDF par courriel",
    "From": "De",
    "To": "À",
    "name@example.com, …": "nom@exemple.com, …",
    "name@example.com": "nom@exemple.com",
    "Up to {1} addresses, separated by commas.": "Jusqu'à {1} adresses, séparées par des virgules.",
    "Choose from contacts": "Choisir dans les contacts",
    "Recent:": "Récents :",
    "Add {1}": "Ajouter {1}",
    "Message (optional)": "Message (facultatif)",
    "Include my notes": "Inclure mes notes",
    "Send": "Envoyer",
    "Sending…": "Envoi…",
    "Add at least one recipient.": "Ajoutez au moins un destinataire.",
    "The email wasn't sent (error {1}).": "Le courriel n'a pas été envoyé (erreur {1}).",
    "Sent to {1}.": "Envoyé à {1}.",
    "Recent recipients": "Destinataires récents",
    "Offered when you email a recipe from Share. Addresses are added here each time you send.":
      "Proposés quand vous envoyez une recette par courriel à partir de Partager. Les adresses s'ajoutent ici à chaque envoi.",
    "Remove": "Retirer",
    "Remove {1}": "Retirer {1}",
    "Removed {1}.": "{1} retiré.",
    "None yet.": "Aucun pour l'instant.",
    "Add an address": "Ajouter une adresse",
    "Add": "Ajouter",
    "Added {1}.": "{1} ajouté.",
    "Scanned {1}: {2} ingested, {3} failed.": "{1} vérifié(s) : {2} reçu(s), {3} en échec.",

    // Edit recipe
    "Edit recipe": "Modifier la recette",
    "Suggest tags": "Suggérer des étiquettes",
    "Added {1}. Remove any that don't fit, then save.": "{1} ajoutée(s). Retirez celles qui ne conviennent pas, puis enregistrez.",
    "No new suggestions.": "Aucune nouvelle suggestion.",
    "Couldn't get suggestions ({1}).": "Impossible d'obtenir des suggestions ({1}).",
    "Save changes": "Enregistrer les modifications",
    "Untitled Recipe": "Recette sans titre",
    "Could not save changes.": "Impossible d'enregistrer les modifications.",
    "Saved, but the tag-scan setting couldn't be changed.": "Enregistré, mais le réglage d'analyse des étiquettes n'a pas pu être modifié.",
    "Recipe updated.": "Recette mise à jour.",
    "Title": "Titre",
    "Ingredients (one per line)": "Ingrédients (un par ligne)",
    "Steps (one per line)": "Étapes (une par ligne)",
    "Tags (comma or semicolon separated)": "Étiquettes (séparées par des virgules ou des points-virgules)",
    "Include in Settings → Tags scans": "Inclure dans les analyses de Paramètres → Étiquettes",
    "Servings": "Portions",
    "Prep time": "Temps de préparation",
    "Total time": "Temps total",
    "Original extracted text (reference)": "Texte extrait d'origine (référence)",
    "Read-only. What the scraper or OCR actually produced.": "Lecture seule. Ce que l'extraction ou la reconnaissance de texte a réellement produit.",
    "No original extracted text stored for this recipe.": "Aucun texte extrait d'origine n'est conservé pour cette recette.",

    // Add recipe
    "🔗 URL": "🔗 URL",
    "📄 PDF": "📄 PDF",
    "📷 Screenshot/Photo": "📷 Capture d'écran/photo",
    "✍️ Manual/Handwritten": "✍️ Manuelle/manuscrite",
    "📥 Check email inbox": "📥 Vérifier la boîte de courriel",
    "Single": "Une seule",
    "Batch": "Lot",
    "Combine multiple": "Combiner plusieurs",
    "Add from URL": "Ajouter à partir d'une URL",
    "https://example.com/recipe": "https://exemple.com/recette",
    "Fetching recipe...": "Récupération de la recette…",
    "Extraction failed.": "L'extraction a échoué.",
    "Recipe URL": "URL de la recette",
    "Fetch recipe": "Récupérer la recette",
    "One URL per line": "Une URL par ligne",
    "That's {1} links; the limit is 50 per batch. Send the rest separately.":
      "Ça fait {1} liens; la limite est de 50 par lot. Envoyez le reste séparément.",
    "Fetching {1} recipe(s)...": "Récupération de {1} recette(s)…",
    "Batch import failed (error {1}).": "L'importation par lot a échoué (erreur {1}).",
    "{1} added, {2} failed.": "{1} ajoutée(s), {2} en échec.",
    "Batch import failed.": "L'importation par lot a échoué.",
    "Recipe URLs (one per line)": "URL des recettes (une par ligne)",
    "Each recipe is saved directly using auto-detected fields — open it afterward to correct anything.":
      "Chaque recette est enregistrée directement avec les champs détectés automatiquement. Ouvrez-la ensuite pour corriger au besoin.",
    "Import all": "Tout importer",
    "Add from PDF": "Ajouter à partir d'un PDF",
    "Only part of the recipe came through": "Seule une partie de la recette a été récupérée",
    "Couldn't import this page": "Impossible d'importer cette page",
    "A PDF of the page usually works: your browser has the whole page, and the app reads recipe text from PDFs well.":
      "Un PDF de la page fonctionne habituellement : votre navigateur a la page complète, et l'application lit bien le texte des recettes en PDF.",
    "Make a PDF": "Faire un PDF",
    "iPhone or iPad (Safari): Share → Options → PDF, then Save to Files.":
      "iPhone ou iPad (Safari) : Partager → Options → PDF, puis Enregistrer dans Fichiers.",
    "Android (Chrome): ⋮ → Share → Print → Save as PDF.": "Android (Chrome) : ⋮ → Partager → Imprimer → Enregistrer au format PDF.",
    "Computer: Print → Save as PDF.": "Ordinateur : Imprimer → Enregistrer au format PDF.",
    "Or email the PDF to the recipe inbox as an attachment, if email ingest is set up.":
      "Ou envoyez le PDF en pièce jointe à la boîte de réception des recettes, si la réception par courriel est configurée.",
    "Continue anyway": "Continuer quand même",
    "Try another link": "Essayer un autre lien",
    "Couldn't read this file": "Impossible de lire ce fichier",
    "Many recipe pages put the recipe card at the bottom, after the article. If the PDF stops before it, there's no recipe in it to read.":
      "Beaucoup de pages de recettes placent la fiche recette en bas, après l'article. Si le PDF s'arrête avant, il ne contient pas de recette à lire.",
    "If the recipe takes several screenshots, add them together with Combine multiple.":
      "Si la recette tient sur plusieurs captures d'écran, ajoutez-les ensemble avec Combiner plusieurs.",
    "Check that the screenshots cover the whole recipe card, ingredients and steps.":
      "Vérifiez que les captures d'écran couvrent toute la fiche recette, ingrédients et étapes.",
    "Other ways to add it": "Autres façons de l'ajouter",
    "Screenshots of the recipe card: the app reads them with text recognition. Use Combine multiple if it takes more than one.":
      "Des captures d'écran de la fiche recette : l'application les lit par reconnaissance de texte. Utilisez Combiner plusieurs s'il en faut plus d'une.",
    "A PDF of the page, if it came from a website: text in a PDF reads more reliably than a screenshot.":
      "Un PDF de la page, si elle vient d'un site Web : le texte d'un PDF se lit de façon plus fiable qu'une capture d'écran.",
    "Enter it by hand.": "La saisir à la main.",
    "Add from screenshots": "Ajouter à partir de captures d'écran",
    "Enter manually": "Saisir à la main",
    "Reading PDF (this can take a moment for scanned pages)...": "Lecture du PDF (les pages numérisées peuvent prendre un moment)…",
    "Could not process PDF.": "Impossible de traiter le PDF.",
    "PDF file": "Fichier PDF",
    "Text-based PDFs are read directly; scanned pages fall back to OCR automatically.":
      "Les PDF contenant du texte sont lus directement; les pages numérisées passent automatiquement par la reconnaissance de texte.",
    "Process PDF": "Traiter le PDF",
    "That's {1} PDFs; the limit is 20 per batch. Send the rest separately.":
      "Ça fait {1} PDF; la limite est de 20 par lot. Envoyez le reste séparément.",
    "Processing {1} PDF(s)...": "Traitement de {1} PDF…",
    "PDF files": "Fichiers PDF",
    "Each is saved directly using auto-detected fields.": "Chacun est enregistré directement avec les champs détectés automatiquement.",
    "Add from screenshot / photo": "Ajouter à partir d'une capture d'écran ou d'une photo",
    "Reading image with OCR...": "Lecture de l'image par reconnaissance de texte…",
    "Could not process image.": "Impossible de traiter l'image.",
    "Screenshot or photo": "Capture d'écran ou photo",
    "Best for rendered/screenshot text. Handwriting recognizes poorly — use Manual entry for handwritten cards instead.":
      "Idéal pour le texte affiché à l'écran. L'écriture manuscrite est mal reconnue : utilisez plutôt la saisie manuelle pour les fiches écrites à la main.",
    "Process image": "Traiter l'image",
    "Reading {1} image(s) with OCR...": "Lecture de {1} image(s) par reconnaissance de texte…",
    "Could not process images.": "Impossible de traiter les images.",
    "Screenshots (select all, in reading order)": "Captures d'écran (sélectionnez-les toutes, dans l'ordre de lecture)",
    "For a recipe that spans multiple screenshots — each is read with OCR and combined into one recipe. Most file pickers preserve the order you select files in; the first image becomes the showcase photo (changeable afterward).":
      "Pour une recette répartie sur plusieurs captures d'écran : chacune est lue par reconnaissance de texte, puis combinée en une seule recette. La plupart des sélecteurs de fichiers gardent l'ordre de sélection; la première image devient la photo principale (modifiable ensuite).",
    "Process & combine": "Traiter et combiner",
    "Review & confirm": "Vérifier et confirmer",
    "OCR quality was low on this input — please check the fields carefully.":
      "La qualité de la reconnaissance de texte était faible : vérifiez attentivement les champs.",
    "OCR confidence: {1}%": "Fiabilité de la reconnaissance : {1} %",
    "Showcase image (auto-detected)": "Photo principale (détectée automatiquement)",
    "Don't use this image": "Ne pas utiliser cette image",
    "Extracted text (source)": "Texte extrait (source)",
    "Auto-suggested from keywords — edit freely.": "Suggérées automatiquement à partir de mots-clés; modifiez librement.",
    "Real-world cook time (optional)": "Temps de cuisson réel (facultatif)",
    "How long it actually took you — separate from any time listed by the source. Leave blank to skip.":
      "Le temps que ça vous a réellement pris, indépendamment du temps indiqué par la source. Laissez vide pour passer.",
    "Save recipe": "Enregistrer la recette",
    "Saving recipe...": "Enregistrement de la recette…",
    "Could not save recipe.": "Impossible d'enregistrer la recette.",
    "Recipe saved.": "Recette enregistrée.",
    "Manual / handwritten entry": "Saisie manuelle ou manuscrite",
    "One ingredient per line": "Un ingrédient par ligne",
    "One step per line": "Une étape par ligne",
    "e.g. dinner, stovetop, beef": "p. ex. souper, cuisinière, bœuf",
    "The photo couldn't be uploaded ({1}). Nothing was saved; remove the photo or try another.":
      "La photo n'a pas pu être envoyée ({1}). Rien n'a été enregistré; retirez la photo ou essayez-en une autre.",
    "Attach a photo of the card (optional)": "Joindre une photo de la fiche (facultatif)",

    // Settings
    "Language": "Langue",
    "English": "English",
    "Français": "Français",
    "Theme": "Thème",
    "Light": "Clair",
    "Dark": "Sombre",
    "System": "Système",
    "Dark mode uses true black, not a gray substitute.": "Le mode sombre utilise un vrai noir, pas un gris.",
    "Screen": "Écran",
    "Keep screen awake when viewing a recipe": "Garder l'écran allumé pendant la consultation d'une recette",
    "Applies automatically to every recipe you open. You can still toggle it per recipe. Released when you close the recipe.":
      "S'applique automatiquement à chaque recette ouverte. Vous pouvez quand même l'activer ou le désactiver par recette. Désactivé à la fermeture de la recette.",
    "Text size": "Taille du texte",
    "Decrease text size": "Réduire la taille du texte",
    "Increase text size": "Augmenter la taille du texte",
    "Recipe list": "Liste des recettes",
    "Show photos on recipe cards": "Afficher les photos sur les fiches de recettes",
    "Off shows a compact list of titles. Remembered on this device.":
      "Désactivé, la liste n'affiche que les titres. Mémorisé sur cet appareil.",
    "Tags": "Étiquettes",
    "Add suggested tags to all recipes": "Ajouter les étiquettes suggérées à toutes les recettes",
    "Runs the automatic tagger over every recipe and adds any suggested tag a recipe doesn't already have. Nothing is removed. You'll see what would be added before anything changes.":
      "Analyse chaque recette avec l'étiquetage automatique et ajoute les étiquettes suggérées qu'elle n'a pas déjà. Rien n'est retiré. Vous verrez ce qui serait ajouté avant toute modification.",
    "Manage tag groups and keywords": "Gérer les groupes d'étiquettes et les mots-clés",
    "Add groups (like Cuisine) and tags, and the words that make the tagger suggest a tag. Built-in tags already know their usual words (Chicken knows chicken, poultry); keywords here are extra.":
      "Ajoutez des groupes (comme Cuisine du monde) et des étiquettes, ainsi que les mots qui déclenchent la suggestion d'une étiquette. Les étiquettes intégrées connaissent déjà leurs mots habituels (Poulet reconnaît poulet, volaille); les mots-clés ajoutés ici s'y ajoutent.",
    "Email ingest": "Réception par courriel",
    "Configure email ingest": "Configurer la réception par courriel",
    "Optional. Lets you email recipes to a dedicated inbox, which Open the Pantry scans once daily.":
      "Facultatif. Permet d'envoyer des recettes par courriel à une boîte dédiée, qu'Open the Pantry vérifie une fois par jour.",
    "Backup & export": "Sauvegarde et exportation",
    "Full backup": "Sauvegarde complète",
    "The database plus every photo and uploaded PDF, as a .zip. This is the one you restore from — it puts the app back exactly as it is now. Instructions are inside.":
      "La base de données, toutes les photos et tous les PDF envoyés, dans un fichier .zip. C'est celle qui sert à restaurer : l'application revient exactement à son état actuel. Les instructions sont incluses.",
    "Recipes as PDFs": "Recettes en PDF",
    "One PDF per recipe in a .zip, readable anywhere without this app.":
      "Un PDF par recette dans un fichier .zip, lisible partout sans cette application.",
    "Cannot": "Ne peut pas",
    "be restored from. Large libraries take a while to build.": "servir à une restauration. Les grandes bibliothèques prennent du temps à générer.",
    "Include my notes in the PDFs": "Inclure mes notes dans les PDF",
    "Building a PDF of every recipe. This can take a while.": "Création d'un PDF pour chaque recette. Ça peut prendre un moment.",
    "Preparing your backup…": "Préparation de votre sauvegarde…",
    "The server couldn't build the file (error {1}). Nothing was downloaded.":
      "Le serveur n'a pas pu créer le fichier (erreur {1}). Rien n'a été téléchargé.",
    "Ready: {1}. Check your downloads.": "Prêt : {1}. Vérifiez vos téléchargements.",
    "Couldn't reach Open the Pantry, so nothing was downloaded. Check your connection and try again.":
      "Impossible de joindre Open the Pantry; rien n'a été téléchargé. Vérifiez votre connexion et réessayez.",
    "{1} recipe(s), {2} uploaded file(s) ({3} total).": "{1} recette(s), {2} fichier(s) envoyé(s) ({3} au total).",

    // Restore instructions (static page text, split around <code> elements)
    "How to restore a backup or move to another drive": "Restaurer une sauvegarde ou déplacer vers un autre disque",
    "Your library lives in one folder on the server: the": "Votre bibliothèque se trouve dans un seul dossier sur le serveur : le",
    "left side": "côté gauche",
    "of the": "de la ligne",
    "line in": "dans",
    "unless you changed it).": "à moins que vous l'ayez modifié).",
    "Move to another drive": "Déplacer vers un autre disque",
    "Stop the app:": "Arrêtez l'application :",
    "Copy the whole folder to the new location, e.g.": "Copiez tout le dossier au nouvel emplacement, p. ex.",
    "In": "Dans",
    ", change only the left side of the volume line:": ", modifiez seulement le côté gauche de la ligne du volume :",
    "Start it:": "Démarrez-la :",
    ", and check your recipes are there before deleting the old folder.":
      ", et vérifiez que vos recettes y sont avant de supprimer l'ancien dossier.",
    "Restore a full backup": "Restaurer une sauvegarde complète",
    "Unzip the backup into the data folder so": "Décompressez la sauvegarde dans le dossier de données pour que",
    "and": "et",
    "are directly inside it:": "s'y trouvent directement :",
    "Restoring replaces everything currently in the app. Always stop the app first: replacing the database while it runs can corrupt it.":
      "La restauration remplace tout le contenu actuel de l'application. Arrêtez toujours l'application d'abord : remplacer la base de données pendant qu'elle fonctionne peut la corrompre.",
    "The backup doesn't include the email encryption key. If you use email ingest, copy":
      "La sauvegarde n'inclut pas la clé de chiffrement du courriel. Si vous utilisez la réception par courriel, copiez aussi",
    "from the old folder too, or set up encryption again and re-enter the password.":
      "depuis l'ancien dossier, ou configurez de nouveau le chiffrement et entrez le mot de passe à nouveau.",

    // Email settings
    "Checking the inbox…": "Vérification de la boîte de réception…",
    "The scan failed (error {1}).": "La vérification a échoué (erreur {1}).",
    "No new recipe emails.": "Aucun nouveau courriel de recette.",
    "Couldn't reach Open the Pantry to run the scan. Check your connection and try again.":
      "Impossible de joindre Open the Pantry pour lancer la vérification. Vérifiez votre connexion et réessayez.",
    "Could not load email settings.": "Impossible de charger les paramètres de courriel.",
    "Encryption key is invalid": "La clé de chiffrement est invalide",
    "RECIPE_APP_ENCRYPTION_KEY is set in your compose file, but its value isn't a valid key. Fix it or delete that line, then restart the container.":
      "RECIPE_APP_ENCRYPTION_KEY est défini dans votre fichier compose, mais sa valeur n'est pas une clé valide. Corrigez-la ou supprimez cette ligne, puis redémarrez le conteneur.",
    "Step 1: set up encryption": "Étape 1 : configurer le chiffrement",
    "Encryption key file is damaged": "Le fichier de clé de chiffrement est endommagé",
    "encryption.key in the data folder doesn't contain a valid key (it may be empty or damaged). Put back the copy it came from. If that's not possible, delete the file and reopen this panel to set up encryption again; the email password then has to be entered again.":
      "encryption.key, dans le dossier de données, ne contient pas de clé valide (le fichier est peut-être vide ou endommagé). Remettez la copie d'origine. Si c'est impossible, supprimez le fichier et rouvrez ce panneau pour configurer de nouveau le chiffrement; il faudra ensuite entrer à nouveau le mot de passe du courriel.",
    "Your email password is stored encrypted, so a key has to exist first. This creates one in the app's data folder (encryption.key). If you ever move the data folder, the key goes with it. The backup zip leaves it out, so after restoring from a backup on a new install you re-enter the password.":
      "Le mot de passe du courriel est conservé chiffré; une clé doit donc exister d'abord. Ceci en crée une dans le dossier de données de l'application (encryption.key). Si vous déplacez le dossier de données, la clé suit. La sauvegarde .zip ne l'inclut pas : après une restauration sur une nouvelle installation, entrez le mot de passe à nouveau.",
    "Set up encryption": "Configurer le chiffrement",
    "Creating key…": "Création de la clé…",
    "Encryption is set up. You can now enter the email password.": "Le chiffrement est configuré. Vous pouvez maintenant entrer le mot de passe du courriel.",
    "Couldn't create the key (error {1}).": "Impossible de créer la clé (erreur {1}).",
    "Set up encryption first (above)": "Configurez d'abord le chiffrement (ci-dessus)",
    "Saved — leave blank to keep": "Enregistré : laissez vide pour le conserver",
    "App password": "Mot de passe d'application",
    "you@example.com, @family.example": "vous@exemple.com, @famille.exemple",
    "Enable daily inbox scan": "Activer la vérification quotidienne de la boîte de réception",
    "the last scan": "la dernière vérification",
    "Problem during the scan at {1}": "Problème lors de la vérification du {1}",
    "Send test email checks both halves; this clears once a scan or test goes through.":
      "« Envoyer un courriel de test » vérifie la réception et l'envoi; ce message disparaît dès qu'une vérification ou un test réussit.",
    "In the server's time zone ({1}). If that isn't yours, set TZ in docker-compose.yml.":
      "Selon le fuseau horaire du serveur ({1}). Si ce n'est pas le vôtre, définissez TZ dans docker-compose.yml.",
    "In the server's time zone. If that isn't yours, set TZ in docker-compose.yml.":
      "Selon le fuseau horaire du serveur. Si ce n'est pas le vôtre, définissez TZ dans docker-compose.yml.",
    "Last scan: {1}": "Dernière vérification : {1}",
    "{n} scan result waiting to be emailed.": "{n} résultat de vérification en attente d'envoi par courriel.",
    "{n} scan results waiting to be emailed.": "{n} résultats de vérification en attente d'envoi par courriel.",
    "Saved. The stored password was cleared because the server or username changed — enter it again if you still need email ingest.":
      "Enregistré. Le mot de passe conservé a été effacé parce que le serveur ou le nom d'utilisateur a changé; entrez-le de nouveau si vous avez encore besoin de la réception par courriel.",
    "Email settings saved.": "Paramètres de courriel enregistrés.",
    "Could not save.": "Impossible d'enregistrer.",
    "Send test email": "Envoyer un courriel de test",
    "Testing...": "Test en cours…",
    "Test failed.": "Le test a échoué.",
    "Scan inbox now": "Vérifier la boîte maintenant",
    "Clear password": "Effacer le mot de passe",
    "Remove the stored email password? This also disables email ingest.":
      "Retirer le mot de passe du courriel conservé? Ceci désactive aussi la réception par courriel.",
    "The stored password": "Le mot de passe conservé",
    "Stored email password removed.": "Mot de passe du courriel retiré.",
    "IMAP host (reading)": "Serveur IMAP (lecture)",
    "IMAP port": "Port IMAP",
    "SMTP host (notifications)": "Serveur SMTP (envoi)",
    "SMTP port": "Port SMTP",
    "Username": "Nom d'utilisateur",
    "Password": "Mot de passe",
    "Settings password": "Mot de passe des paramètres",
    "Change settings password": "Changer le mot de passe des paramètres",
    "Email ingest, HTTPS, Logs and Email PDF use your email account or DNS login, so they need a password. Everything else stays open.":
      "La réception par courriel, HTTPS, le journal et l'envoi du PDF par courriel utilisent votre compte de courriel ou votre accès DNS; ils demandent donc un mot de passe. Tout le reste demeure ouvert.",
    "Email ingest, HTTPS, Logs and Email PDF use your email account or DNS login, so they're behind a password. Create it now. Everything else in the app stays open.":
      "La réception par courriel, HTTPS, le journal et l'envoi du PDF par courriel utilisent votre compte de courriel ou votre accès DNS; ils sont donc protégés par un mot de passe. Créez-le maintenant. Tout le reste de l'application demeure ouvert.",
    "Email ingest, HTTPS, Logs and Email PDF are locked. Enter the settings password to unlock them on this device for 15 minutes.":
      "La réception par courriel, HTTPS, le journal et l'envoi du PDF par courriel sont verrouillés. Entrez le mot de passe des paramètres pour les déverrouiller sur cet appareil pendant 15 minutes.",
    "Every other device that's unlocked gets locked.": "Tous les autres appareils déverrouillés seront verrouillés.",
    "At least 8 characters. Forgot it? Delete admin-password.json in the app's data folder; you'll be asked to create a new one.":
      "Au moins 8 caractères. Oublié? Supprimez admin-password.json dans le dossier de données de l'application; on vous demandera d'en créer un nouveau.",
    "Current password": "Mot de passe actuel",
    "New password": "Nouveau mot de passe",
    "Type it again": "Entrez-le de nouveau",
    "Create password": "Créer le mot de passe",
    "Unlock": "Déverrouiller",
    "Change password": "Changer le mot de passe",
    "Lock now": "Verrouiller maintenant",
    "Locked.": "Verrouillé.",
    "Settings password changed.": "Mot de passe des paramètres changé.",
    "The two passwords don't match.": "Les deux mots de passe ne correspondent pas.",
    "The password must be at least 8 characters.": "Le mot de passe doit compter au moins 8 caractères.",
    "Not set yet. You'll create it the first time you open one of them.":
      "Pas encore créé. Vous le créerez la première fois que vous ouvrirez l'un d'eux.",
    "Unlocked on this device for {n} more minute.": "Déverrouillé sur cet appareil pour encore {n} minute.",
    "Unlocked on this device for {n} more minutes.": "Déverrouillé sur cet appareil pour encore {n} minutes.",
    "Locked again after 15 minutes. Close and reopen this section to enter the password.":
      "Verrouillé de nouveau après 15 minutes. Fermez puis rouvrez cette section pour entrer le mot de passe.",
    "Send notifications to": "Envoyer les avis à",
    "Subject keyword": "Mot-clé de l'objet",
    "Only accept email from": "Accepter les courriels seulement de",
    "Daily scan hour (0-23)": "Heure de la vérification quotidienne (0-23)",
    "Notification cooldown (minutes)": "Délai entre les avis (minutes)",
    "e.g. imap.gmail.com": "p. ex. imap.gmail.com",
    "993 connects with TLS from the start (implicit TLS). Any other port starts unencrypted and upgrades via STARTTLS -- that's how port 143 is normally used.":
      "Le port 993 se connecte en TLS dès le départ (TLS implicite). Tout autre port commence sans chiffrement et passe au chiffrement par STARTTLS; c'est l'usage habituel du port 143.",
    "e.g. smtp.gmail.com": "p. ex. smtp.gmail.com",
    "465 connects with TLS from the start (implicit TLS). Any other port -- typically 587 -- starts unencrypted and upgrades via STARTTLS. Guessed from the port number above; there's no separate setting for it.":
      "Le port 465 se connecte en TLS dès le départ (TLS implicite). Tout autre port (habituellement 587) commence sans chiffrement et passe au chiffrement par STARTTLS. Déduit du numéro de port ci-dessus; il n'y a pas de réglage distinct.",
    "Used for both IMAP and SMTP.": "Utilisé pour IMAP et SMTP.",
    "Stored encrypted. Use an app-specific password, not your main account password.":
      "Conservé chiffré. Utilisez un mot de passe propre à l'application, pas le mot de passe principal du compte.",
    "Only emails whose subject contains this are considered. Everything else is ignored.":
      "Seuls les courriels dont l'objet contient ce mot sont traités. Tous les autres sont ignorés.",
    "Addresses, or a domain (example.com) for everyone there, separated by commas. Leave empty to accept anyone who knows the address and keyword. Recommended: list the addresses you send from.":
      "Des adresses, ou un domaine (exemple.com) pour tout le monde à ce domaine, séparés par des virgules. Laissez vide pour accepter quiconque connaît l'adresse et le mot-clé. Recommandé : indiquez les adresses d'où vous envoyez.",
    "Results within this window are batched into one email.": "Les résultats dans cet intervalle sont regroupés en un seul courriel.",

    // Settings → HTTPS
    "HTTPS": "HTTPS",
    "Configure HTTPS": "Configurer HTTPS",
    "Optional. A secure https:// address on your network, with a free Let's Encrypt certificate. Needed for keeping the screen awake.":
      "Facultatif. Une adresse sécurisée https:// sur votre réseau, avec un certificat Let's Encrypt gratuit. Nécessaire pour garder l'écran allumé.",
    "Could not load the HTTPS settings.": "Impossible de charger les paramètres HTTPS.",
    "Setting up HTTPS…": "Configuration de HTTPS…",
    "On: {1}": "Activé : {1}",
    "Step 1: add your cPanel login and the name to use": "Étape 1 : ajouter vos identifiants cPanel et le nom à utiliser",
    "PANTRY_DOMAIN is the one name this page can set up. It's kept with the login so that nobody using the app can have it create other names in your domain.":
      "PANTRY_DOMAIN est le seul nom que cette page peut configurer. Il est gardé avec les identifiants pour que personne qui utilise l'application ne puisse lui faire créer d'autres noms dans votre domaine.",
    "The app creates its DNS records through your domain's cPanel. For safety the login isn't entered here: put it in a file named .env next to docker-compose.yml, then restart the app (docker compose up -d).":
      "L'application crée ses enregistrements DNS au moyen du cPanel de votre domaine. Par prudence, les identifiants ne s'entrent pas ici : placez-les dans un fichier nommé .env à côté de docker-compose.yml, puis redémarrez l'application (docker compose up -d).",
    "In cPanel: Security → Manage API Tokens → Create. Then add these lines to .env:":
      "Dans cPanel : Sécurité → Gérer les jetons API → Créer. Ajoutez ensuite ces lignes à .env :",
    "Missing now: {1}": "Manquant : {1}",
    "cPanel login found ({1}).": "Identifiants cPanel trouvés ({1}).",
    "Address": "Adresse",
    "Set by PANTRY_DOMAIN in .env; the only name this page can set up. If it isn't in use yet, the app creates its DNS record.":
      "Défini par PANTRY_DOMAIN dans .env; le seul nom que cette page peut configurer. S'il n'est pas encore utilisé, l'application crée son enregistrement DNS.",
    "This server's address on your network": "Adresse de ce serveur sur votre réseau",
    "Where the name will point: an address on your home network or VPN. Filled in from the address you're using now, if it's a number.":
      "L'adresse vers laquelle le nom pointera : une adresse de votre réseau domestique ou de votre RPV. Remplie à partir de l'adresse que vous utilisez en ce moment, si c'est une adresse numérique.",
    "Email for Let's Encrypt": "Courriel pour Let's Encrypt",
    "Let's Encrypt writes here only if something is wrong with the certificate.":
      "Let's Encrypt n'écrit à cette adresse qu'en cas de problème avec le certificat.",
    "Port": "Port",
    "The port docker-compose.yml publishes for HTTPS (8443 unless you changed it). Only used for the link.":
      "Le port que docker-compose.yml publie pour HTTPS (8443, sauf si vous l'avez changé). Sert seulement au lien.",
    "This takes a minute or two. You can close Settings; it carries on.":
      "Ça prend une minute ou deux. Vous pouvez fermer les Paramètres; ça continue.",
    "HTTPS is on:": "HTTPS est activé :",
    "Certificate valid until {1}; it renews by itself.": "Certificat valide jusqu'au {1}; il se renouvelle tout seul.",
    "Open the secure address and add it to your Home Screen from there. Language, theme and similar settings are kept per address, so set them once more.":
      "Ouvrez l'adresse sécurisée et ajoutez-la à votre écran d'accueil à partir de là. La langue, le thème et les réglages semblables sont propres à chaque adresse; réglez-les donc une fois de plus.",
    "Last attempt failed:": "La dernière tentative a échoué :",
    "Set up HTTPS": "Configurer HTTPS",
    "Apply and renew": "Appliquer et renouveler",
    "Turn off HTTPS": "Désactiver HTTPS",
    "Turn off HTTPS? The https:// address stops working; the usual address keeps working. The DNS record stays.":
      "Désactiver HTTPS? L'adresse https:// cessera de fonctionner; l'adresse habituelle continue de fonctionner. L'enregistrement DNS reste.",
    "HTTPS turned off.": "HTTPS désactivé.",
    "HTTPS is ready.": "HTTPS est prêt.",

    // Settings → Logs
    "Logs": "Journal",
    "View log": "Voir le journal",
    "Hide log": "Masquer le journal",
    "Clear log": "Vider le journal",
    "What the app recorded: recipe imports, email scans and errors. Useful when something didn't work. Kept to about 4 MB; the oldest entries drop off by themselves.":
      "Ce que l'application a consigné : importations de recettes, vérifications de courriel et erreurs. Utile quand quelque chose n'a pas fonctionné. Limité à environ 4 Mo; les entrées les plus anciennes disparaissent d'elles-mêmes.",
    "Everything": "Tout",
    "Warnings and errors": "Avertissements et erreurs",
    "Refresh": "Actualiser",
    "Log": "Journal",
    "Nothing logged yet.": "Rien de consigné pour l'instant.",
    "{n} entry shown. The whole log is {1}; Download has all of it.":
      "{n} entrée affichée. Le journal complet fait {1}; Télécharger le contient en entier.",
    "Last {n} entries shown. The whole log is {1}; Download has all of it.":
      "Les {n} dernières entrées sont affichées. Le journal complet fait {1}; Télécharger le contient en entier.",
    "No warnings or errors.": "Aucun avertissement ni erreur.",
    "Could not load the log.": "Impossible de charger le journal.",
    "Clear the log? Everything recorded so far is deleted. Download it first if you might need it.":
      "Vider le journal? Tout ce qui a été consigné jusqu'ici sera supprimé. Téléchargez-le d'abord si vous pourriez en avoir besoin.",
    "The log": "Le journal",
    "Log cleared.": "Journal vidé.",

    // Tag scan (Settings → Tags)
    "Show each recipe": "Afficher chaque recette",
    "Checking every recipe...": "Vérification de chaque recette…",
    "Couldn't check the recipes ({1}).": "Impossible de vérifier les recettes ({1}).",
    "All {1} recipes checked already have their suggested tags": "Les {1} recettes vérifiées ont déjà leurs étiquettes suggérées",
    "Nothing to add.": "Rien à ajouter.",
    "All suggested tags for {1}": "Toutes les étiquettes suggérées pour {1}",
    "Skip in future scans": "Exclure à l'avenir",
    "Leave {1} out of future tag scans": "Exclure {1} des prochaines analyses d'étiquettes",
    "Couldn't skip that recipe.": "Impossible d'exclure cette recette.",
    "{1} will be left out of tag scans.": "{1} sera exclue des analyses d'étiquettes.",
    "Add tags": "Ajouter les étiquettes",
    "Added {1} tag(s) to {2} recipe(s). Remove any you don't want from each recipe's page.":
      "{1} étiquette(s) ajoutée(s) à {2} recette(s). Retirez celles que vous ne voulez pas à partir de la page de chaque recette.",
    "Added {1} tags.": "{1} étiquettes ajoutées.",
    "Couldn't add the tags ({1}). Nothing was changed.": "Impossible d'ajouter les étiquettes ({1}). Rien n'a été modifié.",
    "Adds {1} of {2} suggested tags to {n} recipe. Untick any that don't apply.":
      "Ajoute {1} des {2} étiquettes suggérées à {n} recette. Décochez celles qui ne s'appliquent pas.",
    "Adds {1} of {2} suggested tags to {n} recipes. Untick any that don't apply.":
      "Ajoute {1} des {2} étiquettes suggérées à {n} recettes. Décochez celles qui ne s'appliquent pas.",
    "{n} recipe is skipped (each recipe's edit screen can include it again, or suggest tags for it).":
      "{n} recette est exclue (l'écran de modification de chaque recette permet de l'inclure de nouveau ou de lui suggérer des étiquettes).",
    "{n} recipes are skipped (each recipe's edit screen can include it again, or suggest tags for it).":
      "{n} recettes sont exclues (l'écran de modification de chaque recette permet de les inclure de nouveau ou de leur suggérer des étiquettes).",

    // Tag groups (Settings → Tags)
    "extra words, comma separated": "mots supplémentaires, séparés par des virgules",
    "Keywords for {1}": "Mots-clés pour {1}",
    "{1} rules out Vegetarian": "{1} exclut Végétarien",
    "Saved keywords for {1}.": "Mots-clés enregistrés pour {1}.",
    "Delete tag {1}": "Supprimer l'étiquette {1}",
    "Tap again to delete": "Touchez de nouveau pour supprimer",
    "Deleted {1} (removed from {2} recipes).": "{1} supprimée (retirée de {2} recettes).",
    "Deleted {1}.": "{1} supprimée.",
    "Means it's not vegetarian": "Signifie que ce n'est pas végétarien",
    "New tag name": "Nom de la nouvelle étiquette",
    "New tag in {1}": "Nouvelle étiquette dans {1}",
    "keywords, comma separated": "mots-clés, séparés par des virgules",
    "Keywords for the new tag": "Mots-clés de la nouvelle étiquette",
    "Add tag": "Ajouter l'étiquette",
    "Added {1} to {2}.": "{1} ajoutée à {2}.",
    "Delete group": "Supprimer le groupe",
    "Delete group and its {n} tag?": "Supprimer le groupe et son étiquette ({n})?",
    "Delete group and its {n} tags?": "Supprimer le groupe et ses {n} étiquettes?",
    "New group, e.g. Cuisine": "Nouveau groupe",
    "New group name": "Nom du nouveau groupe",
    "Add group": "Ajouter le groupe",
    "Added the {1} group. Add tags to it below.": "Groupe {1} ajouté. Ajoutez-y des étiquettes ci-dessous.",
  };

  // Built-in tags: the stored name stays English (the tagger, filters and
  // exports key on it); only the label shown changes.
  const FR_TAGS = {
    "Breakfast": "Déjeuner", "Brunch": "Brunch", "Lunch": "Dîner", "Dinner": "Souper",
    "Snack": "Collation", "Cocktails": "Cocktails",
    "Oven": "Four", "Stovetop": "Cuisinière", "Grill": "Gril", "Barbecue/Smoker": "Barbecue/Fumoir",
    "Deep Fryer": "Friteuse", "Pressure Cooker": "Autocuiseur", "Griddle": "Plaque chauffante",
    "Shaken": "Agité", "Stirred": "Remué", "Built": "Monté au verre", "Blended": "Mélangé",
    "Chicken": "Poulet", "Pork": "Porc", "Beef": "Bœuf", "Fish": "Poisson",
    "Vegetarian": "Végétarien", "Game": "Gibier",
  };
  const FR_GROUPS = {
    meal_type: "Type de repas", cooking_style: "Mode de cuisson",
    main_ingredient: "Ingrédient principal", custom: "Personnalisées",
  };
  const BUILTIN_CATEGORY = {
    meal_type: ["Breakfast", "Brunch", "Lunch", "Dinner", "Snack", "Cocktails"],
    cooking_style: ["Oven", "Stovetop", "Grill", "Barbecue/Smoker", "Deep Fryer", "Pressure Cooker", "Griddle",
      "Shaken", "Stirred", "Built", "Blended"],
    main_ingredient: ["Chicken", "Pork", "Beef", "Fish", "Vegetarian", "Game"],
  };

  // Quebec typography: a non-breaking space before ":" and inside « ».
  for (const k of Object.keys(FR)) {
    FR[k] = FR[k].replace(/ :/g, " :").replace(/« /g, "« ").replace(/ »/g, " »");
  }

  const catalog = lang === "fr" ? FR : null;

  function fill(text, vars) {
    if (!vars) return text;
    return text.replace(/\{(\w+)\}/g, (m, name) => (name in vars ? String(vars[name]) : m));
  }

  function tx(key, vars) {
    const text = (catalog && Object.prototype.hasOwnProperty.call(catalog, key)) ? catalog[key] : key;
    return fill(text, vars);
  }

  // French treats 0 and 1 as singular; English only 1.
  function txn(n, oneKey, manyKey, vars) {
    const one = lang === "fr" ? Math.abs(n) < 2 : n === 1;
    return tx(one ? oneKey : manyKey, { ...(vars || {}), n });
  }

  // A tag object or a bare name. Only built-in tags are translated (and
  // only in their own group, so a user's custom "Game" stays as typed).
  function tagLabel(tag) {
    if (tag == null) return "";
    const name = typeof tag === "string" ? tag : tag.name;
    const category = typeof tag === "string" ? null : tag.category;
    if (lang !== "fr" || !Object.prototype.hasOwnProperty.call(FR_TAGS, name)) return name;
    if (category && !(BUILTIN_CATEGORY[category] || []).includes(name)) return name;
    return FR_TAGS[name];
  }

  function groupLabel(key, label) {
    if (lang === "fr" && Object.prototype.hasOwnProperty.call(FR_GROUPS, key)) return FR_GROUPS[key];
    return label;
  }

  // Rating values are stored lowercase English ("quick", "medium").
  function valueLabel(v) {
    const s = String(v);
    return tx(s.charAt(0).toUpperCase() + s.slice(1));
  }

  // English uses the device's own English variant (en-US, en-GB...) for
  // date order. A malformed tag (some systems report "en-US@posix") throws
  // in Intl, so it is checked first.
  function locale() {
    if (lang === "fr") return "fr-CA";
    const nav = String(navigator.language || "");
    try {
      if (nav.toLowerCase().startsWith("en") && Intl.DateTimeFormat.supportedLocalesOf([nav]).length) return nav;
    } catch { /* invalid tag */ }
    return "en-CA";
  }

  function fmtDateTime(value) {
    const d = value instanceof Date ? value : new Date(value);
    if (Number.isNaN(d.getTime())) return String(value);
    try {
      return d.toLocaleString(locale(), { dateStyle: "medium", timeStyle: "short" });
    } catch {
      return d.toLocaleString();
    }
  }

  // A duration in minutes, as on the time filter: "1h 20m" / "1 h 20".
  function durationLabel(total) {
    const days = Math.floor(total / 1440), hours = Math.floor((total % 1440) / 60), mins = total % 60;
    if (lang === "fr") {
      if (days) return hours ? `${days} j ${hours} h` : `${days} j`;
      if (hours) return mins ? `${hours} h ${String(mins).padStart(2, "0")}` : `${hours} h`;
      return `${mins} min`;
    }
    if (days) return hours ? `${days}d ${hours}h` : `${days}d`;
    if (hours) return mins ? `${hours}h ${mins}m` : `${hours}h`;
    return `${mins}m`;
  }

  // Static page text: translate text nodes and a few attributes in place.
  // <code>, <script> and <style> are left alone, as is anything inside
  // [data-no-tx].
  const ATTRS = ["placeholder", "aria-label", "title", "alt"];
  function translateStatic(root) {
    document.documentElement.lang = lang === "fr" ? "fr-CA" : "en";
    if (!catalog) return;
    const walker = document.createTreeWalker(root || document.body, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        const p = node.parentElement;
        if (!p || p.closest("code, script, style, [data-no-tx]")) return NodeFilter.FILTER_REJECT;
        return node.nodeValue.trim() ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
      },
    });
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const node of nodes) {
      const raw = node.nodeValue;
      const key = raw.trim().replace(/\s+/g, " ");
      if (Object.prototype.hasOwnProperty.call(catalog, key)) {
        const lead = raw.match(/^\s*/)[0];
        const trail = raw.match(/\s*$/)[0];
        node.nodeValue = lead + catalog[key] + trail;
      }
    }
    for (const a of ATTRS) {
      (root || document).querySelectorAll(`[${a}]`).forEach((elm) => {
        if (elm.closest("[data-no-tx]")) return;
        const v = elm.getAttribute(a).trim();
        if (Object.prototype.hasOwnProperty.call(catalog, v)) elm.setAttribute(a, catalog[v]);
      });
    }
  }

  window.OTP_I18N = {
    lang, setLang, tx, txn, tagLabel, groupLabel, valueLabel, fmtDateTime, durationLabel, translateStatic,
    // For tests and for matching typed tag names back to stored ones.
    catalog: FR, frTags: FR_TAGS,
  };
})();
