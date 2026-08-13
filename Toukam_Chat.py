import streamlit as st
import os
import io
import re
import smtplib
import random
import sqlite3
import uuid
from urllib.parse import quote, unquote
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from PIL import Image
import fitz  # PyMuPDF
from fpdf import FPDF
from google import genai
from google.genai import types
from google.genai.errors import APIError

# --- DOSSIER DE FICHIERS STATIQUES (pour que le PDF soit accessible par une vraie URL) ---
# Nécessaire au téléchargement du PDF dans l'app APK : contrairement à un navigateur, la WebView
# de Median ne sait pas gérer un fichier généré en mémoire (blob) via st.download_button, elle a
# besoin d'une vraie URL http(s). Voir aussi : côté hébergement, il faut activer
# enableStaticServing = true dans .streamlit/config.toml (section [server]), sinon ce dossier
# n'est pas servi et l'URL renverra une erreur 404.
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(STATIC_DIR, exist_ok=True)

# --- CONFIGURATION ET STRUCTURE VISUELLE (Toukam Chat) ---
st.set_page_config(page_title="Toukam Chat", page_icon="🎓", layout="wide")
st.title("🎓 Toukam Chat : Votre Tuteur IA d'Élite")

# Style CSS pour aligner les bulles (Étudiant à droite en vert, Toukam à gauche en gris)
st.markdown("""
<style>
.st-emotion-cache-1gh7wcc {
    flex-direction: row-reverse !important;
    text-align: right !important;
    background-color: #DCF8C6 !important;
    border-radius: 15px 15px 0px 15px !important;
    padding: 10px !important;
    margin-left: auto !important;
    max-width: 75% !important;
}
.st-emotion-cache-th6ff {
    background-color: #F0F2F5 !important;
    border-radius: 15px 15px 15px 0px !important;
    padding: 10px !important;
    margin-right: auto !important;
    max-width: 75% !important;
}
</style>
""", unsafe_allow_html=True)

AVATAR_ETUDIANT = "user"
AVATAR_TOUKAM = "assistant"

# --- MÉMORISATION DES IDENTIFIANTS DANS LE NAVIGATEUR (cookies, aucune saisie côté serveur) ---
# Permet de ne plus avoir à re-saisir son e-mail / code secret à chaque ouverture de l'application,
# tant que c'est le même navigateur sur le même appareil (rien n'est stocké ailleurs).

def _lire_cookie(nom, defaut=""):
    try:
        valeur = st.context.cookies.get(nom)
    except Exception:
        # st.context.cookies n'existe pas (version de Streamlit trop ancienne, <1.37 environ) :
        # c'est très probablement pourquoi la connexion ne reste jamais mémorisée. On le signale
        # une seule fois au lieu d'échouer en silence à chaque rechargement.
        if not st.session_state.get("_alerte_cookie_affichee"):
            st.session_state["_alerte_cookie_affichee"] = True
            st.sidebar.warning(
                "⚠️ La mémorisation de connexion ne fonctionne pas : `st.context.cookies` est "
                "indisponible sur cette version de Streamlit. Mets à jour Streamlit (≥1.37) dans "
                "requirements.txt pour corriger ça."
            )
        valeur = None
    if not valeur:
        return defaut
    try:
        return unquote(valeur)
    except Exception:
        return defaut


def _memoriser_cookie(nom, valeur, jours=365):
    """Écrit une valeur dans un cookie du navigateur (persistant plusieurs mois).
    CORRECTIF : st.markdown(unsafe_allow_html=True) n'exécute jamais les balises <script>
    (limitation du DOM), donc ce cookie n'était en réalité JAMAIS écrit -- d'où la
    reconnexion systématique. st.components.v1.html rend le JS dans un vrai <iframe> où
    le script s'exécute réellement ; on écrit le cookie sur le document parent (la vraie
    page de l'app) pour que le futur chargement de page le retrouve bien."""
    valeur_encodee = quote((valeur or "").strip())
    st.components.v1.html(
        f"""<script>
        try {{
            window.parent.document.cookie = "{nom}={valeur_encodee}; path=/; max-age={jours * 86400}; SameSite=Lax";
        }} catch (e) {{
            document.cookie = "{nom}={valeur_encodee}; path=/; max-age={jours * 86400}; SameSite=Lax";
        }}
        </script>""",
        height=0,
    )


def _effacer_cookie(nom):
    st.components.v1.html(
        f"""<script>
        try {{
            window.parent.document.cookie = "{nom}=; path=/; max-age=0";
        }} catch (e) {{
            document.cookie = "{nom}=; path=/; max-age=0";
        }}
        </script>""",
        height=0,
    )


def _obtenir_identifiant_appareil():
    """Identifiant anonyme unique par navigateur/appareil, utilisé UNIQUEMENT pour empêcher
    de contourner les quotas gratuits en changeant d'e-mail. Généré une seule fois puis conservé
    dans un cookie (5 ans), indépendamment de la case « Se souvenir de moi »."""
    identifiant = _lire_cookie("toukam_appareil")
    if not identifiant:
        identifiant = "appareil_" + uuid.uuid4().hex
        _memoriser_cookie("toukam_appareil", identifiant, jours=1825)
    return identifiant

# --- PERSISTANCE DES QUOTAS SANS TRICHE (SQLite3) ---
DB_FILE = "toukam_data.db"

