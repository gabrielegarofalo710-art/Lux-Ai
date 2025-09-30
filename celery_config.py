from celery import Celery
import os

# Legge la URL di Redis dalla variabile d'ambiente che Railway fornirà
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Inizializza l'app Celery
celery_app = Celery(
    'lux_ai_worker',
    broker=REDIS_URL,
    backend=REDIS_URL
)

# Configurazione aggiuntiva (opzionale)
celery_app.conf.update(
    task_track_started=True,
    broker_connection_retry_on_startup=True
)