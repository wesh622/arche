# Arche → Claude (connecteur MCP, lecture seule)

Petit serveur qui permet à Claude de lire ton espace Arche (Université de Lorraine) : cours, échéances, devoirs, notes, annonces, et le texte des PDF / DOCX / PPTX de cours.

**Lecture seule** : il est impossible pour Claude de rendre un devoir, poster un message ou modifier quoi que ce soit via ce serveur.

---

## 1. Récupérer ton jeton Arche

1. Arche → ton profil → **Préférences** → **Clefs de sécurité**
2. Ligne **« Moodle mobile web service »** → **Réinitialiser**
3. Copie le jeton affiché (il n'est montré qu'une fois). Garde-le pour l'étape 3.

> ⚠️ Si tu utilises l'app mobile Moodle, elle te demandera de te reconnecter. C'est normal.
> Ne colle jamais ce jeton dans un chat, ni dans un fichier sur GitHub.

## 2. Mettre le code sur GitHub

1. Sur github.com → **New repository** → nom `arche-mcp` → coche **Private** → Create.
2. **Add file → Upload files** → glisse tous les fichiers de ce dossier (`server.py`, `requirements.txt`, `railway.json`, `.python-version`, `.gitignore`, `README.md`) → Commit.

## 3. Déployer sur Railway

1. railway.com → **New Project** → **Deploy from GitHub repo** → choisis `arche-mcp`.
2. Onglet **Variables** du service → ajoute :

| Nom | Valeur |
|---|---|
| `MOODLE_TOKEN` | le jeton de l'étape 1 |
| `MCP_SECRET` | une longue chaîne aléatoire (voir dessous) |

   Pour `MCP_SECRET` : **au moins 32 caractères, uniquement lettres et chiffres** (pas de symboles). Un générateur de mots de passe avec les symboles désactivés fait l'affaire. Ne réutilise aucun mot de passe.

3. **Settings → Networking → Generate Domain**. Tu obtiens une adresse du type `arche-mcp-production.up.railway.app`.
4. Attends que le déploiement soit vert. En ouvrant `https://<ton-domaine>/` tu dois voir `ok`.

## 4. Ajouter le connecteur dans Claude

1. claude.ai → **Paramètres → Connecteurs → Ajouter un connecteur personnalisé**
2. Nom : `Arche`
3. URL :

```
https://<ton-domaine>/mcp/<ton MCP_SECRET>
```

4. Valide, puis active-le dans une conversation et demande : *« Quelles sont mes prochaines échéances sur Arche ? »*

---

## Ce que Claude peut faire

| Outil | Rôle |
|---|---|
| `whoami` | Vérifie que la connexion marche |
| `list_courses` | Liste tes cours (avec leur id) |
| `upcoming_deadlines` | Échéances des N prochains jours |
| `course_contents` | Plan d'un cours + liens des fichiers |
| `read_course_file` | Lit le texte d'un PDF / DOCX / PPTX / TXT du cours |
| `assignments` | Devoirs + dates limites + consignes |
| `submission_status` | Ton rendu pour un devoir (rendu ? noté ?) |
| `grades` | Notes globales ou détail d'un cours |
| `announcements` | Dernières annonces d'un cours |

## Sécurité — à savoir

- **L'URL du connecteur est ta clé.** Quiconque la possède peut lire ton Arche. Ne la partage pas.
- **Si tu penses qu'elle a fuité :** change `MCP_SECRET` dans Railway (l'ancienne URL cesse de marcher), puis mets à jour l'URL dans Claude.
- **Si le jeton Arche a fuité :** **Réinitialiser** sur la page Clefs de sécurité, puis remplace `MOODLE_TOKEN` dans Railway.
- Le serveur refuse de télécharger quoi que ce soit hors d'`arche.univ-lorraine.fr`, et n'appelle que des fonctions Moodle de lecture.
- Respecte la charte informatique de l'université et les règles sur l'IA pour les travaux notés.

## Dépannage

- **Le déploiement plante au démarrage :** regarde les logs Railway. Un message `ERREUR : …` indique quelle variable manque ou est mal formée.
- **`invalidtoken` dans les réponses :** le jeton est faux ou a été réinitialisé. Recopie-le dans Railway.
- **Un PDF renvoie « pas de texte extractible » :** c'est un scan (images). Télécharge-le et envoie-le directement à Claude dans le chat.