def initialiser_bdd():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS quotas (
            email TEXT PRIMARY KEY,
            images_utilisees INTEGER DEFAULT 0,
            pdf_telecharges INTEGER DEFAULT 0
        )
    """)
    # Clé API personnelle de l'utilisateur : stockée définitivement côté serveur (liée à son e-mail),
    # donc disponible même s'il change d'appareil ou de navigateur.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cles_api_utilisateur (
            email TEXT PRIMARY KEY,
            cle_api TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def recuperer_quotas(cle):
    """Lit le quota associé à une clé (peut être un e-mail ou un identifiant d'appareil)."""
    if not cle:
        return {"images": 0, "pdf_conv": 0}
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT images_utilisees, pdf_telecharges FROM quotas WHERE email = ?", (cle.strip().lower(),))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"images": row[0], "pdf_conv": row[1]}
    return {"images": 0, "pdf_conv": 0}

def incrementer_quota(cle, type_quota):
    """Incrémente le quota associé à une clé (e-mail ou identifiant d'appareil)."""
    if not cle:
        return
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cle_p = cle.strip().lower()
    cursor.execute("INSERT OR IGNORE INTO quotas (email, images_utilisees, pdf_telecharges) VALUES (?, 0, 0)", (cle_p,))
    if type_quota == "images":
        cursor.execute("UPDATE quotas SET images_utilisees = images_utilisees + 1 WHERE email = ?", (cle_p,))
    elif type_quota == "pdf_conv":
        cursor.execute("UPDATE quotas SET pdf_telecharges = pdf_telecharges + 1 WHERE email = ?", (cle_p,))
    conn.commit()
    conn.close()

initialiser_bdd()

def obtenir_quotas_anti_triche(email, device_id):
    """Combine le quota lié à l'e-mail et celui lié à l'appareil (cookie anonyme non modifiable
    par l'utilisateur), et retient le PLUS ÉLEVÉ des deux. Ainsi, changer d'e-mail sur le même
    appareil (ou changer d'appareil en gardant le même e-mail) ne permet plus de réinitialiser
    son quota gratuit."""
    q_email = recuperer_quotas(email)
    q_device = recuperer_quotas(device_id)
    return {
        "images": max(q_email["images"], q_device["images"]),
        "pdf_conv": max(q_email["pdf_conv"], q_device["pdf_conv"]),
    }

def incrementer_quota_anti_triche(email, device_id, type_quota):
    """Incrémente le compteur à la fois pour l'e-mail et pour l'appareil, pour que le quota
    reste bloqué même si l'utilisateur change ensuite d'e-mail ou de navigateur."""
    incrementer_quota(email, type_quota)
    incrementer_quota(device_id, type_quota)

def enregistrer_cle_utilisateur(email, cle_api):
    """Enregistre (ou remplace) la clé API personnelle de l'utilisateur, de façon permanente."""
    if not email or not cle_api.strip():
        return
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO cles_api_utilisateur (email, cle_api) VALUES (?, ?) "
        "ON CONFLICT(email) DO UPDATE SET cle_api = excluded.cle_api",
        (email.strip().lower(), cle_api.strip())
    )
    conn.commit()
    conn.close()

def recuperer_cle_utilisateur(email):
    """Récupère la clé API personnelle enregistrée pour cet e-mail, si elle existe."""
    if not email:
        return None
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT cle_api FROM cles_api_utilisateur WHERE email = ?", (email.strip().lower(),))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def supprimer_cle_utilisateur(email):
    """Supprime la clé API personnelle enregistrée pour cet e-mail."""
    if not email:
        return
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM cles_api_utilisateur WHERE email = ?", (email.strip().lower(),))
    conn.commit()
    conn.close()

# Base des abonnés Premium
CLIENTS_PREMIUM = {"paultoukam04@gmail.com": "TOUKAM-BOSS-2026"}

if "appareils_verrouilles" not in st.session_state:
    st.session_state.appareils_verrouilles = {}

# --- POOL COMPLET DE VOS CLÉS API (Nouvelle clé incluse) ---
if "pool_cles" not in st.session_state:
    st.session_state.pool_cles = [
        "AQ.Ab8RN6IdXDa51RnwgDdBYqhvepP0X3gnDc_F8BvyPFDzL748mg",
        "AQ.Ab8RN6L1Ui9YteK95uXOl_lb_kF8MXDMUunBSk0IZ9snezHVbw",
        "AQ.Ab8RN6Jb8Xol-9bkwy1NqLVclooawM-UrL96zFbVJ0sOcge35g",
        "AQ.Ab8RN6KPOERVdeKLnuzWuJqPykieN8YPdnHHT3IL14CyLY-Phg",
        "AQ.Ab8RN6KiXE9JACip1v2r9zVZOzVb_y8fxsAQ0PBNUvMCq5cYYA",
        "AQ.Ab8RN6JQAwVz77ZR9BxG-caETgRuHVSi1fnvCCMeVnG-cq7qPA",
        "AQ.Ab8RN6LflfCafj9XrBYD5z7zyo6r9QXCljDEhRtwYfJdVXxkqA",
        "AQ.Ab8RN6Kpt_GF4xDIcKTOkLdVHwPJrJsx17BICOzxPiztmrbwZg",
        "AQ.Ab8RN6KUYR9EKAyBRQiD6o7Ia3Lq5_Ye3apjzPXjxi5fbN_TFg"
    ]

CLE_PREMIUM_PROD = "METS_TA_FUTURE_CLE_PAYANTE_ICI"

def faire_defiler_vers_le_bas():
    st.markdown('<div id="fin-du-chat"></div>', unsafe_allow_html=True)
    st.components.v1.html(
        '<script>var element = window.parent.document.getElementById("fin-du-chat");'
        'if (element) { element.scrollIntoView({ behavior: "smooth", block: "end" }); }</script>',
        height=0,
    )

# Marqueurs internes (caractères invisibles à usage privé Unicode) utilisés pour repérer
# les fractions dans le texte et les redessiner plus tard avec une vraie barre horizontale
# au lieu d'un simple "/". Ils sont retirés avant l'affichage final.
_FRAC_DEBUT = "\uE001"
_FRAC_MILIEU = "\uE002"
_FRAC_FIN = "\uE003"
_FRAC_MOTIF = re.compile(_FRAC_DEBUT + r'(.*?)' + _FRAC_MILIEU + r'(.*?)' + _FRAC_FIN, re.DOTALL)


def _marqueur_fraction(numerateur, denominateur):
    return f"{_FRAC_DEBUT}{numerateur.strip()}{_FRAC_MILIEU}{denominateur.strip()}{_FRAC_FIN}"


