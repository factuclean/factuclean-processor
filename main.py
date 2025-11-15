# main.py
import os
import smtplib
import json
import pandas as pd
import io
import time
from datetime import datetime

from pydantic import BaseModel
from dotenv import load_dotenv

from fastapi import FastAPI, HTTPException
from google import genai  # type: ignore

from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders

from typing import List, Dict, Any

# --- 1. CONFIGURATION ET LECTURE DES SECRETS (.env) ---
load_dotenv()

# Variables d'environnement pour l'API et l'Email
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY") or ""
SMTP_SERVER: str = os.getenv("SMTP_SERVER") or ""
SMTP_PORT = int(os.getenv("SMTP_PORT") or 0)
SMTP_USER: str = os.getenv("SMTP_USER") or ""
SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD") or ""
SENDER_EMAIL: str = os.getenv("SENDER_EMAIL") or ""

# Initialisation du client Gemini
# Lignes de vérification de la clé commentées pour éviter un plantage sur Render sans clé
# if not GEMINI_API_KEY:
#     raise ValueError("GEMINI_API_KEY non trouvé dans le fichier .env")
client = genai.Client(api_key=GEMINI_API_KEY)


# --- 2. DÉFINITION DU SCHÉMA PYLINT (Tally Submission) ---

class TallySubmission(BaseModel):
    data: Dict[str, Any]


# --- 3. DÉFINITION DES FONCTIONS ---

def send_email_with_attachment(recipient_email: str, excel_bytes: bytes, invoice_count: int):
    """Envoie l'e-mail avec le fichier Excel généré."""

    msg = MIMEMultipart()

    # Correction des avertissements None (avec type: ignore)
    msg['From'] = SENDER_EMAIL  # type: ignore
    msg['To'] = recipient_email
    msg['Subject'] = f"FactuClean - Rapport d'analyse pour {invoice_count} facture(s)"

    # Corps de l'e-mail (Correction PEP 8 L. 119)
    body = (
        f"Bonjour,\n\n"
        f"Veuillez trouver ci-joint le fichier Excel contenant les données "
        f"extraites de vos {invoice_count} factures.\n\n"
        f"Cordialement,\n"
        f"L'équipe FactuClean"
    )

    # Ajout du corps au message
    msg.attach(MIMEText(body, _subtype='plain'))

    # Crée la pièce jointe Excel
    part = MIMEBase(_maintype='application', _subtype='octet-stream')
    part.set_payload(excel_bytes)
    encoders.encode_base64(part)

    # Ajout des métadonnées de l'en-tête
    filename = f"Rapport_Factures_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    part.add_header(
        'Content-Disposition',
        f'attachment; filename="{filename}"',
    )
    msg.attach(part)

    # Envoi via SMTP
    try:
        # Utilisation de SMTP_SSL pour une connexion sécurisée
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:  # type: ignore
            server.login(SMTP_USER, SMTP_PASSWORD)  # type: ignore
            server.sendmail(SENDER_EMAIL, recipient_email, msg.as_string())  # type: ignore
    except Exception as e:
        print(f"Erreur d'envoi d'e-mail: {e}")
        raise HTTPException(status_code=500, detail=f"Échec de l'envoi d'e-mail: {e}")


def create_excel_attachment(data: List[Dict[str, Any]]) -> bytes:
    """Crée un fichier Excel en mémoire à partir des données."""

    # Structure de données pour s'assurer que même les erreurs apparaissent
    df = pd.DataFrame(data)

    # Crée un buffer de bytes en mémoire
    output = io.BytesIO()

    # Écrit le DataFrame dans le buffer
    df.to_excel(output, index=False, sheet_name='Factures_Analysees')  # type: ignore

    # Rembobine le pointeur du buffer au début
    output.seek(0)

    # Retourne le contenu binaire
    return output.read()


def analyze_invoice_with_gemini(invoice_url: str) -> Dict[str, Any]:
    """Analyse une facture à partir d'une URL et retourne un dictionnaire."""

    prompt = (
        f"Vous êtes un analyseur de factures expert. Extrayez les informations suivantes de cette facture: "
        f"nom_fournisseur, date_facture (format AAAA-MM-JJ), montant_HT (en float), montant_TVA (en float), "
        f"montant_TTC (en float), devise. Retournez les données EXCLUSIVEMENT sous forme d'objet JSON. "
        f"Si une information manque, utilisez 'null' (sans guillemets) pour la valeur. URL: {invoice_url}"
    )

    try:
        # Tentative d'analyse avec l'URL
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[prompt, invoice_url]
        )

        # Cherche la première instance d'objet JSON dans la réponse
        json_string = response.text.strip()

        # Le modèle peut entourer le JSON de json...
        if json_string.startswith('json'):
            json_string = json_string.strip('json').strip('```').strip()

        # Parse le JSON
        data = json.loads(json_string)

        return data

    except json.JSONDecodeError as e:
        print(f"Erreur de décodage JSON pour {invoice_url}: {e}")
        return {"error": "JSON malformé par l'IA", "details": str(e)}
    except Exception as e:
        print(f"Erreur d'analyse Gemini: {e}")
        return {"error": f"Analyse échouée pour l'URL {invoice_url}", "details": str(e)}


# --- 4. DÉFINITION DE L'API ET DES ENDPOINTS ---

app = FastAPI(title="FactuClean AI Processor")


@app.get("/")
async def root():
    """Endpoint de test pour vérifier si l'API est en cours d'exécution."""
    return {"status": "ok", "message": "API is running"}


@app.post("/webhook_tally")
async def webhook_tally(submission: TallySubmission):
    """
    Reçoit la soumission de Tally, analyse les factures et envoie le rapport par e-mail.
    """

    # 1. Extraction des données de soumission
    recipient_email = submission.data.get("email_du_client")
    file_urls = submission.data.get("fichiers_factures", [])

    if not recipient_email or not file_urls:
        raise HTTPException(
            status_code=400,
            detail="Données de soumission manquantes (e-mail ou URLs de fichiers)."
        )

    extracted_data = []

    # 2. Traitement par l'IA de chaque facture
    for url in file_urls:
        print(f"Analyse de l'URL: {url}")

        data = analyze_invoice_with_gemini(url)  # type: ignore

        data['url_facture'] = url  # type: ignore

        extracted_data.append(data)

        # Ajoutez un petit délai pour éviter les limites de débit d'API
        time.sleep(1)

        # 3. Création du fichier Excel
    excel_bytes = create_excel_attachment(extracted_data)
    invoice_count = len(file_urls)

    # 4. Envoi de l'e-mail
    send_email_with_attachment(recipient_email, excel_bytes, invoice_count)

    return {"status": "success", "message": f"Rapport pour {invoice_count} facture(s) envoyé à {recipient_email}."}
