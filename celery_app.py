import os
from celery import Celery
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Get Redis URL from environment, with a default for local development
redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379/0')

# Create the Celery application instance
# The first argument is the name of the current module.
# The 'broker' and 'backend' arguments specify the URL of the message broker (Redis).
celery = Celery(
    'web_app',
    broker=redis_url,
    backend=redis_url,
    include=['tasks']  # Look for tasks in tasks.py
)

# Optional Celery configuration
celery.conf.update(
    task_serializer='json',
    accept_content=['json'],  # Ignore other content
    result_serializer='json',
    timezone='Europe/Moscow',
    enable_utc=True,
    # Lower the prefetch multiplier to prevent a single worker from grabbing too many tasks at once
    worker_prefetch_multiplier=1,
    # Acknowledge tasks after they have been executed, not before.
    # This means if a worker crashes, the task will be re-queued.
    task_acks_late=True,
)

if __name__ == '__main__':
    # This allows you to run the celery worker directly for debugging
    # Command: python celery_app.py worker --loglevel=info
    celery.start()