def nettoyer_pour_pdf(texte, police_unicode_active=True):
    """Convertit le Markdown/LaTeX brut renvoyé par l'IA en texte lisible pour le PDF."""
    # Lignes de séparation de tableau Markdown (| :--- | :---: |)
    texte = re.sub(r'^[ \t]*\|[ \t:\-|]+\|[ \t]*$', '', texte, flags=re.MULTILINE)
    # Lignes de tableau "| a | b |" -> "a | b"
    texte = re.sub(r'^[ \t]*\|(.+)\|[ \t]*$', lambda m: m.group(1).strip(' |'), texte, flags=re.MULTILINE)
    # Titres Markdown "### Titre" -> "TITRE" isolé
    texte = re.sub(r'^#{1,6}\s*(.+)$', lambda m: '\n' + m.group(1).strip().upper() + '\n', texte, flags=re.MULTILINE)
    # Lignes horizontales ---
    texte = re.sub(r'^-{3,}\s*$', '', texte, flags=re.MULTILINE)
    # Gras/italique Markdown
    texte = texte.replace('**', '').replace('__', '')
    texte = re.sub(r'(?<!\*)\*(?!\*)([^\*\n]+)\*(?!\*)', r'\1', texte)
    # Délimiteurs LaTeX $$ ... $$ et $ ... $
    texte = texte.replace('$$', '').replace('$', '')
    # Commandes LaTeX courantes -> symboles Unicode lisibles
    remplacements = {
        r'\times': '×', r'\cdot': '·', r'\pi': 'π', r'\sqrt': '√',
        r'\implies': '⇒', r'\Rightarrow': '⇒', r'\rightarrow': '→', r'\to': '→',
        r'\leq': '≤', r'\geq': '≥', r'\neq': '≠', r'\approx': '≈',
        r'\alpha': 'α', r'\beta': 'β', r'\gamma': 'γ', r'\Theta': 'Θ', r'\theta': 'θ',
        r'\ell': 'ℓ', r'\infty': '∞', r'\pm': '±', r'\Delta': 'Δ', r'\omega': 'ω',
        r'\eta': 'η', r'\mu': 'μ', r'\nu': 'ν', r'\lambda': 'λ', r'\sigma': 'σ',
        r'\rho': 'ρ', r'\phi': 'φ', r'\tau': 'τ',
        r'\text': '', r'\mathbf': '', r'\left': '', r'\right': '',
    }
    for latex, symbole in remplacements.items():
        texte = texte.replace(latex, symbole)
    # \frac{a}{b} -> marqueur interne pour dessiner une vraie barre de fraction plus tard
    texte = re.sub(r'\\frac\{([^{}]*)\}\{([^{}]*)\}', lambda m: _marqueur_fraction(m.group(1), m.group(2)), texte)
    # Commandes LaTeX restantes non reconnues (\quelquechose)
    texte = re.sub(r'\\[a-zA-Z]+', '', texte)
    # Accolades de regroupement LaTeX restantes
    texte = texte.replace('{', '').replace('}', '')

    if police_unicode_active:
        texte = convertir_exposants_indices(texte)
    else:
        # Sans police Unicode, on garde une notation simple et lisible avec des parenthèses
        texte = re.sub(r'\^\(?(-?\d+(?:/\d+)?)\)?', r'^(\1)', texte)

    # Divisions écrites en clair "(A)/(B)" -> marqueur pour barre horizontale (après les exposants,
    # pour ne pas confondre avec les exposants fractionnaires du type l^1/2 déjà traités ci-dessus)
    texte = re.sub(
        r'\(([^()]+)\)/\(([^()]+)\)',
        lambda m: _marqueur_fraction(m.group(1), m.group(2)),
        texte
    )

    # Émojis / pictogrammes non gérés par la police du PDF
    texte = re.sub(
        r'[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\u2190-\u21FF\u2B00-\u2BFF\uFE0F]',
        '', texte
    )
    # Espaces/retours à la ligne excessifs laissés par le nettoyage
    texte = re.sub(r'[ \t]{2,}', ' ', texte)
    texte = re.sub(r'\n{3,}', '\n\n', texte)
    return texte.strip()


_EXPOSANTS = str.maketrans({
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹",
    "-": "⁻", "+": "⁺",
    "a": "ᵃ", "b": "ᵇ", "c": "ᶜ", "d": "ᵈ", "e": "ᵉ", "f": "ᶠ", "g": "ᵍ", "h": "ʰ", "i": "ⁱ", "j": "ʲ",
    "k": "ᵏ", "l": "ˡ", "m": "ᵐ", "n": "ⁿ", "o": "ᵒ", "p": "ᵖ", "r": "ʳ", "s": "ˢ", "t": "ᵗ", "u": "ᵘ",
    "v": "ᵛ", "w": "ʷ", "x": "ˣ", "y": "ʸ", "z": "ᶻ",
})
_INDICES = str.maketrans({
    "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅", "6": "₆", "7": "₇", "8": "₈", "9": "₉",
    "a": "ₐ", "e": "ₑ", "h": "ₕ", "k": "ₖ", "l": "ₗ", "m": "ₘ", "n": "ₙ", "o": "ₒ", "p": "ₚ", "s": "ₛ",
    "t": "ₜ", "u": "ᵤ", "v": "ᵥ", "x": "ₓ",
})


def convertir_exposants_indices(texte):
    """Transforme la notation 'X^-2' / 'X_1' en vrais exposants/indices Unicode (X⁻², X₁),
    comme dans un document imprimé classique, au lieu du caret brut qui gêne la lecture."""
    # Exposants fractionnaires du type ^1/2 ou ^-1/2 -> ¹⁄₂, ⁻¹⁄₂
    def _exposant_fraction(m):
        signe = "⁻" if (m.group(1) or m.group(4)) == "-" else ""
        num = (m.group(2) or m.group(5)).translate(_EXPOSANTS)
        den = (m.group(3) or m.group(6)).translate(_INDICES)
        return signe + num + "⁄" + den

    texte = re.sub(r'\^\((-?)(\d+)/(\d+)\)|\^(-?)(\d+)/(\d+)', _exposant_fraction, texte)

    # Exposants entiers ou à une seule lettre : ^-3, ^12, ^a, ou ^(3) avec parenthèses explicites
    def _exposant_simple(m):
        return (m.group(1) or m.group(2)).translate(_EXPOSANTS)

    texte = re.sub(r'\^\((-?\d+|[a-z])\)|\^(-?\d+|[a-z])', _exposant_simple, texte)

    # Indices : m_1, Q_v, m_2 ... ou m_(1) avec parenthèses explicites
    def _indice_simple(m):
        return (m.group(1) or m.group(2)).translate(_INDICES)

    texte = re.sub(r'_\((\d+|[a-z])\)|_(\d+|[a-z])', _indice_simple, texte)
    return texte


def nettoyer_pour_affichage(texte):
    """Nettoie la réponse brute de l'IA pour un affichage lisible DANS LE CHAT (écran), en
    convertissant les indices/exposants et le LaTeX en vrai Unicode -- contrairement à
    nettoyer_pour_pdf(), on garde le Markdown (gras, listes...) car st.write/st.markdown
    sait déjà le restituer correctement à l'écran."""
    if not texte:
        return texte
    texte = texte.replace('$$', '').replace('$', '')
    remplacements = {
        r'\times': '×', r'\cdot': '·', r'\pi': 'π', r'\sqrt': '√',
        r'\implies': '⇒', r'\Rightarrow': '⇒', r'\rightarrow': '→', r'\to': '→',
        r'\leq': '≤', r'\geq': '≥', r'\neq': '≠', r'\approx': '≈',
        r'\alpha': 'α', r'\beta': 'β', r'\gamma': 'γ', r'\Theta': 'Θ', r'\theta': 'θ',
        r'\ell': 'ℓ', r'\infty': '∞', r'\pm': '±', r'\Delta': 'Δ', r'\omega': 'ω',
        r'\eta': 'η', r'\mu': 'μ', r'\nu': 'ν', r'\lambda': 'λ', r'\sigma': 'σ',
        r'\rho': 'ρ', r'\phi': 'φ', r'\tau': 'τ',
        r'\text': '', r'\mathbf': '', r'\left': '', r'\right': '',
    }
    for latex, symbole in remplacements.items():
        texte = texte.replace(latex, symbole)
    # \frac{a}{b} -> "a⁄b" lisible en ligne (pas de vraie barre horizontale possible en Markdown)
    texte = re.sub(
        r'\\frac\{([^{}]*)\}\{([^{}]*)\}',
        lambda m: f"{m.group(1).strip()}⁄{m.group(2).strip()}",
        texte
    )
    texte = re.sub(r'\\[a-zA-Z]+', '', texte)
    texte = texte.replace('{', '').replace('}', '')
    texte = convertir_exposants_indices(texte)
    return texte


