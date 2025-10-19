import os
import shutil
import tempfile
import uuid
import json
from fastapi import FastAPI, UploadFile, File, HTTPException, Request 
from fastapi.responses import FileResponse
from fastapi.templating import Jinja2Templates 
from pydantic import BaseModel, Field
import google.generativeai as genai

# Gestione dell'errore per compatibilità API SDK di Gemini
try:
    from google.generativeai.errors import APIError
except ImportError:
    try:
        from google.generativeai import APIError
    except ImportError:
        class APIError(Exception):
            pass

# --- Configurazione e Inizializzazione ---
app = FastAPI()
# Assicurati che 'index.html' sia nella stessa cartella di main.py
templates = Jinja2Templates(directory=".") 

# --- DATI ESSENZIALI (URL REALI) ---
WIX_LOGIN_URL = 'https://luxailegal.wixsite.com/my-site-9'
HOME_URL = 'https://luxailegal.wixsite.com/my-site-9'

# --- SIMULAZIONE DATABASE ---
USERS_DB = {
    "user_wix_demo": {"is_logged_in": True, "is_paying_member": True, "email": "test-demo@lux-ai.com"}, 
    "ANONIMO": {"is_logged_in": False, "is_paying_member": False, "email": None},
}

# --- Modelli Pydantic per la Risposta (Sincrona) ---
class ResultResponse(BaseModel):
    status: str = Field(..., description="Stato dell'analisi: success, failed")
    result: dict | None = None
    detail: str | None = None

# --- UTILITY: Recupera l'Utente Corrente (SIMULAZIONE SSO) ---
def get_current_user_id(request: Request) -> str:
    auth_token = request.cookies.get("auth_token") 
    
    if auth_token == "demo_token" or auth_token == "paid_token":
        return "user_wix_demo"
    
    return "ANONIMO" 

# ----------------------------------------------------------------------------------
# 🛑 LOGICA AI: COPIATA DAL VECCHIO WORKER (SINCRONA)
# ----------------------------------------------------------------------------------

# Funzione Helper per l'Inizializzazione Gemini
def get_gemini_client():
    api_key = os.getenv("GEMINI_API_KEY") 
    if not api_key:
        raise ValueError("GEMINI_API_KEY non trovata. Impossibile configurare l'AI.")
    
    # FIX PER L'ERRORE 'has no attribute Client'
    # Usiamo genai.Client() solo se disponibile (nuovo SDK)
    # Altrimenti, usiamo genai.configure() (vecchio SDK)
    if hasattr(genai, 'Client'):
        return genai.Client(api_key=api_key)
    else:
        # Per le versioni SDK più vecchie, configuriamo globalmente
        genai.configure(api_key=api_key)
        # E restituiamo un oggetto che usa le funzioni a livello di modulo
        return genai

# Schema JSON per garantire l'output strutturato dall'AI
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "nome_documento": {"type": "string", "description": "Nome del file analizzato."},
        "risparmio_stimato_eur": {"type": "number", "description": "Valore in EUR (es. 60.00)."},
        "tempo_risparmiato_min": {"type": "integer", "description": "Tempo risparmiato in minuti (es. 15)."},
        "riassunto_breve": {"type": "string", "description": "Breve riassunto dei rischi trovati (massimo 2 frasi)."},
        "analisi_clausole": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tipo": {"type": "string"},
                    "rischio": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW", "NON TROVATA"]},
                    "testo_clausola_esatta": {"type": "string", "description": "La clausola esatta dal documento o stringa vuota se NON TROVATA."}
                },
                "required": ["tipo", "rischio", "testo_clausola_esatta"]
            }
        }
    },
    "required": ["nome_documento", "risparmio_stimato_eur", "tempo_risparmiato_min", "riassunto_breve", "analisi_clausole"]
}

