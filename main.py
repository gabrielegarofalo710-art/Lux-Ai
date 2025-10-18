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
templates = Jinja2Templates(directory=".") # Imposta la directory del template (la root del progetto)

# --- DATI ESSENZIALI (URL REALI) ---
STRIPE_CHECKOUT_URL = 'https://buy.stripe.com/test_00w5kDdh648i2GUgHMbQY00'
WIX_LOGIN_URL = 'https://luxailegal.wixsite.com/my-site-9'
HOME_URL = 'https://luxailegal.wixsite.com/my-site-9'
# Chiave segreta per verificare i Webhook di Stripe (CRITICA PER LA SICUREZZA!)
STRIPE_WEBHOOK_SECRET = "whsec_..." # ⚠️ SOSTITUISCI CON LA TUA VERA CHIAVE SEGRETA!

# --- SIMULAZIONE DATABASE (SOSTITUIRE CON DB REALE) ---
USERS_DB = {
    "user_wix_demo": {"is_logged_in": True, "is_paying_member": False, "email": "test-demo@lux-ai.com"}, 
    "user_wix_paid": {"is_logged_in": True, "is_paying_member": True, "email": "paid-user@lux-ai.com"},
    "ANONIMO": {"is_logged_in": False, "is_paying_member": False, "email": None},
}
# La mappa email: ID è cruciale per il Webhook
EMAIL_TO_ID = {data['email']: user_id for user_id, data in USERS_DB.items() if data['email']}

# --- Modelli Pydantic per la Risposta ---
class JobResponse(BaseModel):
    job_id: str
    status: str = "processing"
    message: str = "Analisi avviata. Controllare lo stato tra qualche secondo."

class ResultResponse(BaseModel):
    status: str = Field(..., description="Stato del job: success, processing, failed")
    result: dict | None = None
    detail: str | None = None

class StripeEvent(BaseModel):
    type: str
    data: dict
    id: str

# --- UTILITY: Recupera l'Utente Corrente (SIMULAZIONE SSO) ---
def get_current_user_id(request: Request) -> str:
    auth_token = request.cookies.get("auth_token") 
    
    if auth_token == "paid_token":
        return "user_wix_paid"
    if auth_token == "demo_token":
        return "user_wix_demo"
    
    return "ANONIMO" 

# ----------------------------------------------------------------------------------
#                                 LE NUOVE ROTTE CRITICHE
# ----------------------------------------------------------------------------------

# --- ROTTA PRINCIPALE: SERVE L'HTML DINAMICO ---
@app.get("/")
async def read_index(request: Request):
    user_id = get_current_user_id(request)
    user_data = USERS_DB.get(user_id, USERS_DB["ANONIMO"])

    return templates.TemplateResponse(
        "index.html", 
        {
            "request": request,
            "is_logged_in": str(user_data["is_logged_in"]).lower(), 
            "is_paying_member": str(user_data["is_paying_member"]).lower(),
        }
    )

# --- 2. ROTTA CHIRURGICA: WEBHOOK STRIPE (SBLOCCO AUTOMATICO) ---
@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get('stripe-signature')
    event = None
    
    try:
        # Nota: La vera verifica di sicurezza di Stripe è disabilitata per il test MVP.
        event = json.loads(payload)
    except:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        customer_email = session.get('customer_details', {}).get('email')
        
        if customer_email:
            user_id = EMAIL_TO_ID.get(customer_email)
            if user_id and user_id in USERS_DB:
                USERS_DB[user_id]["is_paying_member"] = True
                print(f"✅ SBLOCCO AVVENUTO: L'utente {user_id} ({customer_email}) è ora MEMBRO PAGANTE.")
            else:
                 print(f"ATTENZIONE: Pagamento ricevuto per email sconosciuta: {customer_email}")

    return {"status": "success"}


# ----------------------------------------------------------------------------------
#                                 LE TUE VECCHIE ROTTE (MODIFICATE)
# ----------------------------------------------------------------------------------

# --- 1. ROTTA: Invia l'Analisi al Worker (Asincrono) ---
@app.post("/analyze", response_model=JobResponse)
async def analyze_pdf_api(request: Request, file: UploadFile = File(...)):
    # ⚠️ PAYWALL DI SICUREZZA LATO SERVER
    user_id = get_current_user_id(request)
    user_data = USERS_DB.get(user_id, USERS_DB["ANONIMO"])
    
    if not user_data["is_paying_member"]:
        raise HTTPException(status_code=403, detail="Accesso negato. Solo i membri del Pilot Programma possono avviare l'analisi.")

    if file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Il file deve essere un PDF (application/pdf).")
    
    file.file.seek(0, os.SEEK_END)
    file_size = file.file.tell()
    file.file.seek(0)
    
    if file_size < 1000:
        raise HTTPException(status_code=400, detail="Impossibile analizzare. Il file è troppo piccolo o vuoto. Si prega di caricare un PDF nativo.")

    temp_filename = f"{uuid.uuid4()}.pdf"
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


# --- 2. ROTTA: Controlla lo Stato del Job ---
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