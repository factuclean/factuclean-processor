# main.py
import os
import smtplib
import json
import pandas as pd
import io
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from email.mime import text
from typing import List, Dict, Any
from google import genai  # type: ignore

# --- 1. CONFIGURATION ET LECTURE DES SECRETS (.env) ---
load_dotenv()

# Variables d'environnement pour l'API et l'Email
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY") or ""  # Ajoutez :str=
SMTP_SERVER = os.getenv("SMTP_SERVER") or ""  # Ajoutez :str=
# Le port est converti en nombre entier
SMTP_PORT = int(os.getenv("SMTP_PORT") or 0)
SMTP_USER: str = os.getenv("SMTP_USER") or ""  # Ajoutez :str=
SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD") or ""  # Ajoutez :str=
SENDER_EMAIL: str = os.getenv("SENDER_EMAIL") or ""  # Ajoutez :str=

# Initialisation du client Gemini
# Lignes de vérification de la clé commentées pour éviter un plantage sur Render
# if not GEMINI_API_KEY:
#     raise ValueError("GEMINI_API_KEY non trouvé dans le fichier .env.")

ai_client = genai.Client(api_key=GEMINI_API_KEY)
# Correction de la syntaxe de la description FastAPI
app = FastAPI(title="FactuClean Processor API", description="API pour traiter les soumissions Tally")


# Endpoint de test pour vérifier le routage (à supprimer après le test final)
@app.get("/")
def read_root():
    # Correction de la syntaxe du message (guillemet)
    return {"status": "ok", "message": "API is running"}


# --- 2. MODÈLE DE DONNÉES (POUR RECEVOIR LES DONNÉES DE TALLY) ---
class TallySubmission(BaseModel):
    # Modèle des données reçues du Webhook Tally.
    data: Dict[str, Any]


# --- 3. FONCTION DE TRAITEMENT DE L'IA ---
def analyze_invoice_with_gemini(invoice_url: str) -> Dict[str, str]:
    # Utilise le modèle Gemini pour extraire les informations clés d'une facture.

    # Structure de données attendue en JSON par Gemini
    json_schema = {
        "type": "object",
        "properties": {
            "nom_fournisseur": {"type": "string"},
            "date_facture": {"type": "string", "description": "Format AAAA-MM-JJ"},
            "montant_ht": {"type": "string", "description": "Montant hors taxes"},
            "montant_tva": {"type": "string", "description": "Montant de la TVA"},
            "montant_total": {"type": "string", "description": "Montant total TTC"},
            "devise": {"type": "string", "description": "Ex: EUR, USD"}
        },
        "required": ["nom_fournisseur", "date_facture", "montant_total"]
    }

    prompt = (
        "Vous êtes un expert en extraction de données financières. "
        "Extrayez les informations demandées de la facture à l'URL suivante: "
        f"{invoice_url}. Répondez uniquement avec un objet JSON valide et complet."
    )

    try:
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config={"response_mime_type": "application/json", "response_schema": json_schema}
        )

        # Le contenu est une chaîne JSON
        return json.loads(response.text)

    except Exception as e:
        print(f"Erreur d'analyse Gemini: {e}")
        return {"error": f"Analyse échouée pour l'URL {invoice_url}", "details": str(e)}


# --- 4. FONCTION D'ATTACHEMENT EXCEL ---
def create_excel_attachment(data_list: List[Dict[str, Any]]) -> bytes:
    """
    Convertit une LISTE de données extraites en un fichier Excel (XLSX) en mémoire.
    """
    # Crée un DataFrame avec toutes les lignes extraites
    df = pd.DataFrame(data_list)
    output = io.BytesIO()

    df.to_excel(output, index=False, sheet_name='Factures_Analysees')  # type:ignore

    # Rembobine le pointeur pour lire le contenu
    output.seek(0)
    return output.read()


# --- 5. FONCTION D'ENVOI D'EMAIL ---
def send_email_with_attachment(recipient_email: str, excel_bytes: bytes, invoice_count: int):
    # Envoie un email avec le fichier Excel en pièce jointe.

    msg = MIMEMultipart()
    msg['From'] = SENDER_EMAIL  # type: ignore
    msg['To'] = recipient_email
    msg['Subject'] = f"FactuClean - Rapport d'analyse pour {invoice_count} facture(s)"

    body = (
        f"Bonjour,\n\n"
        f"Veuillez trouver ci-joint le fichier Excel contenant les données" 
        f"extraites de vos {invoice_count} factures.\n\n"
        f"Cordialement,\n"
        f"L'équipe FactuClean"
    )
    # L'objet 'email' est importé plus haut (L.11)
    msg.attach(text.MIMEText(body, 'plain'))

    # Crée la pièce jointe Excel
    part = MIMEBase('application', 'octet-stream')
    part.set_payload(excel_bytes)
    encoders.encode_base64(part)
    part.add_header('Content-Disposition', 'attachment; filename="Factures_Analysees.xlsx"')
    msg.attach(part)

    try:
        # Connexion au serveur SMTP
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SENDER_EMAIL, recipient_email, msg.as_string())
        print(f"Email envoyé avec succès à {recipient_email}")

    except Exception as e:
        print(f"Échec de l'envoi de l'email: {e}")
        raise HTTPException(status_code=500, detail=f"Erreur d'envoi d'email: {e}")


# --- 6. ENDPOINT WEBHOOK ---
@app.post("/webhook_tally")
async def webhook_tally(submission: TallySubmission):
    # 1. Extraction des URLs de factures et de l'email
    try:
        recipient_email = submission.data.get("email_destinataire")
        file_urls = submission.data.get("fichiers")

        if not recipient_email or not file_urls:
            raise ValueError("Email ou URLs de fichiers manquants dans les données Tally.")

        if not isinstance(file_urls, list):
            file_urls = [file_urls]

    except Exception as e:
        print(f"Erreur de données Tally: {e}")
        raise HTTPException(status_code=400, detail=f"Données Tally mal formées: {e}")

    # 2. Traitement par l'IA de chaque facture
    extracted_data = []
    for url in file_urls:
        print(f"Analyse de l'URL: {url}")
        data = analyze_invoice_with_gemini(url)  # type: ignore
        data['url_facture'] = url  # type: ignore
        extracted_data.append(data)

    # 3. Création du fichier Excel
    if not extracted_data:
        raise HTTPException(status_code=500, detail="Aucune donnée extraite par l'IA.")

    excel_bytes = create_excel_attachment(extracted_data)

    # 4. Envoi de l'email
    send_email_with_attachment(recipient_email, excel_bytes, len(file_urls))

    return {"message": f"Factures analysées et email envoyé à {recipient_email}", "count": len(file_urls)}
