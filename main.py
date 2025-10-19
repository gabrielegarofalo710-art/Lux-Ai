import os
import shutil
import tempfile
import uuid
import json
from fastapi import FastAPI, UploadFile, File, HTTPException, Request 
from fastapi.responses import FileResponse
from fastapi.templating import Jinja2Templates 
from pydantic import BaseModel, Field
from celery_config import celery_app
from worker import analyze_pdf_task

# --- Configurazione e Inizializzazione ---
app = FastAPI()
templates = Jinja2Templates(directory=".") 

# --- DATI ESSENZIALI (URL REALI) ---
WIX_LOGIN_URL = 'https://luxailegal.wixsite.com/my-site-9'
HOME_URL = 'https://luxailegal.wixsite.com/my-site-9'

# --- SIMULAZIONE DATABASE (ACCESSO SEMPLIFICATO) ---
USERS_DB = {
    # Mantenuti per l'iniezione HTML, ma il paywall non li userà più
    "user_wix_demo": {"is_logged_in": True, "is_paying_member": True, "email": "test-demo@lux-ai.com"}, 
    "ANONIMO": {"is_logged_in": False, "is_paying_member": False, "email": None},
}

# --- Modelli Pydantic per la Risposta (Invariati) ---
class JobResponse(BaseModel):
    job_id: str
    status: str = "processing"
    message: str = "Analisi avviata. Controllare lo stato tra qualche secondo."

class ResultResponse(BaseModel):
    status: str = Field(..., description="Stato del job: success, processing, failed")
    result: dict | None = None
    detail: str | None = None

# --- UTILITY: Recupera l'Utente Corrente (SIMULAZIONE SSO) ---
def get_current_user_id(request: Request) -> str:
    # Simula che qualsiasi token valido garantisca l'accesso completo alla MVP
    auth_token = request.cookies.get("auth_token") 
    
    if auth_token == "demo_token" or auth_token == "paid_token":
        return "user_wix_demo"
    
    return "ANONIMO" 

# ----------------------------------------------------------------------------------
#                                 ROTTE (SEMPLIFICATE)
# ----------------------------------------------------------------------------------

# --- ROTTA PRINCIPALE: SERVE L'HTML DINAMICO ---
@app.get("/")
async def read_index(request: Request):
    user_id = get_current_user_id(request)
    user_data = USERS_DB.get(user_id, USERS_DB["ANONIMO"])
    
    # Per l'MVP, forziamo il frontend a comportarsi come se l'utente fosse loggato,
    # anche se non lo è (il JS ora lo forza a true)
    return templates.TemplateResponse(
        "index.html", 
        {
            "request": request,
            "is_logged_in": "true", # Mantenuto per evitare errori di Jinja
            "is_paying_member": "true", # Mantenuto per evitare errori di Jinja
        }
    )

# --- ROTTA: Invia l'Analisi al Worker (Asincrono) ---
@app.post("/analyze", response_model=JobResponse)
async def analyze_pdf_api(request: Request, file: UploadFile = File(...)):
    # 💥 CRITICITÀ RISOLTA: La verifica del login è stata rimossa per l'MVP accessibile.
    user_id = get_current_user_id(request)
    # user_data = USERS_DB.get(user_id, USERS_DB["ANONIMO"])
    
    # 💥 RIMOSSO IL PAYWALL: if not user_data["is_logged_in"]:
    # 💥 RIMOSSO IL PAYWALL:     raise HTTPException(status_code=403, detail="Accesso negato...")

    # Il resto della logica di upload rimane invariato
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Il file deve essere un PDF (application/pdf).")
    
    file.file.seek(0, os.SEEK_END)
    file_size = file.file.tell()
    file.file.seek(0)
    
    if file_size < 1000:
        raise HTTPException(status_code=400, detail="Impossibile analizzare. Il file è troppo piccolo o vuoto. Si prega di caricare un PDF nativo.")

    temp_filename = f"{uuid.uuid4()}.pdf"
    # Usiamo /tmp, la cartella temporanea accessibile su Render
    temp_path = os.path.join("/tmp", temp_filename) 

    try:
        with open(temp_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
    except Exception as e:
        print(f"FATAL ERROR - Salvataggio File: {e}")
        raise HTTPException(status_code=500, detail="Errore I/O: Impossibile salvare il file temporaneo.")
    finally:
        file.file.close()

    try:
        task = analyze_pdf_task.delay(temp_path, user_id) 
        return JobResponse(job_id=task.id)
    except Exception as e:
        print(f"CELERY ERROR: Impossibile inviare il job. Dettaglio: {e}")
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise HTTPException(status_code=500, detail="Errore di servizio: Il sistema di elaborazione AI è inattivo.")


# --- ROTTA: Controlla lo Stato del Job (Invariata) ---
@app.get("/status/{job_id}", response_model=ResultResponse)
async def get_job_status(job_id: str):
    task = celery_app.AsyncResult(job_id)
    
    if task.state == 'PENDING' or task.state == 'STARTED':
        return ResultResponse(status="processing", detail=task.info.get('message', 'Analisi in corso...'))
    
    elif task.state == 'SUCCESS':
        result_data = task.result.get('result') 
        temp_path = task.result.get('temp_path')
        
        # Pulizia del file PDF temporaneo subito dopo il recupero del risultato
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
            
        return ResultResponse(status="success", result=result_data, detail="Analisi completata con successo.")
    
    elif task.state == 'FAILURE':
        return ResultResponse(status="failed", detail=f"Analisi fallita. Dettaglio: {task.info}")

    else:
        return ResultResponse(status="processing", detail="Elaborazione in corso.")