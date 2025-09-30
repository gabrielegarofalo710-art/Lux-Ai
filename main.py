import os
import shutil
import tempfile
import uuid
import json
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from celery_config import celery_app
from workers import analyze_pdf_task

# --- Configurazione e Inizializzazione ---
app = FastAPI()
# Supponiamo un DB semplice (dizionario in memoria per l'MVP) per i risultati dei job
# In produzione si userebbe Redis o un DB persistente
JOB_RESULTS = {}


# --- Modelli Pydantic per la Risposta ---
class JobResponse(BaseModel):
    job_id: str
    status: str = "processing"
    message: str = "Analisi avviata. Controllare lo stato tra qualche secondo."

class ResultResponse(BaseModel):
    status: str = Field(..., description="Stato del job: success, processing, failed")
    result: dict | None = None
    detail: str | None = None

# --- ROTTA PER IL FRONTEND ---
@app.get("/", response_class=FileResponse)
async def read_index():
    # Serve il file index.html
    return "index.html"

# --- 1. ROTTA: Invia l'Analisi al Worker ---
@app.post("/analyze", response_model=JobResponse)
async def analyze_pdf_api(file: UploadFile = File(...)):
    
    # 1. Graceful Degradation: Controlli iniziali
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Il file deve essere un PDF (application/pdf).")
    
    # Simula check per PDF illeggibile/vuoto
    file.file.seek(0, os.SEEK_END)
    file_size = file.file.tell()
    file.file.seek(0)
    if file_size < 1000: 
        raise HTTPException(status_code=400, detail="Impossibile analizzare. Il file è troppo piccolo o vuoto. Caricare un PDF nativo.")

    # 2. Salvataggio temporaneo del file (necessario per Celery)
    # L'API salverà in un'area accessibile dal worker (in un setup completo, sarebbe S3/GCS)
    # Per l'MVP su Railway, usiamo /tmp e ci affidiamo a un filesystem condiviso/accessibile (non ideale ma funziona per il test)
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

    # 3. Inoltra il Job a Celery (Worker)
    try:
        task = analyze_pdf_task.delay(temp_path)
        return JobResponse(job_id=task.id)
    except Exception as e:
        print(f"CELERY ERROR: Impossibile inviare il job. Redis/Worker non attivo. Dettaglio: {e}")
        # Pulizia immediata se il job fallisce l'invio
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise HTTPException(status_code=500, detail="Errore di servizio: Il sistema di elaborazione AI è inattivo.")


# --- 2. ROTTA: Controlla lo Stato del Job ---
@app.get("/status/{job_id}", response_model=ResultResponse)
async def get_job_status(job_id: str):
    task = celery_app.AsyncResult(job_id)
    
    if task.state == 'PENDING':
        return ResultResponse(status="processing", detail="Analisi in corso...")
    
    elif task.state == 'SUCCESS':
        # La risposta è un dict contenente lo stato finale e il risultato
        result_data = task.result 
        
        # Pulizia del file temporaneo dopo il successo
        temp_path = result_data.get('temp_path')
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
            result_data['temp_path'] = 'Cleaned' # Rimuovi il path dal risultato
            
        return ResultResponse(status="success", result=result_data, detail="Analisi completata con successo.")
    
    elif task.state == 'FAILURE':
        # L'errore è contenuto nel task.result
        return ResultResponse(status="failed", detail=f"Analisi fallita: {task.info}")

    else:
        # Altri stati Celery (es. STARTED, RETRY)
        return ResultResponse(status="processing", detail="Elaborazione in corso.")