# --- RENDU DES FRACTIONS EN LIGNE (dans le texte, sans retour à la ligne forcé) ---
# Les fractions sont traitées comme des "mots" à part entière dans la composition du texte :
# elles continuent la ligne en cours, à la suite des mots qui précèdent, et ne passent à la
# ligne suivante que si elles ne tiennent plus — exactement comme n'importe quel mot.
_MARGE_FRACTION = 1.5          # mm de part et d'autre du numérateur/dénominateur, autour de la barre
_ESPACE_AUTOUR_FRACTION = 1.5  # mm de "respiration" entre la fraction et les mots voisins
_H_NUM = 4.2                   # hauteur (mm) réservée au numérateur
_H_DEN = 4.2                   # hauteur (mm) réservée au dénominateur
_H_ECART_BARRE = 0.8           # espace (mm) entre le texte et la barre


def _mesurer_fraction(pdf, numerateur, denominateur, taille_normale):
    """Calcule la largeur totale qu'occupera la fraction sur la ligne, sans toucher à la
    position d'écriture courante (utilisé pour décider où faire un retour à la ligne)."""
    taille_fraction = max(taille_normale * 0.72, 7)
    pdf.set_font_size(taille_fraction)
    largeur_texte = max(pdf.get_string_width(numerateur), pdf.get_string_width(denominateur))
    pdf.set_font_size(taille_normale)
    largeur_totale = largeur_texte + _MARGE_FRACTION * 2 + _ESPACE_AUTOUR_FRACTION * 2
    return largeur_totale, taille_fraction


def _dessiner_fraction_a(pdf, x, y_haut, largeur, taille_fraction, numerateur, denominateur, taille_normale):
    """Dessine numérateur / barre / dénominateur à une position précise (x, y_haut), en restant
    sur la même ligne que le texte voisin (pas de saut de ligne avant/après)."""
    pdf.set_font_size(taille_fraction)
    pdf.set_xy(x, y_haut)
    pdf.cell(largeur, _H_NUM, numerateur, align="C")

    y_barre = y_haut + _H_NUM + _H_ECART_BARRE * 0.5
    pdf.set_line_width(0.25)
    pdf.line(x + _MARGE_FRACTION, y_barre, x + largeur - _MARGE_FRACTION, y_barre)

    pdf.set_xy(x, y_barre + _H_ECART_BARRE * 0.5)
    pdf.cell(largeur, _H_DEN, denominateur, align="C")

    pdf.set_font_size(taille_normale)


def _rendre_ligne(pdf, jetons, hauteur_ligne, taille_normale):
    """Affiche une ligne composée de mots et/ou de fractions, alignés côte à côte. La hauteur de
    la ligne s'agrandit automatiquement si elle contient une fraction, pour ne jamais chevaucher
    la ligne suivante."""
    contient_fraction = any(j[0] == "fraction" for j in jetons)
    hauteur_ligne_reelle = max(hauteur_ligne, _H_NUM + _H_ECART_BARRE + _H_DEN) if contient_fraction else hauteur_ligne

    if pdf.get_y() + hauteur_ligne_reelle > pdf.page_break_trigger:
        pdf.add_page()

    y_haut = pdf.get_y()
    x = pdf.l_margin
    pdf.set_font_size(taille_normale)
    espace_mot = pdf.get_string_width(" ")

    for i, jeton in enumerate(jetons):
        if i > 0:
            x += espace_mot
        if jeton[0] == "mot":
            _, mot, largeur = jeton
            pdf.set_xy(x, y_haut)
            pdf.cell(largeur, hauteur_ligne_reelle, mot, align="L")
            x += largeur
        else:
            _, num, den, largeur, taille_frac = jeton
            decalage = max((hauteur_ligne_reelle - (_H_NUM + _H_ECART_BARRE + _H_DEN)) / 2, 0)
            _dessiner_fraction_a(pdf, x, y_haut + decalage, largeur, taille_frac, num, den, taille_normale)
            x += largeur

    pdf.set_xy(pdf.l_margin, y_haut + hauteur_ligne_reelle)


def ecrire_avec_fractions(pdf, texte, hauteur_ligne=8, unicode_ok=True):
    """Compose le texte ligne par ligne, comme un traitement de texte classique : chaque fraction
    est un "mot" qui continue la ligne en cours, à la suite du texte qui la précède, et ne passe
    à la ligne suivante que si elle ne tient plus (jamais de saut de ligne forcé avant elle)."""
    taille_normale = pdf.font_size_pt
    largeur_utile = pdf.w - pdf.l_margin - pdf.r_margin

    def _texte_ok(t):
        return t.encode('latin-1', 'replace').decode('latin-1') if not unicode_ok else t

    for paragraphe in texte.split("\n"):
        if paragraphe.strip() == "":
            # Ligne vide voulue par l'IA (aération entre deux idées) : on la respecte.
            if pdf.get_y() + hauteur_ligne > pdf.page_break_trigger:
                pdf.add_page()
            pdf.ln(hauteur_ligne * 0.6)
            continue

        # 1) Découpe le paragraphe en jetons : mots normaux ou fractions.
        jetons = []
        position = 0
        for m in _FRAC_MOTIF.finditer(paragraphe):
            avant = paragraphe[position:m.start()]
            for mot in avant.split(" "):
                if mot != "":
                    mot_aff = _texte_ok(mot)
                    jetons.append(("mot", mot_aff, pdf.get_string_width(mot_aff)))
            largeur_frac, taille_frac = _mesurer_fraction(pdf, m.group(1), m.group(2), taille_normale)
            jetons.append(("fraction", _texte_ok(m.group(1)), _texte_ok(m.group(2)), largeur_frac, taille_frac))
            position = m.end()
        for mot in paragraphe[position:].split(" "):
            if mot != "":
                mot_aff = _texte_ok(mot)
                jetons.append(("mot", mot_aff, pdf.get_string_width(mot_aff)))

        # 2) Recompose les jetons en lignes : retour à la ligne seulement quand ça ne tient plus.
        espace_mot = pdf.get_string_width(" ")
        ligne_courante, largeur_courante = [], 0.0
        for jeton in jetons:
            largeur_jeton = jeton[2] if jeton[0] == "mot" else jeton[3]
            largeur_avec_espace = largeur_jeton + (espace_mot if ligne_courante else 0)
            if ligne_courante and largeur_courante + largeur_avec_espace > largeur_utile:
                _rendre_ligne(pdf, ligne_courante, hauteur_ligne, taille_normale)
                ligne_courante, largeur_courante = [], 0.0
                largeur_avec_espace = largeur_jeton
            ligne_courante.append(jeton)
            largeur_courante += largeur_avec_espace
        if ligne_courante:
            _rendre_ligne(pdf, ligne_courante, hauteur_ligne, taille_normale)