# Funzione che esegue l'analisi AI
async def run_gemini_analysis(temp_path: str, filename: str):
    gemini_client_or_module = None
    uploaded_file = None
    output_text = ""
    
    try:
        gemini_client_or_module = get_gemini_client()
        
        # 1. Carica il file su Gemini (usa la funzione corretta a seconda dell'oggetto)
        if hasattr(gemini_client_or_module, 'files'): # Nuovo SDK
            uploaded_file = gemini_client_or_module.files.upload(file=temp_path)
        else: # Vecchio SDK (usa la funzione a livello di modulo)
            uploaded_file = genai.upload_file(file=temp_path)


        # 2. Prompt focalizzato
        prompt = f"""
            Analizza il documento PDF fornito. Il tuo obiettivo è concentrarti SOLO su due aree critiche:
            1. Clausole di Limitazione di Responsabilità (Liability Caps).
            2. Clausole di Indennizzo (Indemnification).
            Estrai i risultati in formato JSON. Per il ROI, assumi che l'avvocato medio risparmi 15 minuti di revisione per queste clausole.
            Popola il campo "nome_documento" con "{filename}". Rispondi ESCLUSIVAMENTE con l'oggetto JSON richiesto.
            """
        
        # 3. Generazione del Contenuto con Schema Enforced
        
        # Usa la funzione di generazione corretta a seconda dell'oggetto
        if hasattr(gemini_client_or_module, 'models'): # Nuovo SDK
            response = gemini_client_or_module.models.generate_content(
                model='gemini-2.5-flash',
                contents=[prompt, uploaded_file],
                config={
                    "response_mime_type": "application/json",
                    "response_schema": OUTPUT_SCHEMA
                }
            )
        else: # Vecchio SDK (usa la funzione a livello di modulo)
             response = genai.generate_content(
                model='gemini-2.5-flash',
                contents=[prompt, uploaded_file],
                config={
                    "response_mime_type": "application/json",
                    "response_schema": OUTPUT_SCHEMA
                }
            )


        # 4. Decodifica e Pulizia (Il testo della risposta dovrebbe essere un JSON puro)
        output_text = response.text.strip()
        json_result = json.loads(output_text)

        # 5. Pulizia del file da Gemini (usa la funzione corretta)
        if hasattr(gemini_client_or_module, 'files'): # Nuovo SDK
            gemini_client_or_module.files.delete(name=uploaded_file.name)
        else: # Vecchio SDK
            genai.delete_file(name=uploaded_file.name)
        
        return json_result

    except APIError as e:
        raise HTTPException(status_code=500, detail=f"Errore API Gemini: Verifica chiave/quota: {e}")
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"Errore di decodifica JSON dall'AI. L'output era malformato.")
    except Exception as e:
        # Pulizia in caso di errore generico
        if uploaded_file:
            try: 
                if hasattr(gemini_client_or_module, 'files'):
                    gemini_client_or_module.files.delete(name=uploaded_file.name)
                else:
                    genai.delete_file(name=uploaded_file.name)
            except: pass
        raise HTTPException(status_code=500, detail=f"Errore Fatale durante l'analisi: {e}")
    finally:
        # Pulizia del file temporaneo locale
        if os.path.exists(temp_path):
            os.remove(temp_path)

# ----------------------------------------------------------------------------------
#                                 ROTTE FASTAPI
# ----------------------------------------------------------------------------------

# --- ROTTA PRINCIPALE: SERVE L'HTML DINAMICO ---
@app.get("/")
async def read_index(request: Request):
    # Forziamo a True per l'MVP accessibile
    return templates.TemplateResponse(
        "index.html", 
        {
            "request": request,
            "is_logged_in": "true", 
            "is_paying_member": "true", 
        }
    )

# --- ROTTA: Esegue l'Analisi Sincrona ---
@app.post("/analyze", response_model=ResultResponse)
async def analyze_pdf_api(file: UploadFile = File(...)):
    
    # 1. Validazione del file
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Il file deve essere un PDF (application/pdf).")
    
    file.file.seek(0, os.SEEK_END)
    file_size = file.file.tell()
    file.file.seek(0)
    
    if file_size < 1000:
        raise HTTPException(status_code=400, detail="Impossibile analizzare. Il file è troppo piccolo o vuoto.")

    # 2. Salvataggio del file temporaneo
    temp_filename = f"{uuid.uuid4()}.pdf"
    temp_path = os.path.join("/tmp", temp_filename) 

    try:
        with open(temp_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
    except Exception as e:
        raise HTTPException(status_code=500, detail="Errore I/O: Impossibile salvare il file temporaneo.")
    finally:
        file.file.close()

    # 3. Esecuzione Sincrona dell'analisi AI
    try:
        analysis_result = await run_gemini_analysis(temp_path, file.filename)
        
        # 4. Restituiamo il risultato finale
        return ResultResponse(status="success", result=analysis_result, detail="Analisi completata con successo.")
        
    except HTTPException:
        # Rilancia le eccezioni HTTP interne
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Errore di servizio FATALE: {e}")