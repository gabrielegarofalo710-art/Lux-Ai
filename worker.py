import os
import json
import google.generativeai as genai
from celery_config import celery_app

# --- Funzione Helper per l'Inizializzazione Gemini ---
def get_gemini_client():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        # Non solleviamo HTTP error, ma un errore standard per Celery
        raise ValueError("GEMINI_API_KEY non trovata. Impossibile configurare l'AI.")
    
    genai.configure(api_key=api_key)
    return genai.client.Client()

# --- TASK CELERY: Analisi PDF ---
@celery_app.task(bind=True)
def analyze_pdf_task(self, temp_path: str):
    """
    Esegue l'analisi PDF pesante in background.
    temp_path: Il percorso locale del file PDF da analizzare.
    """
    client = None
    uploaded_file = None
    
    try:
        # 1. Ottieni il Client AI
        client = get_gemini_client()

        # 2. Carica il file su Gemini
        self.update_state(state='STARTED', meta={'progress': 20, 'message': 'Caricamento file AI...'})
        uploaded_file = client.files.upload(file=temp_path)

        # 3. Prompt focalizzato sul ROI e Rischio
        prompt = """
            Analizza questo documento PDF concentrandoti SOLO su due aree critiche ad alto rischio e alta frequenza:
            1. Clausole di Limitazione di Responsabilità (Liability Caps).
            2. Clausole di Indennizzo (Indemnification).

            Estrai i risultati in formato JSON. Per il ROI, assumi che l'avvocato medio risparmi 15 minuti di revisione per queste clausole.
            
            Rispondi SOLO con il blocco di codice JSON con la seguente struttura:
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

        # Restituisce il risultato e il path del file (per la pulizia nel main.py)
        return {"result": json_result, "temp_path": temp_path}

    except Exception as e:
        # Cattura qualsiasi errore e lo logga
        print(f"WORKER FATAL ERROR: {e}")
        # Rilancia l'errore per segnalare il fallimento a Celery
        raise e
    
    finally:
        # 6. Pulizia del file su Gemini (essenziale)
        if uploaded_file:
            client.files.delete(name=uploaded_file.name)
        
        # NOTA: La pulizia del file locale (temp_path) è gestita nella rotta /status