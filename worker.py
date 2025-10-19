import os
import json
import google.generativeai as genai
import time 
from celery_config import celery_app
# 🛑 CORREZIONE ULTIMA CHANCE: Importa la classe APIError direttamente dal modulo 'genai'
# Se questo non funziona, il problema è solo la versione Python 3.13
from google.generativeai import APIError # <- MODIFICA QUI!

# --- Funzione Helper per l'Inizializzazione Gemini ---
def get_gemini_client():
    # Legge la chiave dall'ambiente (che Render inietterà)
    api_key = os.getenv("GEMINI_API_KEY") 
    
    if not api_key:
        raise ValueError("GEMINI_API_KEY non trovata. Impossibile configurare l'AI.")
    
    # Restituisce il client usando la chiave
    return genai.Client(api_key=api_key)

# --- Definizione dello Schema JSON per l'Output (Cruciale per la dinamicità) ---
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

# --- TASK CELERY: Analisi PDF ---
@celery_app.task(bind=True)
def analyze_pdf_task(self, temp_path: str, user_id: str):
    client = None
    uploaded_file = None
    
    # ⚠️ DEBUG: Conferma l'inizio del task
    print(f"DEBUG: Avvio task Celery per file: {temp_path} (User: {user_id})")
    
    try:
        # 1. Ottieni il Client AI
        client = get_gemini_client()
        
        # ⚠️ CONTROLLO CRITICO: Il file temporaneo esiste?
        if not os.path.exists(temp_path):
             raise FileNotFoundError(f"FATAL: File PDF non trovato nel percorso temporaneo: {temp_path}. I servizi non condividono /tmp?")


        # 2. Carica il file su Gemini (Passando il path corretto da /tmp)
        self.update_state(state='STARTED', meta={'progress': 20, 'message': 'Caricamento file AI su Gemini...'})
        
        uploaded_file = client.files.upload(file=temp_path)
        print(f"DEBUG: File {os.path.basename(temp_path)} caricato con successo su Gemini come {uploaded_file.name}")


        # 3. Prompt focalizzato
        prompt = f"""
            Analizza il documento PDF fornito. Il tuo obiettivo è concentrarti SOLO su due aree critiche:
            1. Clausole di Limitazione di Responsabilità (Liability Caps).
            2. Clausole di Indennizzo (Indemnification).

            Estrai i risultati in formato JSON. Per il ROI, assumi che l'avvocato medio risparmi 15 minuti di revisione per queste clausole.
            
            Popola il campo "nome_documento" con "{os.path.basename(temp_path)}". Rispondi ESCLUSIVAMENTE con l'oggetto JSON richiesto.
            """
        
        # 4. Generazione del Contenuto con Schema Enforced
        self.update_state(state='STARTED', meta={'progress': 50, 'message': 'Analisi AI in corso... (Gemini 2.5 Flash)'})
        
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[prompt, uploaded_file],
            config={
                # PARAMETRI CRUCIALI PER L'OUTPUT JSON PERFETTO:
                "response_mime_type": "application/json",
                "response_schema": OUTPUT_SCHEMA
            }
        )

        # 5. Decodifica e Pulizia (Il testo della risposta dovrebbe essere un JSON puro)
        output_text = response.text.strip()
        json_result = json.loads(output_text)

        # 6. Pulizia del file da Gemini in caso di SUCCESSO (CRITICO!)
        client.files.delete(name=uploaded_file.name)
        print(f"DEBUG: Pulizia file Gemini completata: {uploaded_file.name}")

        # 7. Restituisce il risultato
        return {"result": json_result, "temp_path": temp_path, "status": "SUCCESS"}

    except FileNotFoundError as e:
        error_msg = str(e)
        raise self.raise_for_status(error_msg, status='FAILURE')
        
    except APIError as e:
        error_msg = f"Errore API Gemini (Verifica chiave): {e}"
        # Gestione della pulizia in caso di fallimento
        if uploaded_file and client:
            try:
                client.files.delete(name=uploaded_file.name)
            except: pass
        raise self.raise_for_status(error_msg, status='FAILURE')
        
    except json.JSONDecodeError as e:
        error_msg = f"Errore di decodifica JSON dall'AI. Output: {output_text[:100]}..."
        if uploaded_file and client:
            try:
                client.files.delete(name=uploaded_file.name)
            except: pass
        raise self.raise_for_status(error_msg, status='FAILURE')
        
    except Exception as e:
        error_msg = f"WORKER FATAL ERROR (Generico): {e}"
        # Pulizia in caso di qualsiasi altro errore
        if uploaded_file and client:
            try:
                client.files.delete(name=uploaded_file.name)
            except: pass
        raise self.raise_for_status(error_msg, status='FAILURE')