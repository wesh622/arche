"""
Serveur MCP (lecture seule) pour Arche / Moodle — Université de Lorraine.

Variables d'environnement (à définir dans Railway) :
  MOODLE_TOKEN   -> ton jeton "Moodle mobile web service" (OBLIGATOIRE)
  MCP_SECRET     -> longue chaîne aléatoire, fait partie de l'URL secrète (OBLIGATOIRE)
  MOODLE_URL     -> par défaut https://arche.univ-lorraine.fr
  PORT           -> fourni automatiquement par Railway

URL du connecteur à mettre dans Claude :
  https://<ton-app>.up.railway.app/mcp/<MCP_SECRET>
"""

import io
import os
import re
import sys
import time
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse
from zoneinfo import ZoneInfo

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
MOODLE_URL = os.environ.get("MOODLE_URL", "https://arche.univ-lorraine.fr").rstrip("/")
MOODLE_TOKEN = os.environ.get("MOODLE_TOKEN", "").strip()
MCP_SECRET = os.environ.get("MCP_SECRET", "").strip()
PORT = int(os.environ.get("PORT", "8000"))

if not MOODLE_TOKEN:
    sys.exit("ERREUR : la variable MOODLE_TOKEN est vide.")
if len(MCP_SECRET) < 24 or not re.fullmatch(r"[A-Za-z0-9_-]+", MCP_SECRET):
    sys.exit("ERREUR : MCP_SECRET doit faire au moins 24 caractères (lettres, chiffres, - ou _).")

MOODLE_HOST = urlparse(MOODLE_URL).hostname
REST_ENDPOINT = f"{MOODLE_URL}/webservice/rest/server.php"
MAX_FILE_BYTES = 25 * 1024 * 1024      # 25 Mo max par fichier téléchargé
MAX_TEXT_CHARS = 60_000                # texte renvoyé à Claude par appel

# Seules des fonctions de LECTURE sont autorisées. Rien qui écrit/soumet.
ALLOWED_FUNCTIONS = {
    "core_webservice_get_site_info",
    "core_enrol_get_users_courses",
    "core_course_get_contents",
    "core_calendar_get_action_events_by_timesort",
    "mod_assign_get_assignments",
    "mod_assign_get_submission_status",
    "gradereport_overview_get_course_grades",
    "gradereport_user_get_grade_items",
    "mod_forum_get_forums_by_courses",
    "mod_forum_get_forum_discussions",
    "core_course_search_courses",
}

http = httpx.Client(timeout=40.0, follow_redirects=True)
_site_info_cache: dict = {}


# --------------------------------------------------------------------------- #
# Appels Moodle
# --------------------------------------------------------------------------- #
def _flatten(params: dict, prefix: str = "") -> dict:
    """Transforme {'courseids': [1, 2]} en {'courseids[0]': 1, 'courseids[1]': 2}."""
    out = {}
    for key, value in params.items():
        full = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, dict):
            out.update(_flatten(value, full))
        elif isinstance(value, (list, tuple)):
            out.update(_flatten({i: v for i, v in enumerate(value)}, full))
        elif isinstance(value, bool):
            out[full] = int(value)
        elif value is not None:
            out[full] = value
    return out


class MoodleError(Exception):
    pass


def call(function: str, **params):
    if function not in ALLOWED_FUNCTIONS:
        raise MoodleError(f"Fonction non autorisée : {function}")
    data = {"wstoken": MOODLE_TOKEN, "wsfunction": function, "moodlewsrestformat": "json"}
    data.update(_flatten(params))
    resp = http.post(REST_ENDPOINT, data=data)
    resp.raise_for_status()
    result = resp.json()
    if isinstance(result, dict) and result.get("exception"):
        raise MoodleError(f"{result.get('errorcode')}: {result.get('message')}")
    return result


def site_info() -> dict:
    if not _site_info_cache or time.time() - _site_info_cache.get("_t", 0) > 3600:
        info = call("core_webservice_get_site_info")
        info["_t"] = time.time()
        _site_info_cache.clear()
        _site_info_cache.update(info)
    return _site_info_cache


def user_id() -> int:
    return int(site_info()["userid"])


