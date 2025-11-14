# main.py

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, EmailStr
from dotenv import load_dotenv
from google import genai
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import json
import pandas as pd
import io
from typing import List  # NOUVEL IMPORT pour gérer les listes d'URLs

# --- 1. CONFIGURATION ET LECTURE DES SECRETS (.env) ---
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SMTP_SERVER = os.getenv("SMTP_SERVER")
# Le port doit être converti en nombre entier (int)
SMTP_PORT = int(os.getenv("SMTP_PORT"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
SENDER_EMAIL = os.getenv("SENDER_EMAIL")

# Initialisation du client Gemini
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY non trouvé dans le fichier .env.")

ai_client = genai.Client(api_key=GEMINI_API_KEY)
app = FastAPI(title="FactuClean Processor API", description="API pour traiter les soumissions Tally avec Gemini.")


# --- 2. MODÈLE DE DONNÉES (POUR RECEVOIR LES DONNÉES DE TALLY) ---

class TallySubmission(BaseModel):
    """
    Modèle des données reçues du Webhook Tally.
    Utilise List[str] pour accepter plusieurs URLs de factures.
    """
    invoice_urls: List[str]  # MODIFIÉ pour accepter une liste d'URLs
    client_email: EmailStr
    client_name: str


# --- 3. FONCTIONS CŒUR DE L'AUTOMATISATION ---

def extract_data_with_ai(image_url: str) -> dict:
    """
    Utilise Gemini pour analyser l'URL de l'image de la facture
    et extrait les données au format JSON.
    """
    try:
        # Prompt en français pour extraire les données et formater en JSON strict
        prompt = f"""
        Analyze the invoice image provided by the URL: {image_url}. 
        Extract the following data fields. Respond ONLY with a valid JSON object.

        Fields to extract (use English keys):
        - 'invoice_number': (String) Numéro de la facture.
        - 'total_amount_ht': (Float) Montant Total Hors Taxes.
        - 'vat_amount': (Float) Montant Total de la TVA.
        - 'total_amount_ttc': (Float) Montant Total Toutes Taxes Comprises.
        - 'emission_date': (String) Date d'émission de la facture (format YYYY-MM-DD si possible).
        - 'supplier_name': (String) Nom du fournisseur.

        If a field is not found, use 0 for float values and 'N/A' for string values.
        """

        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[prompt]
        )

        # Nettoyage et chargement du JSON
        json_str = response.text.strip().replace("json", "").replace("", "")
        return json.loads(json_str)

    except Exception as e:
        print(f"Erreur lors de l'extraction IA: {e}")
        raise HTTPException(status_code=500, detail="Erreur dans le traitement par l'IA.")


def create_excel_attachment(data_list: List[dict]) -> bytes:
    """
    Convertit une LISTE de données extraites en un fichier Excel (XLSX) en mémoire.
    Utilise 'openpyxl' comme moteur.
    """
    try:
        # Crée un DataFrame avec toutes les lignes extraites
        df = pd.DataFrame(data_list)
        output = io.BytesIO()

        # Utilise le moteur openpyxl
        df.to_excel(output, engine='openpyxl', index=False, sheet_name='Factures_Analysees')

        # Remet le pointeur au début pour la lecture des bytes
        output.seek(0)

        return output.getvalue()
    except Exception as e:
        print(f"Erreur lors de la création Excel: {e}")
        # Assurez-vous d'avoir 'pip install openpyxl'
        raise HTTPException(status_code=500, detail="Erreur lors de la création du fichier Excel.")


def send_email_with_attachment(recipient: EmailStr, client_name: str, excel_content: bytes):
    """
    Envoie un e-mail avec le fichier Excel en pièce jointe via SendGrid (SMTP).
    """
    try:
        # Création du message
        msg = MIMEMultipart()
        msg['From'] = SENDER_EMAIL
        msg['To'] = recipient
        msg['Subject'] = f"✅ Votre analyse de facture FactuClean est prête, {client_name} !"

        body = f"""g
        Bonjour {client_name},

        Votre facture(s) a été traitée(s) avec succès par l'IA de FactuClean.
        Vous trouverez ci-joint le fichier Excel compilant toutes les données clés extraites.

        Merci d'utiliser FactuClean !
        """
        msg.attach(MIMEBase('text', 'plain', payload=body.encode('utf-8'), _charset='utf-8'))

        # Attacher le fichier Excel
        part = MIMEBase('application', 'octet-stream')
        part.set_payload(excel_content)
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', 'attachment', filename=f"Factures_Analysees_{client_name}.xlsx")
        msg.attach(part)

        # Connexion et envoi via SMTP (SendGrid)
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SENDER_EMAIL, recipient, msg.as_string())

    except Exception as e:
        print(f"Erreur lors de l'envoi d'e-mail: {e}")
        raise HTTPException(status_code=500, detail=f"Échec de l'envoi d'e-mail: {e}")


# --- 4. ENDPOINT FASTAPI ---

@app.post("/tally-processor")
async def process_tally_submission(submission: TallySubmission):
    """
    Reçoit les données de Tally (Liste d'URLs de factures et Email du client).
    1. Extrait les données de chaque facture via Gemini (en boucle).
    2. Compile les résultats.
    3. Crée le fichier Excel unique.
    4. Envoie le fichier Excel par e-mail au client.
    """

    all_extracted_data = []

    # BOUCLE SUR CHAQUE URL DE FACTURE SOUMISE
    for invoice_url in submission.invoice_urls:
        extracted_data = extract_data_with_ai(invoice_url)

        # Ajout des données client à l'objet pour chaque ligne de l'Excel
        extracted_data['Client_Email'] = submission.client_email
        extracted_data['Client_Name'] = submission.client_name

        all_extracted_data.append(extracted_data)

    # Vérification que l'extraction n'a pas retourné une liste vide
    if not all_extracted_data:
        return {"status": "warning", "message": "Aucune URL de facture soumise pour le traitement."}

    # Création du fichier Excel unique à partir de TOUTES les données compilées
    excel_file_bytes = create_excel_attachment(all_extracted_data)

    # Envoi de l'e-mail
    send_email_with_attachment(
        recipient=submission.client_email,
        client_name=submission.client_name,
        excel_content=excel_file_bytes
    )

    return {"status": "success",
            "message": f"Traitement de {len(all_extracted_data)} facture(s) terminé et envoyé à {submission.client_email}"}