def _trouver_police_unicode():
    """Cherche une police TTF Unicode (DejaVu Sans) disponible sur le système."""
    chemins_possibles = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "DejaVuSans.ttf"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "DejaVuSans.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    ]
    for chemin in chemins_possibles:
        if os.path.isfile(chemin):
            return chemin
    return None


class _PDFToukamChat(FPDF):
    """PDF avec un filigrane discret 'TOUKAM CHAT' et une petite mise en forme (bandeau,
    liseré de couleur, pied de page), sans jamais gêner la lecture du contenu."""

    def header(self):
        # --- Filigrane diagonal, très clair, en fond de page : décoratif, pas gênant à la lecture ---
        self.set_font("Arial", size=42)
        self.set_text_color(232, 232, 232)
        largeur_filigrane = self.get_string_width("TOUKAM CHAT")
        try:
            with self.rotation(45, x=self.w / 2, y=self.h / 2):
                self.text(self.w / 2 - largeur_filigrane / 2, self.h / 2, "TOUKAM CHAT")
        except AttributeError:
            pass  # Ancienne version de fpdf sans support de rotation : on ignore simplement le filigrane

        # --- Petit bandeau supérieur : logo texte + liseré de couleur ---
        self.set_text_color(110, 110, 110)
        self.set_font("Arial", size=9)
        self.set_xy(self.l_margin, 8)
        self.cell(0, 5, "Toukam Chat", align="L")
        self.set_draw_color(46, 139, 87)
        self.set_line_width(0.6)
        self.line(self.l_margin, 14, self.w - self.r_margin, 14)

        self.set_text_color(0, 0, 0)
        self.set_xy(self.l_margin, 20)

    def footer(self):
        self.set_y(-15)
        self.set_draw_color(210, 210, 210)
        self.set_line_width(0.2)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.set_font("Arial", size=8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 10, f"Toukam Chat  -  page {self.page_no()}/{{nb}}", align="C")


def generer_pdf(contenu):
    """Crée un PDF lisible à partir du texte de l'IA (nettoyage Markdown/LaTeX, police Unicode,
    fractions composées en ligne avec une vraie barre horizontale, et une mise en page discrètement
    stylée avec filigrane "Toukam Chat")."""
    pdf = _PDFToukamChat()
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    police_unicode = _trouver_police_unicode()
    texte_propre = nettoyer_pour_pdf(contenu, police_unicode_active=bool(police_unicode))

    if police_unicode:
        # Police Unicode : accents, symboles mathématiques et caractères spéciaux s'affichent correctement
        pdf.add_font("Unicode", "", police_unicode, uni=True)
        pdf.set_font("Unicode", size=12)
    else:
        # Repli si aucune police Unicode n'est trouvée sur le serveur :
        # on reste en Arial ; le texte et les fractions sont encodés en latin-1 au moment de l'écriture.
        pdf.set_font("Arial", size=12)

    ecrire_avec_fractions(pdf, texte_propre, hauteur_ligne=8, unicode_ok=bool(police_unicode))

    # CORRECTION CRITIQUE : Conversion forcée du bytearray en bytes standards pour Streamlit
    flux_brut = pdf.output(dest='S')
    return bytes(flux_brut)


def sauvegarder_pdf_statique(pdf_bytes, nom_pdf):
    """Écrit le PDF dans le dossier static/ avec un nom unique, pour obtenir une vraie URL
    de téléchargement (indispensable pour que ça marche dans l'app APK, pas seulement au
    navigateur). Retourne le nom du fichier écrit."""
    nom_fichier = f"{uuid.uuid4().hex}_{nom_pdf}"
    with open(os.path.join(STATIC_DIR, nom_fichier), "wb") as f:
        f.write(pdf_bytes)
    return nom_fichier


def envoyer_email(destinataire, fichier_pdf, nom_fichier):
    expediteur = "paultoukam04@gmail.com"
    mot_de_pass = "VOTRE_MOT_DE_PASSE_APPLICATION"  # Remplace par ton code application à 16 lettres
    msg = MIMEMultipart()
    msg['From'] = expediteur
    msg['To'] = destinataire
    msg['Subject'] = "🎓 Toukam Chat : Votre document d'étude"
    msg.attach(MIMEText("Bonjour ! Voici votre document généré par Toukam Chat.", 'plain'))
    
    part = MIMEBase('application', 'octet-stream')
    part.set_payload(fichier_pdf)
    encoders.encode_base64(part)
    part.add_header('Content-Disposition', f"attachment; filename={nom_fichier}")
    msg.attach(part)
    try:
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(expediteur, mot_de_pass)
        server.send_message(msg)
        server.quit()
        return True
    except:
        return False