# --------------------------------------------------------------------------- #
# Utilitaires
# --------------------------------------------------------------------------- #
def strip_html(html: str | None) -> str:
    if not html:
        return ""
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>|</div>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


TZ = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Paris"))
JOURS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]


def fmt_ts(ts) -> str:
    if not ts:
        return "—"
    d = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(TZ)
    return f"{JOURS[d.weekday()]} {d:%d/%m/%Y %H:%M}"


def truncate(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[… tronqué : {len(text) - limit} caractères restants. Demande une page suivante avec offset={limit}.]"


def safe_file_url(url: str) -> str:
    """N'autorise que les fichiers hébergés sur Arche, et ajoute le jeton."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != MOODLE_HOST:
        raise MoodleError("Seuls les fichiers hébergés sur Arche peuvent être lus.")
    path = parsed.path
    if "/pluginfile.php" not in path:
        raise MoodleError("Ce lien n'est pas un fichier de cours Arche (pluginfile.php).")
    if "/webservice/pluginfile.php" not in path:
        path = path.replace("/pluginfile.php", "/webservice/pluginfile.php", 1)
    query = [(k, v) for k, v in parse_qsl(parsed.query) if k != "token"]
    query.append(("token", MOODLE_TOKEN))
    return urlunparse(parsed._replace(path=path, query=urlencode(query)))


def extract_text(content: bytes, filename: str, mimetype: str) -> str:
    name = filename.lower()
    if name.endswith(".pdf") or "pdf" in mimetype:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        pages = []
        for i, page in enumerate(reader.pages, 1):
            pages.append(f"--- Page {i} ---\n{(page.extract_text() or '').strip()}")
        return "\n\n".join(pages)
    if name.endswith(".docx"):
        import docx
        d = docx.Document(io.BytesIO(content))
        return "\n".join(p.text for p in d.paragraphs)
    if name.endswith(".pptx"):
        from pptx import Presentation
        prs = Presentation(io.BytesIO(content))
        slides = []
        for i, slide in enumerate(prs.slides, 1):
            texts = [sh.text_frame.text for sh in slide.shapes if sh.has_text_frame and sh.text_frame.text.strip()]
            slides.append(f"--- Diapo {i} ---\n" + "\n".join(texts))
        return "\n\n".join(slides)
    if name.endswith((".txt", ".md", ".csv", ".py", ".c", ".java", ".tex", ".json", ".sql", ".m", ".r")) or mimetype.startswith("text/"):
        return content.decode("utf-8", errors="replace")
    if name.endswith((".html", ".htm")):
        return strip_html(content.decode("utf-8", errors="replace"))
    raise MoodleError(f"Format non pris en charge pour la lecture : {filename} ({mimetype or 'type inconnu'}).")


# --------------------------------------------------------------------------- #
# Serveur MCP
# --------------------------------------------------------------------------- #
mcp = FastMCP(
    name="Arche (Université de Lorraine)",
    instructions=(
        "Accès en lecture seule à l'espace Arche (Moodle) de l'étudiant. "
        "Commence par list_courses pour obtenir les identifiants de cours. "
        "Utilise upcoming_deadlines pour les échéances, course_contents pour le contenu d'un cours, "
        "read_course_file pour lire un PDF/DOCX/PPTX dont l'URL vient de course_contents."
    ),
    host="0.0.0.0",
    port=PORT,
    streamable_http_path=f"/mcp/{MCP_SECRET}",
    stateless_http=True,
    json_response=True,
    # Railway place le serveur derrière un proxy : on désactive la vérification du Host
    # (la protection vient de l'URL secrète).
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
def whoami() -> str:
    """Vérifie la connexion à Arche et renvoie le nom de l'étudiant connecté."""
    info = site_info()
    return f"Connecté à {info.get('sitename')} en tant que {info.get('fullname')} (id {info.get('userid')})."


@mcp.tool()
def list_courses(include_past: bool = False) -> str:
    """Liste les cours auxquels l'étudiant est inscrit (id, nom, progression).
    Par défaut, masque les cours terminés/cachés ; include_past=True pour tout voir."""
    courses = call("core_enrol_get_users_courses", userid=user_id())
    now = time.time()
    lines = []
    for c in sorted(courses, key=lambda c: c.get("lastaccess") or 0, reverse=True):
        ended = c.get("enddate") and c["enddate"] < now
        if not include_past and (ended or c.get("hidden")):
            continue
        prog = c.get("progress")
        prog_txt = f" — progression {round(prog)}%" if isinstance(prog, (int, float)) else ""
        fav = " ★" if c.get("isfavourite") else ""
        lines.append(f"- [{c['id']}] {c.get('fullname')}{fav}{prog_txt}")
    return "\n".join(lines) or "Aucun cours trouvé."


@mcp.tool()
def upcoming_deadlines(days: int = 30) -> str:
    """Échéances à venir sur Arche (devoirs, quiz, rendus…) dans les `days` prochains jours."""
    now = int(time.time())
    data = call(
        "core_calendar_get_action_events_by_timesort",
        timesortfrom=now - 3600,
        timesortto=now + max(1, min(days, 365)) * 86400,
        limitnum=50,
    )
    events = data.get("events", [])
    if not events:
        return f"Aucune échéance dans les {days} prochains jours."
    lines = []
    for e in events:
        course = (e.get("course") or {}).get("fullname", "")
        action = e.get("action") or {}
        status = "" if action.get("actionable", True) else " (déjà fait / plus d'action requise)"
        lines.append(
            f"- {fmt_ts(e.get('timesort'))} · {e.get('name')} · {course} "
            f"[{e.get('modulename', '')}]{status}\n  {e.get('url', '')}"
        )
    return "\n".join(lines)


@mcp.tool()
def course_contents(course_id: int, include_descriptions: bool = False) -> str:
    """Plan détaillé d'un cours : sections, activités, et fichiers (avec URL pour read_course_file)."""
    sections = call("core_course_get_contents", courseid=course_id)
    out = []
    for s in sections:
        if not s.get("uservisible", True):
            continue
        out.append(f"\n## {s.get('name') or 'Section'}")
        if include_descriptions and s.get("summary"):
            out.append(strip_html(s["summary"]))
        for m in s.get("modules", []):
            if not m.get("uservisible", True):
                continue
            out.append(f"- {m.get('name')} ({m.get('modname')}, id {m.get('id')})")
            if include_descriptions and m.get("description"):
                out.append("  " + strip_html(m["description"])[:800].replace("\n", "\n  "))
            for f in m.get("contents") or []:
                if f.get("type") == "file":
                    size_kb = (f.get("filesize") or 0) // 1024
                    out.append(f"    • {f.get('filename')} ({size_kb} Ko) — {f.get('fileurl')}")
    return truncate("\n".join(out).strip() or "Cours vide ou inaccessible.")


@mcp.tool()
def read_course_file(file_url: str, offset: int = 0) -> str:
    """Télécharge un fichier de cours Arche (URL obtenue via course_contents) et renvoie son texte.
    Formats : PDF, DOCX, PPTX, TXT/code, HTML. `offset` permet de lire la suite d'un long fichier."""
    url = safe_file_url(file_url)
    with http.stream("GET", url) as resp:
        resp.raise_for_status()
        chunks, total = [], 0
        for chunk in resp.iter_bytes():
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                raise MoodleError("Fichier trop volumineux (> 25 Mo).")
            chunks.append(chunk)
        mimetype = resp.headers.get("content-type", "")
    content = b"".join(chunks)
    if content[:1] == b"{" and b"exception" in content[:300]:
        raise MoodleError("Arche a refusé l'accès à ce fichier.")
    filename = urlparse(file_url).path.rsplit("/", 1)[-1]
    text = extract_text(content, filename, mimetype)
    text = text[max(0, offset):]
    if not text.strip():
        return "Le fichier ne contient pas de texte extractible (ex. PDF scanné en images)."
    return truncate(text)


@mcp.tool()
def assignments(course_id: int | None = None) -> str:
    """Devoirs (mod_assign) avec date limite et consignes. Sans course_id : tous les cours."""
    params = {"courseids": [course_id]} if course_id else {}
    data = call("mod_assign_get_assignments", **params)
    now = time.time()
    out = []
    for c in data.get("courses", []):
        items = c.get("assignments", [])
        if not items:
            continue
        out.append(f"\n## {c.get('fullname')}")
        for a in sorted(items, key=lambda a: a.get("duedate") or 0):
            due = a.get("duedate")
            flag = " ⚠️ dépassé" if due and due < now else ""
            out.append(f"- [{a['id']}] {a.get('name')} — limite : {fmt_ts(due)}{flag}")
            intro = strip_html(a.get("intro"))
            if intro:
                out.append("  " + intro[:600].replace("\n", "\n  "))
    return truncate("\n".join(out).strip() or "Aucun devoir trouvé.")


@mcp.tool()
def submission_status(assignment_id: int) -> str:
    """Statut de ton rendu pour un devoir (rendu ou non, note, commentaire)."""
    data = call("mod_assign_get_submission_status", assignid=assignment_id)
    last = (data.get("lastattempt") or {})
    sub = (last.get("submission") or {})
    fb = data.get("feedback") or {}
    lines = [
        f"Statut du rendu : {sub.get('status', 'aucun rendu')}",
        f"Dernière modification : {fmt_ts(sub.get('timemodified'))}",
        f"Statut de notation : {last.get('gradingstatus', '—')}",
    ]
    if fb:
        lines.append(f"Note : {strip_html(fb.get('gradefordisplay')) or '—'}")
        for p in fb.get("plugins", []):
            for e in p.get("editorfields", []) or []:
                if e.get("text"):
                    lines.append(f"Commentaire : {strip_html(e['text'])}")
    return "\n".join(lines)


@mcp.tool()
def grades(course_id: int | None = None) -> str:
    """Notes. Sans course_id : la note globale de chaque cours. Avec course_id : le détail des éléments notés."""
    if course_id is None:
        data = call("gradereport_overview_get_course_grades", userid=user_id())
        courses = {c["id"]: c.get("fullname") for c in call("core_enrol_get_users_courses", userid=user_id())}
        lines = [f"- [{g['courseid']}] {courses.get(g['courseid'], '?')} : {g.get('grade') or '—'}"
                 for g in data.get("grades", [])]
        return "\n".join(lines) or "Aucune note."
    data = call("gradereport_user_get_grade_items", courseid=course_id, userid=user_id())
    out = []
    for ug in data.get("usergrades", []):
        for item in ug.get("gradeitems", []):
            name = item.get("itemname") or ("Total du cours" if item.get("itemtype") == "course" else "Catégorie")
            grade = item.get("gradeformatted") or "—"
            rng = item.get("rangeformatted") or ""
            fbk = strip_html(item.get("feedback"))
            out.append(f"- {name} : {grade} {f'(sur {rng})' if rng else ''}{f' — {fbk}' if fbk else ''}")
    return "\n".join(out) or "Aucune note pour ce cours."


@mcp.tool()
def announcements(course_id: int, limit: int = 10) -> str:
    """Dernières annonces / messages des forums d'un cours (dont le forum « Annonces »)."""
    forums = call("mod_forum_get_forums_by_courses", courseids=[course_id])
    forums = sorted(forums, key=lambda f: f.get("type") != "news")  # « Annonces » en premier
    out = []
    for f in forums[:3]:
        try:
            data = call("mod_forum_get_forum_discussions", forumid=f["id"], sortorder=1, page=0, perpage=limit)
        except MoodleError:
            continue
        discs = data.get("discussions", [])
        if not discs:
            continue
        out.append(f"\n## {f.get('name')}")
        for d in discs:
            out.append(f"### {d.get('name')} — {fmt_ts(d.get('created'))} — {d.get('userfullname', '')}")
            out.append(strip_html(d.get("message"))[:1500])
    return truncate("\n".join(out).strip() or "Aucune annonce.")


# --------------------------------------------------------------------------- #
# Lancement
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import uvicorn
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    app = mcp.streamable_http_app()
    # Petite route de santé pour Railway (ne révèle rien).
    app.router.routes.append(Route("/", lambda request: PlainTextResponse("ok")))
    print(f"Serveur MCP Arche prêt sur le port {PORT} (chemin secret configuré).", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=PORT, proxy_headers=True, forwarded_allow_ips="*")
