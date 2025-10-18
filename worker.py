import os
import json
import google.generativeai as genai
import time 
from celery_config import celery_app

# --- Funzione Helper per l'Inizializzazione Gemini ---
# Legge la chiave dall'ambiente (che Render inietterà)
def get_gemini_client():
    # Usiamo GEMINI_API_KEY come variabile d'ambiente standard
    api_key = os.getenv("GEMINI_API_KEY") 
    
    # Se la chiave non è stata trovata, solleviamo un errore che bloccherà il worker
    if not api_key:
        raise ValueError("GEMINI_API_KEY non trovata. Impossibile configurare l'AI.")
    
    # Inizializzazione della configurazione Gemini
    genai.configure(api_key=api_key)
    return genai.client.Client()

# --- TASK CELERY: Analisi PDF ---
@celery_app.task(bind=True)
def analyze_pdf_task(self, temp_path: str, user_id: str):
    """
    Esegue l'analisi PDF pesante in background, carica il file su Gemini e pulisce.
    """
    client = None
    uploaded_file = None
    
    try:
        # 1. Ottieni il Client AI
        client = get_gemini_client()

        # 2. Carica il file su Gemini
        self.update_state(state='STARTED', meta={'progress': 20, 'message': 'Caricamento file AI...'})
        
        # Caricamento effettivo del file su Google AI
        uploaded_file = client.files.upload(file=temp_path)

        # 3. Prompt focalizzato sul ROI e Rischio (MVP: solo 2 clausole)
        prompt = """
            Analizza questo documento PDF concentrandoti SOLO su due aree critiche ad alto rischio e alta frequenza:
            1. Clausole di Limitazione di Responsabilità (Liability Caps).
            2. Clausole di Indennizzo (Indemnification).

            Estrai i risultati in formato JSON. Per il ROI, assumi che l'avvocato medio risparmi 15 minuti di revisione per queste clausole.
            
            Rispondi SOLO con il blocco di codice JSON con la seguente struttura. Se una clausola non è trovata, usa "NON TROVATA" per il rischio e "" per il testo.
            {
                "nome_documento": "...",
                "risparmio_stimato_eur": 60.00, 
                "tempo_risparmiato_min": 15,
                "analisi_clausole": [
                    {
                        "tipo": "Limitazione di Responsabilità",
                        "rischio": "HIGH" | "MEDIUM" | "LOW" | "NON TROVATA",
                        "testo_clausola_esatta": "..." 
                    },
                    {
                        "tipo": "Indennizzo",
                        "rischio": "HIGH" | "MEDIUM" | "LOW" | "NON TROVATA",
                        "testo_clausola_esatta": "..." 
                    }
                ],
                "riassunto_breve": "Breve riassunto dei rischi trovati (massimo 2 frasi)."
            }
            """
        
        # 4. Generazione del Contenuto
        self.update_state(state='STARTED', meta={'progress': 50, 'message': 'Analisi AI in corso...'})
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[prompt, uploaded_file]
        )

        output_text = response.text.strip()
        if output_text.startswith("```json"):
            output_text = output_text.replace("```json", "").replace("```", "").strip()

        # 5. Tentativo di Decodifica e Restituzione
        json_result = json.loads(output_text)

        # Restituisce il risultato e il path del file locale (per la pulizia in main.py)
        return {"result": json_result, "temp_path": temp_path}

    except Exception as e:
        print(f"WORKER FATAL ERROR: {e}")
        # Pulisci subito il file Gemini in caso di fallimento
        if uploaded_file and client:
             # Assicurati che l'oggetto client sia valido prima di chiamare delete
             try:
                 client.files.delete(name=uploaded_file.name)
             except Exception as cleanup_e:
                 print(f"Cleanup Error on Gemini: {cleanup_e}")

        # Rilancia l'errore per segnalare il fallimento a Celery
        raise e
    
    # Pulizia locale del file PDF è gestita nella rotta /status del main.py