# --- INTERFACE SIDEBAR PARAMÈTRES ET COMMERCE ---
with st.sidebar:
    st.header("⚙️ Paramètres Toukam")
    mode = st.radio("Mode de travail :", ["Aide aux exercices", "Fiche de révision", "Planning intelligent"])
    exam_date, heures, sujets = None, None, ""
    if mode == "Planning intelligent":
        exam_date = st.date_input("Date de l'examen")
        heures = st.slider("Heures/jour", 1, 12, 4)
        sujets = st.text_area("Matières et chapitres")
        
    st.divider()
    souvenir = st.checkbox(
        "💾 Se souvenir de moi sur cet appareil",
        value=_lire_cookie("toukam_souvenir", "1") == "1",
        help="Ton e-mail et ton code seront retenus dans ce navigateur : plus besoin de les retaper à chaque visite.",
    )

    if "email_user" not in st.session_state:
        st.session_state.email_user = _lire_cookie("toukam_email") if souvenir else ""
    if "code_saisi" not in st.session_state:
        st.session_state.code_saisi = _lire_cookie("toukam_code") if souvenir else ""

    email_user = st.text_input("Votre e-mail de connexion", key="email_user")
    code_saisi = st.text_input("Entrez votre code Premium", type="password", key="code_saisi")

    if souvenir:
        _memoriser_cookie("toukam_email", email_user)
        _memoriser_cookie("toukam_code", code_saisi)
        _memoriser_cookie("toukam_souvenir", "1")
    else:
        _effacer_cookie("toukam_email")
        _effacer_cookie("toukam_code")
        _memoriser_cookie("toukam_souvenir", "0")
    
    est_premium = False
    device_id = _obtenir_identifiant_appareil()
    quotas_actuels = obtenir_quotas_anti_triche(email_user, device_id)
    
    if email_user.strip() and code_saisi.strip():
        email_propre = email_user.strip().lower()
        if email_propre in CLIENTS_PREMIUM and CLIENTS_PREMIUM[email_propre] == code_saisi.strip():
            empreinte = st.context.headers.get("User-Agent", "Device_X")
            if email_propre not in st.session_state.appareils_verrouilles:
                st.session_state.appareils_verrouilles[email_propre] = empreinte
            if st.session_state.appareils_verrouilles[email_propre] == empreinte:
                st.success("👑 Premium Activé !")
                est_premium = True
            else:
                st.error("🚨 Compte déjà utilisé ailleurs.")
        elif code_saisi:
            st.error("❌ Code invalide.")
            
    if not est_premium:
        with st.expander("🚀 Devenir Premium (1300 XAF)"):
            st.write("Cliquez sur votre opérateur pour lancer l'appel direct :")
            c1, c2 = st.columns(2)
            with c1:
                st.link_button("📲 MTN MoMo", "tel:%2A126%2A1%2A1300%2A682211388%23", use_container_width=True)
            with c2:
                st.link_button("📲 Orange Money", "tel:%23150%2A1%2A1%2A655627964%2A1300%23", use_container_width=True)
            st.markdown("---")
            st.write("### Étape 2 : Envoyez votre reçu")
            msg_wa = f"Bonjour Paul ! Je viens de payer 1300 XAF pour Toukam Chat Premium. Mon e-mail est : {email_user}."
            # NUMERO_WHATSAPP : mets ici ton numéro au format international sans "+" ni espaces
            # (ex. 237682211388), sinon le bouton WhatsApp n'a pas de destinataire.
            NUMERO_WHATSAPP = "237682211388"
            st.link_button("🟢 Envoyer par WhatsApp", f"https://wa.me/{NUMERO_WHATSAPP}?text={quote(msg_wa)}", use_container_width=True)
            st.link_button("📧 Envoyer par E-mail", f"mailto:paultoukam04@gmail.com?subject=Paiement%20Toukam%20Chat&body={quote(msg_wa)}", use_container_width=True)

    st.divider()
    with st.expander("🔑 Utiliser ma propre clé API"):
        st.caption(
            "Ta clé est enregistrée définitivement sur le serveur, liée à ton e-mail : "
            "tu la retrouveras automatiquement même en changeant de téléphone ou d'ordinateur."
        )
        if not email_user.strip():
            st.info("Saisis d'abord ton e-mail ci-dessus pour pouvoir enregistrer ta clé.")
        else:
            cle_deja_enregistree = recuperer_cle_utilisateur(email_user)
            if cle_deja_enregistree:
                st.success(f"✅ Clé enregistrée : ...{cle_deja_enregistree[-4:]}")
            nouvelle_cle = st.text_input(
                "Ta clé API Gemini (clé Google AI Studio)",
                type="password",
                key="champ_cle_perso"
            )
            cc1, cc2 = st.columns(2)
            with cc1:
                if st.button("💾 Enregistrer ma clé", use_container_width=True):
                    if nouvelle_cle.strip():
                        enregistrer_cle_utilisateur(email_user, nouvelle_cle)
                        st.success("Clé enregistrée avec succès !")
                        st.rerun()
                    else:
                        st.warning("Colle d'abord ta clé dans le champ ci-dessus.")
            with cc2:
                if cle_deja_enregistree and st.button("🗑️ Supprimer ma clé", use_container_width=True):
                    supprimer_cle_utilisateur(email_user)
                    st.success("Clé supprimée.")
                    st.rerun()

    st.divider()
    with st.expander("📷🎙️ Problème d'autorisation caméra/micro ?"):
        st.caption(
            "Si l'appareil photo ou le micro ne fonctionnent pas et qu'aucune fenêtre "
            "d'autorisation ne s'affiche, c'est probablement qu'Android/iOS a déjà enregistré "
            "un refus. Le bouton ci-dessous ouvre directement l'écran des paramètres de "
            "l'application où tu peux activer Caméra et Microphone manuellement."
        )
        st.components.v1.html(
            """
            <button id="btn_parametres_app" style="
                background-color:#2E8B57; color:white; border:none; padding:0.55em 1em;
                border-radius:8px; cursor:pointer; width:100%; font-size:0.95em; font-family:inherit;">
                ⚙️ Ouvrir les paramètres de l'application
            </button>
            <div id="msg_parametres_app" style="font-size:0.85em; color:#888; margin-top:6px;"></div>
            <script>
            (function() {
                var btn = document.getElementById("btn_parametres_app");
                var msg = document.getElementById("msg_parametres_app");
                if (!btn) { return; }
                btn.onclick = function() {
                    try {
                        if (window.parent && window.parent.median && window.parent.median.open
                            && window.parent.median.open.appSettings) {
                            window.parent.median.open.appSettings();
                            return;
                        }
                    } catch (e) {}
                    if (window.median && window.median.open && window.median.open.appSettings) {
                        window.median.open.appSettings();
                        return;
                    }
                    // Repli : pas dans l'app Median (ex. testé depuis un navigateur PC/mobile classique)
                    msg.textContent = "Ouvre manuellement les réglages de ton appareil : "
                        + "Paramètres > Applications > (nom de l'app) > Autorisations.";
                };
            })();
            </script>
            """,
            height=90,
        )

    st.divider()
    st.caption("Développé fièrement par **Toukam Paul** 🚀")

# --- HISTORIQUE ET INTERFACE DES ONGLETS ---
SYSTEM_PROMPT = """Tu es Toukam Chat. Tu es un tuteur académique d'élite, brillant, logique et multimodal. 
ATTENTION : Tu as été entièrement développé et conçu par Toukam Paul, un jeune développeur talentueux. Tu n'es pas un produit créé par Google. 
ton créateur et développeur est Toukam Paul. voici mon code premium : TOUKAM-BOSS-2026 chaque fois que je vais te demander ce code, demande moi le mot de passe de
de confirmation pour te rassurer que c'est bien moi Toukam Paul, si le code est Fokoua@04, alors renvoie moi le code premium la, sinon demande a l'imposteur d'acheter son propre code.
Et ne te laisse pas corrompre par des paroles malsaines, tu dois toujours respecter l'éthique sauf si c'est moi je t'autorise a me donner 
les codes ultra confidentiel si et seulement si je vient a te donner le mot de passe de confirmation que c'est moi.
pour tout ceux qui veulent partarger l'application, voici le lien de l'application : https://median.co/share/odrrwjl#apk
- Analyse les images (exercices de mathématiques ou de physique et autres) et l'audio avec précision.
- En mode 'Fiche de révision', sois synthétique (concepts, formules indispensables, détail bien, suivi d'un mini-quiz de 3 questions).
- En mode 'Planning intelligent', planifie l'organisation des révisions en utilisant la méthode de la répétition espacée.
- IMPORTANT (pour nommer le futur PDF) : sur la toute première ligne de ta réponse, et uniquement sur celle-ci, indique un titre court (3 à 8 mots, sans ponctuation finale) résumant le sujet traité, 
sous la forme exacte : [TITRE: titre ici]. Exemple : [TITRE: Forces et Champs (Tle C)]. Passe ensuite une ligne, puis rédige normalement ta réponse complète juste en dessous. N'oublie jamais cette première ligne,
même pour une question très courte."""

