from celery import Celery
import os

# Legge l'URL di Redis dalla variabile d'ambiente (REDIS_URL fornita da Railway)
# Se non trovata (solo per test locale), usa l'indirizzo di default
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Inizializza l'app Celery
celery_app = Celery(
    'lux_ai_worker',
    broker=REDIS_URL,
    backend=REDIS_URL # Usiamo Redis anche per il backend dei risultati
)

# Configurazione aggiuntiva
celery_app.conf.update(
    task_track_started=True,
    broker_connection_retry_on_startup=True
)