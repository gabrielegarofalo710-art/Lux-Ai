import os
import shutil
import tempfile
import uuid
import json
from fastapi import FastAPI, UploadFile, File, HTTPException, Request 
from fastapi.responses import FileResponse
from fastapi.templating import Jinja2Templates 
from pydantic import BaseModel, Field

import google.genai as genai 

# Gestione dell'errore per compatibilità API SDK di Gemini
try:
    from google.genai.errors import APIError
except ImportError:
    try:
        from google.genai import APIError
    except ImportError:
        class APIError(Exception):
            pass

# --- Configurazione e Inizializzazione (OMESSE per brevità, sono invariate) ---
app = FastAPI()
templates = Jinja2Templates(directory=".") 

WIX_LOGIN_URL = 'https://luxailegal.wixsite.com/my-site-9'
HOME_URL = 'https://luxailegal.wixsite.com/my-site-9'

USERS_DB = {
    "user_wix_demo": {"is_logged_in": True, "is_paying_member": True, "email": "test-demo@lux-ai.com"}, 
    "ANONIMO": {"is_logged_in": False, "is_paying_member": False, "email": None},
}

class ResultResponse(BaseModel):
    status: str = Field(..., description="Stato dell'analisi: success, failed")
    result: dict | None = None
    detail: str | None = None

def get_current_user_id(request: Request) -> str:
    auth_token = request.cookies.get("auth_token") 
    
    if auth_token == "demo_token" or auth_token == "paid_token":
        return "user_wix_demo"
    
    return "ANONIMO" 

def get_gemini_client():
    api_key = os.getenv("GEMINI_API_KEY") 
    if not api_key:
        raise ValueError("GEMINI_API_KEY non trovata. Impossibile configurare l'AI.")
    
    if hasattr(genai, 'Client'):
        return genai.Client(api_key=api_key)
    else:
        genai.configure(api_key=api_key)
        return genai

# Schema JSON aggiornato con Motivazione e Suggerimenti
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "nome_documento": {"type": "string", "description": "Nome del file analizzato."},
        "risparmio_stimato_eur": {"type": "number", "description": "Valore in EUR (es. 60.00)."},
        "tempo_risparmiato_min": {"type": "integer", "description": "Tempo risparmiato in minuti (es. 15)."},
        "riassunto_breve": {"type": "string", "description": "Breve riassunto dei rischi trovati (massimo 2 frasi)."},
        "tempo_lettura_avvocato_stimato_min": {"type": "integer", "description": "Tempo stimato in minuti per un avvocato per leggere il documento e calcolare i rischi di liability/indemnification (es. 30)."},
        "rischio_complessivo_percentuale": {"type": "integer", "description": "Percentuale di rischio complessivo del documento, da 0 a 100 (es. 65)."},
        # NUOVI CAMPI RICHIESTI
        "motivazione_rischio_complessivo": {"type": "string", "description": "Breve spiegazione (1 frase) del perché è stato assegnato quel livello di rischio."},
        "suggerimenti_ai": {"type": "string", "description": "Consigli proattivi (1-2 frasi) per migliorare il contratto."},
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
    "required": [
        "nome_documento", 
        "risparmio_stimato_eur", 
        "tempo_risparmiato_min", 
        "riassunto_breve", 
        "tempo_lettura_avvocato_stimato_min", 
        "rischio_complessivo_percentuale", 
        "motivazione_rischio_complessivo", # AGGIUNTO
        "suggerimenti_ai",                 # AGGIUNTO
        "analisi_clausole"
    ]
}