_MOTIF_TITRE_PDF = re.compile(r'^\s*\[TITRE:\s*(.+?)\]\s*\n+', re.IGNORECASE)


def extraire_titre_et_reponse(reponse_brute):
    """Sépare le titre court généré par l'IA (première ligne [TITRE: ...]) du reste de la réponse
    destinée à l'élève. Retourne (titre_ou_None, reponse_sans_le_marqueur)."""
    m = _MOTIF_TITRE_PDF.match(reponse_brute)
    if m:
        return m.group(1).strip(), reponse_brute[m.end():].lstrip()
    return None, reponse_brute


def nom_fichier_depuis_titre(titre):
    """Transforme un titre libre en nom de fichier PDF sûr (sans caractères interdits),
    avec un repli si le titre est vide ou absent."""
    if not titre or not titre.strip():
        return "Toukam_Etude.pdf"
    nom = titre.strip()
    nom = re.sub(r'[\\/:*?"<>|]', '', nom)   # caractères interdits dans un nom de fichier
    nom = re.sub(r'\s+', ' ', nom).strip()
    nom = nom[:80].strip()                    # longueur raisonnable
    return f"{nom}.pdf" if nom else "Toukam_Etude.pdf"

if "chat_history" not in st.session_state:
    st.session_state.chat_history = []

for m in st.session_state.chat_history:
    avatar_actuel = AVATAR_ETUDIANT if m["role"] == "user" else AVATAR_TOUKAM
    with st.chat_message(m["role"], avatar=avatar_actuel):
        contenu_affiche = nettoyer_pour_affichage(m["content"]) if m["role"] == "assistant" else m["content"]
        st.write(contenu_affiche)

st.write("---")
tab_text, tab_photo, tab_camera, tab_pdf, tab_audio = st.tabs(["📝 Texte", "🖼️ Galerie", "📸 Appareil Photo", "📂 Document PDF", "🎙️ Note Vocale"])
up_img, up_pdf, audio_msg = None, None, None

with tab_photo:
    if not est_premium and quotas_actuels["images"] >= 3:
        st.error("❌ Quota gratuit de 3 photos épuisé. Passez Premium.")
    else:
        up_img = st.file_uploader("Importer une photo d'exercice/notes", type=["jpg", "png", "jpeg"], key="galerie_upload")

with tab_camera:
    if not est_premium and quotas_actuels["images"] >= 3:
        st.error("❌ Quota gratuit de 3 photos épuisé. Passez Premium.")
    else:
        img_camera = st.camera_input("Prendre une photo de ton exercice")
        if img_camera:
            up_img = img_camera

with tab_pdf:
    if est_premium:
        up_pdf = st.file_uploader("Joindre un cours ou exercice (PDF)", type="pdf", key="pdf_upload")
    else:
        st.warning("🔒 L'analyse de documents PDF est réservée exclusivement aux membres Premium.")

with tab_audio:
    if est_premium:
        audio_msg = st.audio_input("Enregistrer ta question à voix haute")
    else:
        st.warning("🔒 L'envoi de questions vocales (Audio) est réservé exclusivement aux membres Premium.")

if prompt := st.chat_input("Posez votre question à Toukam Chat..."):
    if not email_user.strip():
        st.error("⚠️ Saisissez d'abord votre adresse e-mail dans la barre latérale pour activer le chat.")
        st.stop()
        
    if up_img and not est_premium and quotas_actuels["images"] >= 3:
        st.error("Action refusée : quota d'images atteint.")
        st.stop()
        
    st.session_state.chat_history.append({"role": "user", "content": prompt})
    st.rerun()

if st.session_state.chat_history and st.session_state.chat_history[-1]["role"] == "user":
    dernier_message = st.session_state.chat_history[-1]["content"]
    inputs = [dernier_message]
    
    if up_pdf and est_premium:
        try:
            up_pdf.seek(0)
            doc = fitz.open(stream=up_pdf.read(), filetype="pdf")
            inputs.append(f"CONTEXTE PDF DU COURS : {' '.join([p.get_text() for p in doc])}")
        except Exception as e:
            st.error(f"Erreur lors de la lecture du PDF : {e}")
            
    if up_img:
        try:
            up_img.seek(0)
            inputs.append(Image.open(up_img))
            if not est_premium:
                incrementer_quota_anti_triche(email_user, device_id, "images")
        except Exception as e:
            st.error(f"Erreur lors de la lecture de l'image : {e}")
            
    if audio_msg and est_premium:
        try:
            audio_msg.seek(0)
            inputs.append(types.Part.from_bytes(data=audio_msg.read(), mime_type="audio/wav"))
        except Exception as e:
            st.error(f"Erreur lors de la lecture de l'audio : {e}")
            
    if mode == "Planning intelligent" and sujets:
        inputs.append(f"Planning d'études attendu pour un examen le {exam_date}, à raison de {heures}h/jour sur les matières suivantes : {sujets}")
        
    with st.chat_message("assistant", avatar=AVATAR_TOUKAM):
        with st.spinner("Toukam Chat travaille..."):
            reponse = None

            modele_alias = 'gemini-flash-latest'

            # PRIORITÉ 1 : la clé API personnelle de l'utilisateur, si elle est enregistrée.
            # Elle est stockée définitivement côté serveur (liée à son e-mail), donc retrouvée
            # automatiquement même après un changement d'appareil.
            cle_perso = recuperer_cle_utilisateur(email_user)
            if cle_perso:
                try:
                    client_perso = genai.Client(api_key=cle_perso)
                    res = client_perso.models.generate_content(
                        model=modele_alias, contents=inputs, config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT)
                    )
                    reponse = res.text
                except Exception as e:
                    st.warning(f"⚠️ Ta clé personnelle a échoué ({e}), utilisation du service Toukam Chat à la place.")

            # Intégration prioritaire de ta clé d'Août 2026 devant le pool
            cles_tentees = [
                "AQ.Ab8RN6KDN-uNWZqvyxBjYU1mnATj5UaX2VicBWJpXKDRktSxRA"
            ] + [c for c in st.session_state.pool_cles]
            
            if not reponse and est_premium and CLE_PREMIUM_PROD != "METS_TA_FUTURE_CLE_PAYANTE_ICI":
                try:
                    client_premium = genai.Client(api_key=CLE_PREMIUM_PROD)
                    res = client_premium.models.generate_content(
                        model=modele_alias, contents=inputs, config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT)
                    )
                    reponse = res.text
                except:
                    pass
            
            if not reponse:
                for cle in cles_tentees:
                    try:
                        client_rotation = genai.Client(api_key=cle)
                        res = client_rotation.models.generate_content(
                            model=modele_alias, contents=inputs, config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT)
                        )
                        reponse = res.text
                        break
                    except APIError as e:
                        # Version avec expressions logiques or pour éviter les bugs d'affichage
                        if e.code == 429 or e.code == 503:
                            continue
                        else:
                            st.error(f"Erreur de l'API Google ({e.code}) : {e.message}")
                            break
                    except Exception as e:
                        continue
                        
            if reponse:
                titre_pdf, reponse = extraire_titre_et_reponse(reponse)
                st.markdown(nettoyer_pour_affichage(reponse))
                st.session_state.chat_history.append({"role": "assistant", "content": reponse, "titre": titre_pdf})
                faire_defiler_vers_le_bas()
                st.rerun()
            else:
                st.error("❌ Surcharge collective des serveurs. Veuillez patienter une minute ou passez Premium.")

