"""
Конфигурационный файл для Gunicorn WSGI сервера.

Использование:
    gunicorn -c gunicorn_config.py web_app:app
"""

import multiprocessing
import os
from pathlib import Path

# Базовая директория проекта
BASE_DIR = Path(__file__).parent

# Количество worker процессов
# Рекомендуется: (2 * CPU cores) + 1
workers = multiprocessing.cpu_count() * 2 + 1

# Класс worker'ов
worker_class = 'sync'

# Биндинг
bind = '127.0.0.1:8000'

# Таймауты (важно для долгих операций загрузки товаров)
timeout = 300  # 5 минут для обработки запросов
keepalive = 5

# Логирование
accesslog = str(BASE_DIR / 'kash' / 'gunicorn-access.log')
errorlog = str(BASE_DIR / 'kash' / 'gunicorn-error.log')
loglevel = 'info'

# Переменные окружения
raw_env = [
    'PATH=' + str(BASE_DIR / 'venv' / 'bin'),
]

# Перезапуск при изменении кода (для development)
reload = False

# Максимальное количество запросов на worker перед перезапуском
max_requests = 1000
max_requests_jitter = 50

# Ограничение памяти (в MB) - перезапуск worker при превышении
worker_tmp_dir = '/dev/shm'  # Используем shared memory для производительности

# Безопасность
limit_request_line = 4094
limit_request_fields = 100
limit_request_field_size = 8190

# Имя процесса
proc_name = 'nesivtoroi-tech'