# Funzione che esegue l'analisi AI
async def run_gemini_analysis(temp_path: str, filename: str):
    gemini_client_or_module = None
    uploaded_file = None
    output_text = ""
    
    try:
        gemini_client_or_module = get_gemini_client()
        
        if hasattr(gemini_client_or_module, 'files'): 
            uploaded_file = gemini_client_or_module.files.upload(file=temp_path)
        else: 
            uploaded_file = genai.upload_file(file=temp_path)


        # PROMPT AGGIORNATO per tutti i 5 punti richiesti
        prompt = f"""
            Analizza il documento PDF fornito. Il tuo obiettivo è concentrarti SOLO su due aree critiche:
            1. Clausole di Limitazione di Responsabilità (Liability Caps).
            2. Clausole di Indennizzo (Indemnification).
            
            Estrai i risultati in formato JSON.
            
            **Per il ROI e il Tempo (Punto 4):**
            - Stima il "tempo_lettura_avvocato_stimato_min" basandoti sulla complessità e lunghezza del documento. Considera che un avvocato esperto impiega mediamente 3 minuti per pagina per analizzare queste specifiche clausole critiche.
            - Calcola il "tempo_risparmiato_min" sottraendo 5 minuti (tempo dell'AI) dal "tempo_lettura_avvocato_stimato_min". Se il risultato è negativo, usa il valore fittizio **12** (come richiesto per la demo).
            - Stima il "risparmio_stimato_eur" usando un costo medio orario dell'avvocato di 150 EUR/ora (2.5 EUR/minuto) moltiplicato per il "tempo_risparmiato_min". Se il risultato è troppo basso, usa il valore fittizio **45.00** (come richiesto per la demo).

            **Per la Percentuale di Rischio e Motivazione (Punti 1 e 2):**
            - Calcola una "rischio_complessivo_percentuale" da 0 a 100. (es. se trova HIGH, usa 75; se trova solo LOW, usa 25).
            - Genera la "motivazione_rischio_complessivo" in una singola frase che spieghi il punteggio.
            
            **Per i Suggerimenti (Punto 3 e 5):**
            - Genera "suggerimenti_ai" con 1 o 2 frasi di consiglio proattivo (es. "Considera l’aggiunta di una clausola di indennizzo o di manleva...").

            Popola il campo "nome_documento" con "{filename}".
            Rispondi ESCLUSIVAMENTE con l'oggetto JSON richiesto secondo lo schema.
            """
        
        if hasattr(gemini_client_or_module, 'models'): 
            response = gemini_client_or_module.models.generate_content(
                model='gemini-2.5-flash',
                contents=[prompt, uploaded_file],
                config={"response_mime_type": "application/json", "response_schema": OUTPUT_SCHEMA}
            )
        else:
             response = genai.generate_content(
                model='gemini-2.5-flash',
                contents=[prompt, uploaded_file],
                config={"response_mime_type": "application/json", "response_schema": OUTPUT_SCHEMA}
            )

        output_text = response.text.strip()
        json_result = json.loads(output_text)

        if hasattr(gemini_client_or_module, 'files'): 
            gemini_client_or_module.files.delete(name=uploaded_file.name)
        else: 
            genai.delete_file(name=uploaded_file.name)
        
        return json_result

    except APIError as e:
        raise HTTPException(status_code=500, detail=f"Errore API Gemini: Verifica chiave/quota: {e}")
    except json.JSONDecodeError as e:
        print(f"Errore di decodifica JSON. Output AI RAW: {output_text}") 
        raise HTTPException(status_code=500, detail=f"Errore di decodifica JSON dall'AI. L'output era malformato o vuoto. Output: {output_text[:200]}...")
    except Exception as e:
        if uploaded_file:
            try: 
                if hasattr(gemini_client_or_module, 'files'):
                    gemini_client_or_module.files.delete(name=uploaded_file.name)
                else:
                    genai.delete_file(name=uploaded_file.name)
            except: pass
        raise HTTPException(status_code=500, detail=f"Errore Fatale durante l'analisi: {e}")
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

# --- ROTTE FASTAPI (invariate) ---
@app.get("/")
async def read_index(request: Request):
    return templates.TemplateResponse(
        "index.html", 
        {"request": request, "is_logged_in": "true", "is_paying_member": "true"}
    )

@app.post("/analyze", response_model=ResultResponse)
async def analyze_pdf_api(file: UploadFile = File(...)):
    
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Il file deve essere un PDF (application/pdf).")
    
    file.file.seek(0, os.SEEK_END)
    file_size = file.file.tell()
    file.file.seek(0)
    
    if file_size < 1000:
        raise HTTPException(status_code=400, detail="Impossibile analizzare. Il file è troppo piccolo o vuoto.")

    temp_filename = f"{uuid.uuid4()}.pdf"
    temp_path = os.path.join("/tmp", temp_filename) 

    try:
        with open(temp_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
    except Exception as e:
        raise HTTPException(status_code=500, detail="Errore I/O: Impossibile salvare il file temporaneo.")
    finally:
        file.file.close()

    try:
        analysis_result = await run_gemini_analysis(temp_path, file.filename)
        return ResultResponse(status="success", result=analysis_result, detail="Analisi completata con successo.")
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Errore di servizio FATALE: {e}")