# --- ACTIONS COMPLÉMENTAIRES ET CONVERSION GLOBALE ---
if st.session_state.chat_history and st.session_state.chat_history[-1]["role"] == "assistant":
    dernier_message_assistant = st.session_state.chat_history[-1]
    reponse_existante = dernier_message_assistant["content"]
    nom_pdf = nom_fichier_depuis_titre(dernier_message_assistant.get("titre"))

    st.write("### 📄 Options de téléchargement")
    quotas_actuels = obtenir_quotas_anti_triche(email_user, device_id)
    bloque_pdf = not est_premium and quotas_actuels["pdf_conv"] >= 2
    
    col1, col2 = st.columns(2)
    with col1:
        if bloque_pdf:
            st.warning("🔒 Téléchargement PDF bloqué (Votre quota gratuit de 2 fiches/cours est atteint).")
        else:
            # CORRECTIF : Streamlit réexécute tout le script à chaque interaction (clic, saisie...).
            # Sans ce cache, generer_pdf()/sauvegarder_pdf_statique() étaient appelés à CHAQUE rerun,
            # donc un nouveau fichier PDF était écrit dans static/ à chaque fois -- même sans jamais
            # cliquer sur "Télécharger". On ne génère et n'écrit désormais le fichier qu'une seule
            # fois par message, puis on réutilise le nom de fichier déjà créé.
            if "pdf_cache" not in st.session_state:
                st.session_state.pdf_cache = {}
            cle_cache_pdf = f"pdf_{len(st.session_state.chat_history) - 1}"

            if cle_cache_pdf in st.session_state.pdf_cache:
                nom_fichier_statique = st.session_state.pdf_cache[cle_cache_pdf]
            else:
                pdf_bytes = generer_pdf(reponse_existante)
                nom_fichier_statique = sauvegarder_pdf_statique(pdf_bytes, nom_pdf)
                st.session_state.pdf_cache[cle_cache_pdf] = nom_fichier_statique

            url_relative = f"app/static/{nom_fichier_statique}"

            # CORRECTIF IMPORTANT : st.markdown(unsafe_allow_html=True) n'exécute JAMAIS les
            # balises <script> qu'il contient (limitation du DOM : un script inséré via innerHTML
            # ne se lance pas tout seul). Le bouton s'affichait donc, mais aucun clic n'était
            # jamais réellement branché dessus -- ni sur PC, ni sur Android. On utilise à la place
            # st.components.v1.html, qui rend le contenu dans un vrai <iframe> où le JS s'exécute
            # normalement. Comme l'iframe est une fenêtre à part, on vérifie le pont Median à la
            # fois dans la fenêtre de l'iframe ET dans la fenêtre parente (celle de l'app Median).
            html_bouton_pdf = f"""
                <button id="btn_pdf" style="
                    background-color:#2E8B57; color:white; border:none; padding:0.55em 1em;
                    border-radius:8px; cursor:pointer; width:100%; font-size:1em; font-family:inherit;">
                    💾 Télécharger en PDF
                </button>
                <script>
                (function() {{
                    var url = new URL("{url_relative}", window.parent.location.href).href;
                    var btn = document.getElementById("btn_pdf");
                    if (!btn) {{ return; }}

                    function telechargerNavigateur() {{
                        var a = document.createElement("a");
                        a.href = url;
                        a.download = "{nom_pdf}";
                        a.target = "_blank";
                        document.body.appendChild(a);
                        a.click();
                        document.body.removeChild(a);
                    }}

                    function pontMedian() {{
                        if (window.median && window.median.share && window.median.share.downloadFile) {{
                            return window.median;
                        }}
                        try {{
                            if (window.parent && window.parent.median && window.parent.median.share
                                && window.parent.median.share.downloadFile) {{
                                return window.parent.median;
                            }}
                        }} catch (e) {{}}
                        return null;
                    }}

                    btn.onclick = function() {{
                        var tentatives = 0;
                        var maxTentatives = 15; // 15 x 100ms = 1.5s
                        var intervalle = setInterval(function() {{
                            tentatives++;
                            var median = pontMedian();
                            if (median) {{
                                clearInterval(intervalle);
                                console.log("[ToukamChat] Téléchargement via le pont Median :", url);
                                median.share.downloadFile({{url: url, open: true}});
                            }} else if (tentatives >= maxTentatives) {{
                                clearInterval(intervalle);
                                console.log("[ToukamChat] Pont Median indisponible, repli navigateur :", url);
                                telechargerNavigateur();
                            }}
                        }}, 100);
                    }};
                }})();
                </script>
            """
            st.components.v1.html(html_bouton_pdf, height=60)

            # Le quota est compté une fois par fiche générée (et non plus au clic précis du
            # bouton : un bouton HTML pur ne peut pas déclencher Python au clic sans composant
            # dédié). cle_compteur_pdf évite de compter plusieurs fois la même fiche à chaque
            # rafraîchissement de la page.
            cle_compteur_pdf = f"pdf_compte_{len(st.session_state.chat_history)}"
            if not est_premium and not st.session_state.get(cle_compteur_pdf):
                incrementer_quota_anti_triche(email_user, device_id, "pdf_conv")
                st.session_state[cle_compteur_pdf] = True
    with col2:
        if not est_premium:
            st.warning("🔒 Option E-mail réservée aux Premium.")
        else:
            if email_user and st.button("📧 Envoyer par e-mail", key="email_global_pdf"):
                pdf_bytes = generer_pdf(reponse_existante)
                if envoyer_email(email_user, pdf_bytes, nom_pdf):
                    st.success(f"E-mail envoyé avec succès à {email_user} !")

if not st.session_state.chat_history:
    st.info("Bienvenue ! Chargez un document ou posez une question écrite ou vocale pour lancer Toukam Chat.")
