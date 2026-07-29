import streamlit as st
import os
import io
import smtplib
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

# --- CONFIGURATION ET MÉMOIRE (Toukam Chat) ---
st.set_page_config(page_title="Toukam Chat", page_icon="🎓", layout="wide")
st.title("🎓 Toukam Chat : Votre Tuteur IA d'Élite")

# Votre clé API valide
GEMINI_API_KEY = "AQ.Ab8RN6L1EhFEOMSJ32AbUjjQXHPU3dQIB6DfdTaXR3c89T7isg"
client = genai.Client(api_key=GEMINI_API_KEY)

# --- FONCTIONS TECHNIQUES ---

def generer_pdf(contenu):
    """Crée un PDF proprement à partir du texte de l'IA."""
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=12)
    texte_propre = contenu.encode('latin-1', 'replace').decode('latin-1')
    pdf.multi_cell(0, 10, texte_propre)
    return pdf.output(dest='S').encode('latin-1')

def envoyer_email(destinataire, fichier_pdf, nom_fichier):
    """Envoie la fiche ou le planning par e-mail."""
    expediteur = "paultoukam04@gmail.com"
    mot_de_pass = "VOTRE_MOT_DE_PASSE_APPLICATION" 
    
    msg = MIMEMultipart()
    msg['From'] = expediteur
    msg['To'] = destinataire
    msg['Subject'] = "🎓 Toukam Chat : Votre document d'étude"
    
    corps = "Bonjour ! Voici votre document généré par Toukam Chat pour vous aider dans vos études. Bonnes révisions !"
    msg.attach(MIMEText(corps, 'plain'))
    
    part = MIMEBase('application', 'octet-stream')
    part.set_payload(fichier_pdf)
    encoders.encode_base64(part)
    part.add_header('Content-Disposition', f"attachment; filename={nom_fichier}")
    msg.attach(part)
    
    try:
        server = smtplib.SMTP('://gmail.com', 587)
        server.starttls()
        server.login(expediteur, mot_de_pass)
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        st.error(f"Erreur technique lors de l'envoi de l'e-mail : {e}")
        return False

# --- INTERFACE SIDEBAR ---
with st.sidebar:
    st.header("⚙️ Paramètres Toukam")
    mode = st.radio("Mode de travail :", ["Aide aux exercices", "Fiche de révision", "Planning intelligent"])
    
    st.divider()
    up_pdf = st.file_uploader("Joindre un cours (PDF)", type="pdf")
    up_img = st.file_uploader("Photo d'exercice/notes", type=["jpg", "png", "jpeg"])
    audio_msg = st.audio_input("Question vocale")
    
    if mode == "Planning intelligent":
        st.info("Complétez pour votre planning :")
        exam_date = st.date_input("Date de l'examen")
        heures = st.slider("Heures/jour", 1, 12, 4)
        sujets = st.text_area("Matières et chapitres")

    st.divider()
    email_user = st.text_input("Votre e-mail pour l'envoi direct", value="paultoukam04@gmail.com")

# --- LOGIQUE IA ---
SYSTEM_PROMPT = """Tu es Toukam Chat. Tu es un tuteur académique d'élite, brillant, logique et multimodal.
- Analyse les images (exercices de mathématiques ou de physique) et l'audio avec précision.
- En mode 'Fiche de révision', sois synthétique (concepts, formules indispensables, suivi d'un mini-quiz de 3 questions).
- En mode 'Planning intelligent', planifie l'organisation des révisions en utilisant la méthode de la répétition espacée."""

if "chat_history" not in st.session_state:
    st.session_state.chat_history = []

for m in st.session_state.chat_history:
    st.chat_message(m["role"]).write(m["content"])

if prompt := st.chat_input("Posez votre question à Toukam Chat..."):
    st.session_state.chat_history.append({"role": "user", "content": prompt})
    st.chat_message("user").write(prompt)

    inputs = [prompt]
    if up_pdf: 
        doc = fitz.open(stream=up_pdf.read(), filetype="pdf")
        inputs.append(f"CONTEXTE PDF DU COURS : {' '.join([p.get_text() for p in doc])}")
    if up_img: 
        inputs.append(Image.open(up_img))
    if audio_msg: 
        inputs.append(types.Part.from_bytes(data=audio_msg.read(), mime_type="audio/wav"))
    if mode == "Planning intelligent" and sujets: 
        inputs.append(f"Planning d'études attendu pour un examen le {exam_date}, à raison de {heures}h/jour sur les matières suivantes : {sujets}")

    with st.chat_message("assistant"):
        with st.spinner("Toukam Chat travaille..."):
            
            # Utilisation de l'alias intelligent de Google pour sélectionner le modèle actif
            # Cela évite les erreurs 404 de modèles indisponibles ou dépréciés.
            modele_alias = 'gemini-flash-latest'
            
            try:
                res = client.models.generate_content(
                    model=modele_alias,
                    contents=inputs,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT
                    )
                )
                reponse = res.text
                st.markdown(reponse)
                st.session_state.chat_history.append({"role": "assistant", "content": reponse})

                if mode in ["Fiche de révision", "Planning intelligent"]:
                    pdf_bytes = generer_pdf(reponse)
                    col1, col2 = st.columns(2)
                    with col1:
                        st.download_button("💾 Télécharger PDF", data=pdf_bytes, file_name="Toukam_Etude.pdf")
                    with col2:
                        if email_user and st.button("📧 Envoyer par e-mail"):
                            if envoyer_email(email_user, pdf_bytes, "Toukam_Etude.pdf"):
                                st.success("E-mail envoyé avec succès !")
                                
            except APIError as e:
                st.error(f"Erreur de l'API Google : {e.message}")
            except Exception as e:
                st.error(f"Une erreur est survenue : {e}")

if not st.session_state.chat_history:
    st.info("Bienvenue ! Chargez un document ou posez une question écrite ou vocale pour lancer Toukam Chat.")
