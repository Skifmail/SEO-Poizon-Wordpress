"""
Веб-приложение для синхронизации товаров Poizon → WordPress.

Это Flask веб-приложение предоставляет графический интерфейс для:
- Поиска товаров на Poizon по ключевым словам
- Просмотра товаров с изображениями и ценами
- Выбора товаров для загрузки в WordPress
- Настройки курса валюты и наценки
- Автоматической генерации SEO-оптимизированных описаний через GigaChat
- Загрузки товаров в WooCommerce с вариациями (размеры, цвета)

Технологии:
    - Flask: веб-фреймворк
    - Server-Sent Events (SSE): потоковая передача прогресса загрузки
    - In-memory кэш: минимизация запросов к API
    - Файловый кэш: долговременное хранение брендов (обновление раз в месяц)
    - GigaChat API: генерация описаний товаров

Архитектура:
    /api/search - поиск товаров в Poizon
    /api/upload-stream - загрузка товаров с потоковым прогрессом
    /api/gigachat-generate - генерация описаний через AI
    
Безопасность:
    - Работает только локально (127.0.0.1)
    - Не требует внешнего доступа
    
"""
import os
import logging
import requests
import json
from typing import Dict, List, Optional
from flask import Flask, render_template, jsonify, request, Response, stream_with_context, redirect, url_for, flash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from dataclasses import dataclass, asdict
from celery_app import celery
from pathlib import Path
import time
import uuid
from datetime import datetime

# Импорт задач Celery
from tasks import process_product_task

# Импорт существующих модулей
from poizon_to_wordpress_service import (
    WooCommerceService,
    SyncSettings
)
from poizon_api_fixed import PoisonAPIClientFixed as PoisonAPIService

# Настройка логирования (конфигурируем root logger для совместимости с Flask)
import logging.handlers

# Создаем папку для логов если не существует
from pathlib import Path
log_dir = Path("kash")
log_dir.mkdir(parents=True, exist_ok=True)

# Создаем и настраиваем handlers
file_handler = logging.FileHandler('kash/web_app.log', encoding='utf-8')
file_handler.setLevel(logging.DEBUG)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)

# Формат логов
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)

# Настраиваем root logger
root_logger = logging.getLogger()
root_logger.setLevel(logging.DEBUG)
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

# Отключаем DEBUG логи от сторонних библиотек (urllib3, requests и т.д.)
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('requests').setLevel(logging.WARNING)

# Настраиваем werkzeug чтобы избежать дублирования
werkzeug_logger = logging.getLogger('werkzeug')
werkzeug_logger.setLevel(logging.INFO)
werkzeug_logger.propagate = False  # Не передавать логи root logger (избегаем дублей)
# Добавляем наши handlers напрямую к werkzeug
werkzeug_logger.addHandler(file_handler)
werkzeug_logger.addHandler(console_handler)

# Используем root logger напрямую (уже настроен выше с file_handler + console_handler)
logger = root_logger

# Загрузка переменных окружения
load_dotenv()

# Инициализация Flask
app = Flask(__name__)
# Используем фиксированный SECRET_KEY из .env для стабильности сессий между перезапусками
secret_key = os.getenv('FLASK_SECRET_KEY')
if not secret_key:
    secret_key = os.urandom(24).hex()
    logger.warning("FLASK_SECRET_KEY не установлен в .env, используется случайный ключ (сессии могут сбрасываться при перезапуске)")
app.config['SECRET_KEY'] = secret_key
app.config['JSON_AS_ASCII'] = False

# ============================================================================
# АВТОРИЗАЦИЯ (Flask-Login)
# ============================================================================

# Инициализация LoginManager
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Пожалуйста, войдите в систему для доступа к этой странице.'
login_manager.login_message_category = 'info'


class User(UserMixin):
    """
    Класс пользователя для Flask-Login.
    
    Используется для управления сессиями авторизованных пользователей.
    """
    def __init__(self, user_id: str):
        """
        Инициализация пользователя.
        
        Args:
            user_id: Идентификатор пользователя (обычно "admin")
        """
        self.id = user_id


@login_manager.user_loader
def load_user(user_id: str):
    """
    Загружает пользователя по ID для Flask-Login.
    
    Args:
        user_id: Идентификатор пользователя
        
    Returns:
        User объект или None
    """
    return User(user_id)


def verify_password(username: str, password: str) -> bool:
    """
    Проверяет логин и пароль пользователя.
    
    Пароль хранится в переменных окружения в виде хеша (рекомендуется)
    или в открытом виде (для простоты настройки).
    
    Args:
        username: Имя пользователя
        password: Пароль в открытом виде
        
    Returns:
        True если логин и пароль верны, иначе False
    """
    admin_username = os.getenv('ADMIN_USERNAME', 'admin')
    admin_password_hash = os.getenv('ADMIN_PASSWORD_HASH', '')
    admin_password_plain = os.getenv('ADMIN_PASSWORD', '')
    
    # Проверяем имя пользователя
    if username != admin_username:
        logger.warning(f"Попытка входа с неправильным логином: {username}")
        return False
    
    # Если есть хеш пароля - проверяем через хеш
    if admin_password_hash:
        try:
            is_valid = check_password_hash(admin_password_hash, password)
            if is_valid:
                logger.info(f"Успешная авторизация пользователя: {username}")
            else:
                logger.warning(f"Неверный пароль для пользователя: {username}")
            return is_valid
        except Exception as e:
            logger.error(f"Ошибка проверки хеша пароля: {e}")
            return False
    
    # Если есть пароль в открытом виде - сравниваем напрямую
    if admin_password_plain:
        is_valid = (password == admin_password_plain)
        if is_valid:
            logger.info(f"Успешная авторизация пользователя: {username}")
        else:
            logger.warning(f"Неверный пароль для пользователя: {username}")
        return is_valid
    
    # Если не настроены ни хеш, ни пароль - авторизация отключена
    logger.error("ADMIN_PASSWORD или ADMIN_PASSWORD_HASH не установлены в .env - авторизация отключена!")
    return False

import redis

# ============================================================================
# КЭШИРОВАНИЕ (Redis)
# ============================================================================

class RedisCache:
    """
    Унифицированный кэш на основе Redis с TTL и JSON-сериализацией.
    
    Заменяет SimpleCache и BrandFileCache, обеспечивая общий кэш для
    всех рабочих процессов в production-среде.
    """
    def __init__(self, redis_url: str):
        """
        Инициализация клиента Redis.
        
        Args:
            redis_url: URL для подключения к Redis.
        """
        try:
            # decode_responses=True автоматически декодирует ответы из UTF-8
            self.redis = redis.from_url(redis_url, decode_responses=True)
            self.redis.ping()
            logger.info(f"[CACHE] Успешное подключение к Redis: {redis_url}")
        except Exception as e:
            logger.error(f"[CACHE] Ошибка подключения к Redis: {e}")
            logger.error("[CACHE] Кэширование будет отключено.")
            self.redis = None
        
        self.stats = {
            'hits': 0,
            'misses': 0,
            'sets': 0
        }

    def get(self, key: str) -> Optional[any]:
        """
        Получить значение из кэша Redis.
        
        Args:
            key: Ключ для поиска.
            
        Returns:
            Десериализованный объект или None, если ключ не найден.
        """
        if not self.redis:
            return None
            
        try:
            value = self.redis.get(key)
            if value:
                self.stats['hits'] += 1
                return json.loads(value)
            else:
                self.stats['misses'] += 1
                return None
        except Exception as e:
            logger.error(f"[CACHE] Ошибка получения ключа '{key}' из Redis: {e}")
            return None

    def set(self, key: str, value: any, ttl: int = 3600):
        """
        Сохранить значение в кэш Redis.
        
        Args:
            key: Ключ для сохранения.
            value: Значение (будет сериализовано в JSON).
            ttl: Время жизни в секундах.
        """
        if not self.redis:
            return
            
        try:
            serialized_value = json.dumps(value)
            self.redis.set(key, serialized_value, ex=ttl)
            self.stats['sets'] += 1
        except Exception as e:
            logger.error(f"[CACHE] Ошибка сохранения ключа '{key}' в Redis: {e}")

    def get_or_fetch(self, key: str, fetch_function: callable, ttl: int) -> Optional[any]:
        """
        Получает данные из кэша или выполняет функцию для их получения и кэширования.
        
        Args:
            key: Ключ кэша.
            fetch_function: Функция, которая будет вызвана, если данные не в кэше.
            ttl: Время жизни для новых данных в кэше.
            
        Returns:
            Данные из кэша или от fetch_function.
        """
        cached_data = self.get(key)
        if cached_data is not None:
            logger.info(f"[CACHE] Данные для ключа '{key}' найдены в Redis.")
            return cached_data
        
        logger.info(f"[CACHE] Данные для ключа '{key}' не найдены, вызываем fetch_function...")
        fresh_data = fetch_function()
        
        if fresh_data:
            self.set(key, fresh_data, ttl=ttl)
            logger.info(f"[CACHE] Новые данные для ключа '{key}' сохранены в Redis (TTL: {ttl}s).")
            
        return fresh_data

    def get_stats(self):
        """Получить статистику кэша"""
        if not self.redis:
            return {'error': 'Redis is not connected'}
            
        total = self.stats['hits'] + self.stats['misses']
        hit_rate = (self.stats['hits'] / total * 100) if total > 0 else 0
        
        try:
            # Получаем реальное количество ключей из Redis
            cached_items = self.redis.dbsize()
        except Exception:
            cached_items = -1 # Ошибка подключения

        return {
            'hits': self.stats['hits'],
            'misses': self.stats['misses'],
            'hit_rate': f"{hit_rate:.1f}%",
            'sets': self.stats['sets'],
            'cached_items': cached_items
        }

    def clear(self):
        """Очистить весь кэш (в текущей базе данных Redis)"""
        if not self.redis:
            return
        try:
            self.redis.flushdb()
            logger.info("[CACHE] Кэш Redis (текущая БД) полностью очищен.")
        except Exception as e:
            logger.error(f"[CACHE] Ошибка очистки кэша Redis: {e}")


# Создаем глобальный кэш на основе Redis
redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
cache = RedisCache(redis_url)


# ============================================================================
# КАТЕГОРИИ И ФИЛЬТРАЦИЯ
# ============================================================================

# Словарь категорий и ключевых слов (на основе анализа dewu.com)
CATEGORY_KEYWORDS = {
    # ОБУВЬ (29)
    29: {
        'keywords': ['鞋', '运动鞋', '板鞋', '跑鞋', '篮球鞋', '足球鞋', '球鞋', '拖鞋', '凉鞋', '靴', '靴子', '滑板鞋',
                    'shoes', 'sneakers', 'boots', 'sandals', 'trainers', 'loafers', 'slippers', 'footwear'],
        'search_terms': ['sneakers', 'shoes', 'boots', 'trainers', 'sandals', 'loafers', 'slippers', 'footwear']
    },
    
    # ЖЕНСКАЯ ОДЕЖДА (1000095)
    1000095: {
        'keywords': ['女装', '女士', '女款', 'T恤', '卫衣', '外套', '裤子', '短裤', '裙', '连衣裙',
                    'women clothing', 'dress', 'blouse', 'skirt', 'top', 't-shirt', 'jacket', 
                    'pants', 'jeans', 'women', 'coat', 'sweater'],
        'search_terms': ['women clothing', 'dress', 'blouse', 'skirt', 'women jacket', 'women pants', 'women jeans']
    },
    
    # МУЖСКАЯ ОДЕЖДА (1000096)
    1000096: {
        'keywords': ['男装', '男士', '男款', 'T恤', '卫衣', '外套', '裤子', '短裤',
                    'men clothing', 'shirt', 't-shirt', 'jacket', 'pants', 'jeans', 
                    'sweater', 'hoodie', 'men', 'coat'],
        'search_terms': ['men clothing', 'shirt', 'men jacket', 'men pants', 'jeans', 'sweater', 'hoodie']
    },
    
    # АКСЕССУАРЫ (92)
    92: {
        'keywords': ['帽子', '眼镜', '围巾', '手套', '袜子', '腰带', '领带', '发带',
                    'accessories', 'belt', 'hat', 'cap', 'necklace', 'earring', 'bracelet', 
                    'ring', 'sunglasses', 'scarf', 'gloves', 'socks'],
        'search_terms': ['accessories', 'belt', 'hat', 'cap', 'necklace', 'sunglasses']
    },
    
    # СУМКИ И РЮКЗАКИ (48)
    48: {
        'keywords': ['包', '背包', '手提包', '单肩包', '斜挎包', '钱包',
                    'bag', 'handbag', 'tote', 'shoulder bag', 'clutch', 'crossbody bag', 
                    'purse', 'backpack', 'rucksack', 'school bag', 'laptop backpack', 'sports backpack'],
        'search_terms': ['bag', 'handbag', 'tote', 'backpack', 'shoulder bag', 'clutch', 'crossbody bag']
    },
    
    # КОСМЕТИКА И ПАРФЮМЕРИЯ (278)
    278: {
        'keywords': ['香水', '口红', '面膜', '护肤', '化妆', '精华', '乳液', '面霜',
                    'cosmetics', 'perfume', 'skincare', 'lipstick', 'foundation', 
                    'eyeshadow', 'mascara', 'toner', 'moisturizer', 'fragrance'],
        'search_terms': ['cosmetics', 'perfume', 'skincare', 'lipstick', 'foundation', 'fragrance', 'moisturizer']
    },
}


def filter_products_by_category(products: List[Dict], category_id: int) -> List[Dict]:
    """
    Фильтрует товары по категории на основе ключевых слов в названии
    
    Args:
        products: Список товаров
        category_id: ID категории
        
    Returns:
        list: Отфильтрованные товары
    """
    if not category_id or category_id not in CATEGORY_KEYWORDS:
        logger.warning(f"Нет ключевых слов для категории {category_id}, показываем все товары")
        return products
    
    keywords = CATEGORY_KEYWORDS[category_id]['keywords']
    filtered = []
    
    for product in products:
        title = product.get('title', '').lower()
        
        # Проверяем наличие хотя бы одного ключевого слова
        if any(keyword.lower() in title for keyword in keywords):
            filtered.append(product)
    
    logger.info(f"Фильтрация: {len(products)} товаров → {len(filtered)} (категория {category_id})")
    return filtered


# Глобальные клиенты
poizon_client = None
woocommerce_client = None
gigachat_client = None



class GigaChatService:
    """Клиент для работы с GigaChat API"""
    
    def __init__(self):
        """Инициализация клиента GigaChat"""
        self.auth_key = os.getenv('GIGACHAT_AUTH_KEY')
        self.client_id = os.getenv('GIGACHAT_CLIENT_ID')
        self.base_url = 'https://gigachat.devices.sberbank.ru/api/v1'
        self.access_token = None
        
        if not self.auth_key or not self.client_id:
            logger.warning("GIGACHAT_AUTH_KEY или GIGACHAT_CLIENT_ID не найдены в .env")
            self.enabled = False
        else:
            self.enabled = True
            self._get_access_token()
        
        # Убрано: логи инициализации (дублируются в режиме DEBUG)
    
    def translate_color(self, color_chinese: str) -> str:
        """
        Переводит название цвета с китайского на русский через GigaChat.
        
        Args:
            color_chinese: Название цвета на китайском (например "黑白灰")
            
        Returns:
            Переведенное название цвета на русском
        """
        if not self.enabled or not color_chinese:
            return color_chinese
        
        # Быстрая проверка - если уже латиница/кириллица, не переводим
        if all(ord(c) < 0x4E00 for c in color_chinese.replace(' ', '')):
            return color_chinese
        
        try:
            url = f"{self.base_url}/chat/completions"
            
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
            }
            
            prompt = f"""Переведи название цвета с китайского на русский.

ЦВЕТ: {color_chinese}

ИНСТРУКЦИЯ:
- Если это один цвет (例如: "黑色" → "Черный", "白色" → "Белый")
- Если это комбинация цветов (例如: "黑白" → "Черно-белый", "黑白灰" → "Черно-бело-серый")
- Отвечай ОДНИМ словом или словосочетанием через дефис
- Без пояснений, только перевод цвета
- Первая буква заглавная

ОТВЕТ (только название цвета):"""
            
            data = {
                "model": "GigaChat",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 50
            }
            
            response = requests.post(url, headers=headers, json=data, verify=False, timeout=30)
            response.raise_for_status()
            
            result = response.json()
            translated = result['choices'][0]['message']['content'].strip()
            
            # Очищаем от лишних символов
            translated = translated.strip('"\'.,;!? \n\r\t')
            
            logger.info(f"  [GigaChat] Перевод цвета: '{color_chinese}' → '{translated}'")
            return translated
            
        except Exception as e:
            logger.warning(f"Ошибка перевода цвета через GigaChat: {e}, используем оригинал")
            return color_chinese
    
    def _get_access_token(self):
        """Получает access token от GigaChat API"""
        if not self.enabled:
            return
        
        url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
        
        # Генерируем уникальный RqUID как в main.py
        rq_uid = str(uuid.uuid4())
        
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "RqUID": rq_uid,
            "Authorization": f"Basic {self.auth_key}",
            "X-Client-ID": str(self.client_id)
        }
        
        data = {"scope": "GIGACHAT_API_PERS"}
        
        try:
            response = requests.post(url, headers=headers, data=data, verify=False, timeout=30)
            response.raise_for_status()
            self.access_token = response.json()["access_token"]
            # Убрано: логи инициализации (дублируются в режиме DEBUG)
        except Exception as e:
            logger.error(f"Ошибка получения токена GigaChat: {e}")
            if hasattr(e, 'response') and e.response:
                logger.error(f"Ответ сервера: {e.response.text}")
            self.enabled = False
    
    def translate_and_generate_seo(
        self,
        title: str,
        description: str,
        category: str,
        brand: str,
        attributes: Dict[str, str] = None,
        article_number: str = ''
    ) -> Dict[str, str]:
        """
        Переводит название, создает SEO описание через GigaChat.
        
        Args:
            title: Оригинальное название товара
            description: Оригинальное описание
            category: Категория товара
            brand: Бренд
            
        Returns:
            Словарь с переведенными и сгенерированными данными
        """
        # Если GigaChat не настроен, используем базовую обработку
        if not self.enabled:
            logger.warning("GigaChat не настроен, используется базовая обработка")
            return {
                "title_ru": title,
                "seo_title": f"{brand} {title[:50]}",
                "short_description": f"Качественный товар {brand} из категории {category}",
                "full_description": f"Описание товара {title}. {description[:200] if description else 'Подробное описание будет добавлено позже.'}",
                "meta_description": f"{brand} - {title[:80]}"
            }
        
        try:
            # Если атрибуты не переданы, используем пустой словарь
            if attributes is None:
                attributes = {}
            
            # Извлекаем данные из атрибутов
            color = attributes.get('Цвет', attributes.get('Основной цвет', ''))
            material = attributes.get('Материал', attributes.get('Материал верхней части', ''))
            
            # Определяем тип товара из категории
            cat_lower = category.lower()
            if '运动鞋' in title or '跑步鞋' in title or 'кроссовк' in cat_lower or 'туфля' in cat_lower:
                product_type = 'спортивная обувь'
            elif '板鞋' in title:
                product_type = 'кеды'
            else:
                product_type = 'обувь'
            
            # Извлекаем название модели из title
            product_name = title.split()[2:4] if len(title.split()) > 3 else title.split()[:2]
            product_name = ' '.join(str(x) for x in product_name if x)
            
            # Формируем промпт ТОЧНО как в main.py
            prompt = f"""⚠️ КРИТИЧЕСКИ ВАЖНО: 
1. СНАЧАЛА переведи ВСЕ китайские/японские слова на АНГЛИЙСКИЙ
2. ЗАТЕМ составь торговое название ТОЛЬКО из латиницы (A-Z) и цифр всегда с заглавной буквы
3. НЕ копируй иероглифы - ПЕРЕВОДИ их!

Ты — профессиональный SEO-копирайтер интернет-магазина, специализирующийся на бренде {brand}.
Создай структурированный SEO-контент для товара.

ИСХОДНЫЕ ДАННЫЕ (могут содержать китайский текст - ПЕРЕВЕДИ ЕГО):
- Бренд: {brand}
- Тип товара: {product_type}
- Исходное название: {title}  // ПЕРЕВЕДИ китайские слова на английский!
- Артикул/Style ID: {article_number}
- Цвет: {color}
- Материал: {material}
- Исходная категория: {category}
- Атрибуты: {attributes}

КЛЮЧЕВАЯ ФРАЗА: {brand} {product_name} {color} {article_number}
ВАЖНО: эта ключевая фраза должна присутствовать В ПЕРВОМ АБЗАЦЕ полного описания!

ИНСТРУКЦИЯ ПО ПЕРЕВОДУ:
- "定制球鞋" → "Custom Sneakers" (или просто убери)
- "阿卡丽" → транслитерация "Akali" или убери если непонятно
- "男款" → "Men's" или "Мужские"
- "黑白" → "Black White" или "Черно-белые"
- Если не можешь перевести - УБЕРИ это слово, НЕ копируй иероглифы!

ДОПОЛНИТЕЛЬНЫЕ ПРАВИЛА:
✓ Никогда не придумывай характеристики, которых нет в данных.
✓ Названия моделей, линейки (Air Jordan 1, Dunk Low, Yeezy 350 V2, Samba OG и т.д.) пиши латиницей всегда с заглавной буквы и не переводи.
✓ Не упоминай другие бренды.
✓ Пиши живым, разговорным языком: избегай канцелярита «высококачественный, многофункциональный, превосходный».
✓ Используй конкретику: вместо «удобные» – «мягкий воротник не натирает ахилл», вместо «лёгкие» – «вес одной кроссовки 320 г (42 размер)».
✓ Объём: краткое описание 280-320 зн., ПОЛНОЕ ОПИСАНИЕ МИНИМУМ 800 СИМВОЛОВ (не менее 300 слов!).

ПРАВИЛА ЧИТАЕМОСТИ (КРИТИЧЕСКИ ВАЖНО!):
⚠️ АКТИВНЫЙ ГОЛОС: Используй ТОЛЬКО активный голос! Максимум 10% пассивного голоса.
   ❌ Плохо: "Модель была выпущена в 2021 году", "Кроссовки были созданы для бега"
   ✅ Хорошо: "Бренд выпустил модель в 2021 году", "Дизайнеры создали кроссовки для бега"
   ✅ Хорошо: "Кроссовки держат асфальт", "Подошва амортизирует удары", "Материал дышит"

⚠️ КОРОТКИЕ ПРЕДЛОЖЕНИЯ: Максимум 25% предложений длиннее 15 слов!
   ❌ Плохо: Длинное предложение с множеством придаточных, запятых и деепричастных оборотов, которое трудно читать и понимать.
   ✅ Хорошо: Пиши короткими фразами. Одна мысль – одно предложение. Максимум 12-15 слов.
   ✅ Примеры коротких предложений: "Верх из кожи." "Подошва держит асфальт." "Вес 320 грамм."

В ПОЛНОМ ОПИСАНИИ обязательно раскрой (КОРОТКИМИ ПРЕДЛОЖЕНИЯМИ!):
→ ПЕРВЫЙ АБЗАЦ: начни с ключевой фразы "{brand} {product_name} {color} {article_number}". Кратко опиши товар;
→ визуальный образ (цвет, фактуры, контрасты);
→ материалы и их тактильные ощущения;
→ технологии (если указаны в attributes: Air, Boost, GORE-TEX и т.д.);
→ с чем носить. Куда надевать;
→ выгода для покупателя (лёгкость, устойчивость к погоде, доп. шнурки и т.д.).

SEO-заголовок ≤ 60 зн., ОБЯЗАТЕЛЬНО включает ТОЧНУЮ ключевую фразу в начале: "{brand} {product_name} {color}" (например: "Nike Dunk Low White Black – купить").
Мета-описание 150-160 зн., ОБЯЗАТЕЛЬНО включает ключевую фразу в начале, заканчивается призывом «Купить с доставкой» / «Закажи онлайн».
Ключевые слова: 3-4 слова (Nike; Dunk Low; кроссовки).

ФОРМАТ ОТВЕТА (ровно 6 строк, без пустых, без комментариев):
1. Название модели СТРОГО в формате: Бренд Модель Артикул (БЕЗ иероглифов, БЕЗ эмодзи, БЕЗ скобок)
2. Краткое описание
3. Полное описание
4. SEO Title (только латиница + кириллица, БЕЗ иероглифов)
5. Meta Description
6. Ключевые слова через точку с запятой (3-4 слова)

КРИТИЧЕСКИ ВАЖНО для строк 1 и 4:
- ❌ ЗАПРЕЩЕНО использовать китайские/японские иероглифы (定制球鞋、阿卡丽、时尚 и т.д.)
- ❌ ЗАПРЕЩЕНО использовать спецсимволы (【】、（）等)
- ✅ ТОЛЬКО латиница (A-Z, a-z) и кириллица (А-Я, а-я)
- ✅ ОБЯЗАТЕЛЬНО начинай с бренда: {brand} всегда с заглавной буквы
- ✅ Формат строки 1: "{brand} Модель Артикул" (например: Nike Court Borough BQ5448-115)
- ✅ Формат строки 4: "{brand} Модель - купить оригинал" (например: Nike Court Borough - купить оригинал)

Пример ПРАВИЛЬНОГО перевода с китайского:
Исходное название: "【定制球鞋】 Jordan Air Jordan 1 Mid 阿卡丽2 中帮 复古篮球鞋 男款 黑白"
                        ↓ ПЕРЕВОДИМ ↓
1. Jordan Air Jordan 1 Mid Akali 2 Black White DQ8426-154
   (убрали "定制球鞋", перевели "阿卡丽"→"Akali", "黑白"→"Black White", убрали лишнее)

Пример полного ответа (опирайся на стиль):
1. Nike Dunk Low White Black DD1391-103
2. Классический двухцветный Dunk Low: белая кожаная основа + чёрные замшевые оверлеи. Подошва средней толщины. Плотная строчка.
3. Nike Dunk Low White Black DD1391-103 – классика уличного стиля. Nike выпустил эту модель в 2021 году. Дизайн повторяет оригинальный блок 1985-го. Верх сделан из натуральной кожи. Белая гладкая кожа покрывает toe-box. Чёрная замша украшает swoosh и пятку. Перфорация в носке пропускает воздух. Внутри текстильная сетка. Она приятная и не растягивается. Промежуточная подошва из EVA весит на 30% меньше оригинала. Кроссовки подходят для города. Резиновая подметка держит асфальт и плитку. Даже в дождь. В комплекте белые шнурки flat. Есть вторая пара чёрных. Эти кроссовки сочетаются с джинсами-скинни. С карго тоже. С летними шортами отлично. Белый с чёрным всегда в тренде.
4. Nike Dunk Low White Black DD1391-103 – купить оригинал
5. Nike Dunk Low White Black DD1391-103 – оригинальные кроссовки в наличии. Бесплатная примерка, доставка по РФ в день заказа. Закажи онлайн!
6. Nike"""

            # Запрос к GigaChat
            logger.info(f"Обработка через GigaChat: {title[:50]}...")
            
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "model": "GigaChat",
                "messages": [
                    {"role": "system", "content": f"Ты - SEO-копирайтер, эксперт по товарам {brand}."},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.7,
                "max_tokens": 1500
            }
            
            url = f"{self.base_url}/chat/completions"
            response = requests.post(url, headers=headers, json=payload, verify=False, timeout=120)
            
            if response.status_code == 401:
                # Токен истек, получаем новый
                logger.warning("Access token истек, получаем новый...")
                self._get_access_token()
                headers["Authorization"] = f"Bearer {self.access_token}"
                response = requests.post(url, headers=headers, json=payload, verify=False, timeout=120)
            
            response.raise_for_status()
            
            result_text = response.json()['choices'][0]['message']['content'].strip()
            
            # Парсим построчный ответ (как в main.py) - ожидаем 6 строк
            lines = result_text.split('\n')
            
            # Убираем пустые строки и нумерацию
            parsed_lines = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                # Убираем "1. ", "2. ", "3. " и т.д.
                if line and len(line) > 3:
                    if line[0].isdigit() and line[1:3] in ['. ', ') ', ': ']:
                        line = line[3:].strip()
                    elif line[:2].isdigit() and line[2:4] in ['. ', ') ', ': ']:
                        line = line[4:].strip()
                
                if line:
                    parsed_lines.append(line)
            
            # Логируем что получили
            logger.info(f"GigaChat вернул {len(parsed_lines)} строк")
            logger.info(f"  Строка 1 (title): {parsed_lines[0][:100] if len(parsed_lines) > 0 else 'НЕТ'}")
            logger.info(f"  Строка 4 (seo_title): {parsed_lines[3][:100] if len(parsed_lines) > 3 else 'НЕТ'}")
            
            # Функция для АГРЕССИВНОЙ очистки текста от иероглифов
            import re
            
            def clean_chinese_chars(text: str) -> str:
                """ИЗВЛЕКАЕТ только латиницу, КИРИЛЛИЦУ, цифры и базовые символы из текста"""
                if not text:
                    return ""
                
                original = text
                # Убрано DEBUG: шаги очистки
                
                # ИЗВЛЕКАЕМ только нужные символы вместо удаления ненужных!
                # Оставляем: A-Z, a-z, А-Я, а-я, 0-9, пробел, тире, апостроф, точку, запятую
                result = []
                for char in text:
                    code = ord(char)
                    # ASCII латиница и цифры (основной диапазон)
                    if (0x0041 <= code <= 0x005A or   # A-Z
                        0x0061 <= code <= 0x007A or   # a-z
                        0x0030 <= code <= 0x0039 or   # 0-9
                        code == 0x0020 or              # пробел
                        code == 0x002D or              # тире -
                        code == 0x0027 or              # апостроф '
                        code == 0x002E or              # точка .
                        code == 0x002C or              # запятая ,
                        code == 0x002F):               # слэш / (для удаления в начале)
                        result.append(char)
                    # КИРИЛЛИЦА (русский алфавит)
                    elif (0x0410 <= code <= 0x042F or  # А-Я
                          0x0430 <= code <= 0x044F or  # а-я
                          code == 0x0401 or            # Ё
                          code == 0x0451):             # ё
                        result.append(char)
                    # Полноширинные латинские (FF21-FF5A)
                    elif 0xFF21 <= code <= 0xFF3A:  # Ａ-Ｚ
                        result.append(chr(code - 0xFEE0))
                    elif 0xFF41 <= code <= 0xFF5A:  # ａ-ｚ
                        result.append(chr(code - 0xFEE0))
                    elif 0xFF10 <= code <= 0xFF19:  # ０-９
                        result.append(chr(code - 0xFEE0))
                    # Все остальное игнорируем (китайские иероглифы, спецсимволы и т.д.)
                
                text = ''.join(result)
                
                # Убираем множественные пробелы
                text = re.sub(r'\s+', ' ', text).strip()
                # Убираем ведущий слэш (GigaChat иногда добавляет /название)
                text = text.lstrip('/')
                # Убираем лишние знаки препинания в начале/конце
                text = text.strip(' -.,/')
                
                # Если осталось меньше 3 символов - пустая строка
                if not text or len(text) < 3:
                    logger.warning(f"Очистка текста: '{original[:50]}' → пустая строка")
                    return ""
                
                return text
            
            # Очищаем ВСЕ строки от GigaChat
            title_clean = clean_chinese_chars(parsed_lines[0] if len(parsed_lines) > 0 else title)
            seo_title_clean = clean_chinese_chars(parsed_lines[3] if len(parsed_lines) > 3 else title)
            
            logger.info(f"После очистки: title='{title_clean}', seo_title='{seo_title_clean}'")
            
            # ВАЖНО: Если после очистки осталась пустая строка или мусор - используем бренд + артикул
            if not title_clean or len(title_clean.strip()) < 5 or title_clean.strip() in ['-', '-(', '-(-', '(', ')']:
                logger.warning(f"Title пустой или мусор: '{title_clean}', используем бренд + артикул")
                # Используем ОЧИЩЕННЫЙ бренд (product.brand уже очищен выше)
                title_clean = f"{brand} {article_number}".strip() if article_number else brand
                logger.info(f"Fallback title: '{title_clean}' (бренд из product.brand: '{brand}')")
            
            if not seo_title_clean or len(seo_title_clean.strip()) < 5 or seo_title_clean.strip() in ['-', '-(', '-(-', '(', ')']:
                logger.warning(f"SEO title пустой или мусор: '{seo_title_clean}', используем title_clean")
                seo_title_clean = title_clean + " - купить оригинал"
            
            # КРИТИЧНО: Очищаем бренд от иероглифов перед добавлением!
            brand_for_title = clean_chinese_chars(brand)
            if not brand_for_title or len(brand_for_title) < 2:
                brand_for_title = brand  # Если очистка вернула пустоту - используем как есть
            
            logger.info(f"Бренд для добавления в title: '{brand}' → '{brand_for_title}'")
            
            # Если название НЕ содержит бренд - добавляем ОЧИЩЕННЫЙ бренд
            # Проверяем НАЛИЧИЕ бренда в названии (не обязательно в начале)
            brand_upper = brand_for_title.upper()
            
            if title_clean and brand_upper not in title_clean.upper():
                logger.info(f"Бренд '{brand_for_title}' НЕ найден в title, добавляем в начало")
                title_clean = f"{brand_for_title} {title_clean}"
            
            if seo_title_clean and brand_upper not in seo_title_clean.upper():
                logger.info(f"Бренд '{brand_for_title}' НЕ найден в seo_title, добавляем в начало")
                seo_title_clean = f"{brand_for_title} {seo_title_clean}"
            
            # Получаем полное описание
            full_description = parsed_lines[2] if len(parsed_lines) > 2 else f"Описание {title}"
            
            # Очищаем keywords от слешей и китайских символов
            keywords_raw = parsed_lines[5] if len(parsed_lines) > 5 else f"{brand}, {category}"
            keywords_clean = clean_chinese_chars(keywords_raw)
            
            result = {
                "title_ru": title_clean,
                "short_description": parsed_lines[1] if len(parsed_lines) > 1 else f"Товар {brand}",
                "full_description": full_description,
                "seo_title": seo_title_clean,
                "meta_description": parsed_lines[4] if len(parsed_lines) > 4 else f"{brand} - купить онлайн",
                "keywords": keywords_clean
            }
            
            logger.info(f"[OK] GigaChat обработал товар:")
            logger.info(f"  title_ru: {result.get('title_ru', '')[:80]}")
            logger.info(f"  seo_title: {result.get('seo_title', '')[:80]}")
            return result
            
        except Exception as e:
            logger.error(f"Ошибка GigaChat обработки: {e}")
            # Возвращаем базовую обработку в случае ошибки
            return {
                "title_ru": title,
                "seo_title": f"{brand} {title[:50]}",
                "short_description": f"Качественный товар {brand} из категории {category}",
                "full_description": f"Описание товара {title}. {description[:200] if description else 'Подробное описание будет добавлено позже.'}",
                "meta_description": f"{brand} - {title[:80]}"
            }


# Инициализация при запуске
def init_services():
    """Инициализация всех сервисов"""
    global poizon_client, woocommerce_client, gigachat_client
    
    # Предотвращаем дублирование логов в режиме DEBUG (Flask запускает процесс дважды)
    # Показываем логи инициализации только в главном процессе
    is_reloader = os.environ.get('WERKZEUG_RUN_MAIN') == 'true'
    
    try:
        poizon_client = PoisonAPIService()
        woocommerce_client = WooCommerceService()
        gigachat_client = GigaChatService()
        
        if not is_reloader:
            logger.info("[OK] Все сервисы инициализированы")
    except Exception as e:
        logger.error(f"[ERROR] Ошибка инициализации сервисов: {e}")
        raise


# ============================================================================
# API ENDPOINTS
# ============================================================================

# ============================================================================
# МАРШРУТЫ АВТОРИЗАЦИИ
# ============================================================================

@app.route('/login', methods=['GET', 'POST'])
def login():
    """
    Страница авторизации пользователя.
    
    GET: Отображает форму входа
    POST: Проверяет логин и пароль, создает сессию пользователя
    """
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        remember = bool(request.form.get('remember', False))
        
        if not username or not password:
            flash('Пожалуйста, введите логин и пароль', 'error')
            return render_template('login.html'), 400
        
        # Проверяем учетные данные
        if verify_password(username, password):
            user = User(username)
            login_user(user, remember=remember)
            next_page = request.args.get('next')
            if next_page:
                return redirect(next_page)
            return redirect(url_for('index'))
        else:
            flash('Неверный логин или пароль', 'error')
            logger.warning(f"Неудачная попытка входа: username={username}")
            return render_template('login.html'), 401
    
    # GET запрос - показываем форму входа
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout_route():
    """
    Выход из системы.
    
    Удаляет сессию пользователя и перенаправляет на страницу входа.
    """
    username = current_user.id
    logout_user()
    logger.info(f"Пользователь вышел из системы: {username}")
    flash('Вы успешно вышли из системы', 'info')
    return redirect(url_for('login'))


# ============================================================================
# ЗАЩИЩЕННЫЕ МАРШРУТЫ (требуют авторизации)
# ============================================================================

@app.before_request
def require_login():
    """
    Автоматически проверяет авторизацию для всех маршрутов кроме /login и статических файлов.
    """
    # Разрешаем доступ без авторизации к странице логина и статическим файлам
    if request.endpoint == 'login' or request.endpoint == 'static' or request.path.startswith('/static/'):
        return None
    
    # Для всех остальных маршрутов требуется авторизация
    if not current_user.is_authenticated:
        # Если это API запрос - возвращаем JSON ошибку
        if request.path.startswith('/api/'):
            return jsonify({
                'success': False,
                'error': 'Требуется авторизация',
                'login_required': True
            }), 401
        # Для обычных страниц - перенаправляем на логин
        return redirect(url_for('login', next=request.url))


@app.route('/')
@login_required
def index():
    """Главная страница"""
    return render_template('index.html')


@app.route('/update')
@login_required
def update_page():
    """Страница обновления цен и остатков"""
    return render_template('update.html')


# ============================================================================
# ЗАГРУЗКА БРЕНДОВ (вспомогательные функции)
# ============================================================================

def fetch_all_brands_from_api(api_client) -> List[Dict]:
    """
    Загружает ВСЕ бренды из Poizon API через пагинацию.
    
    Используется файловым кэшем для обновления данных раз в месяц.
    
    Args:
        api_client: Экземпляр PoisonAPIService для запросов к API
    
    Returns:
        Список брендов с полями: id, name, logo, products_count
    """
    all_brands_raw = []
    page = 0
    max_pages = 50  # Максимум 5000 брендов (50 × 100)
    
    logger.info("[API] Загрузка всех брендов через пагинацию...")
    
    while page < max_pages:
        brands_page = api_client.get_brands(limit=100, page=page)
        
        if not brands_page or len(brands_page) == 0:
            logger.info(f"[API] Страница {page} пустая - все бренды загружены")
            break
        
        all_brands_raw.extend(brands_page)
        # Убрано DEBUG: информация о каждой странице
        
        # Если получили меньше 100, значит это последняя страница
        if len(brands_page) < 100:
            logger.info(f"[API] Последняя страница {page}: {len(brands_page)} брендов")
            break
        
        page += 1
    
    logger.info(f"[API] Загружено {len(all_brands_raw)} брендов с {page + 1} страниц")
    
    # Фильтруем и форматируем
    brands_list = []
    for brand in all_brands_raw:
        brand_name = brand.get('name', '')
        if brand_name and brand_name != '热门系列':  # Пропускаем "Горячие серии"
            brands_list.append({
                'id': brand.get('id'),
                'name': brand_name,
                'logo': brand.get('logo', ''),
                'products_count': 0
            })
    
    logger.info(f"[API] Отфильтровано брендов: {len(brands_list)}")
    return brands_list


@app.route('/api/brands', methods=['GET'])
def get_brands():
    """
    Получает список всех доступных брендов.
    
    Использует Redis кэш (обновление раз в 30 дней).
    
    Returns:
        JSON список брендов
    """
    try:
        # Ключ и TTL для кэша брендов
        cache_key = "all_brands"
        cache_ttl_seconds = 30 * 24 * 60 * 60  # 30 дней

        # Используем новый Redis кэш
        brands_list = cache.get_or_fetch(
            key=cache_key,
            fetch_function=lambda: fetch_all_brands_from_api(poizon_client),
            ttl=cache_ttl_seconds
        )
        
        logger.info(f"[API /brands] Возвращаем {len(brands_list)} брендов (из Redis кэша)")
        return jsonify({
            'success': True,
            'brands': brands_list
        })
        
    except Exception as e:
        logger.error(f"Ошибка получения брендов: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/categories', methods=['GET'])
def get_categories():
    """
    Получает список категорий (главные категории первого уровня).
    
    Returns:
        JSON список категорий
    """
    try:
        # Получаем все категории
        all_categories = poizon_client.get_categories(lang="RU")
        
        # Фильтруем только главные категории (level = 1)
        main_categories = []
        for cat in all_categories:
            if cat.get('level') == 1:
                main_categories.append({
                    'id': cat.get('id'),
                    'name': cat.get('name', ''),
                    'rootId': cat.get('rootId')
                })
        
        logger.info(f"Найдено главных категорий: {len(main_categories)}")
        return jsonify({
            'success': True,
            'categories': main_categories
        })
        
    except Exception as e:
        logger.error(f"Ошибка получения категорий: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


# ============================================================================
# НОВЫЕ ЭНДПОИНТЫ ДЛЯ КАТЕГОРИЙ И ПОИСКА
# ============================================================================

@app.route('/api/categories/simplified', methods=['GET'])
def get_simplified_categories():
    """
    Получает упрощенный список основных категорий
    (6 категорий вместо тысяч для удобства пользователя)
    """
    try:
        simple_categories = [
            {'id': 29, 'name': 'Обувь', 'level': 1},
            {'id': 1000095, 'name': 'Женская одежда', 'level': 1},
            {'id': 1000096, 'name': 'Мужская одежда', 'level': 1},
            {'id': 92, 'name': 'Аксессуары', 'level': 1},
            {'id': 48, 'name': 'Сумки и рюкзаки', 'level': 1},
            {'id': 278, 'name': 'Косметика и парфюмерия', 'level': 1},
        ]
        
        logger.info(f"Возвращаем {len(simple_categories)} упрощенных категорий")
        return jsonify({
            'success': True,
            'categories': simple_categories,
            'total': len(simple_categories)
        })
        
    except Exception as e:
        logger.error(f"Ошибка получения категорий: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/brands/by-category', methods=['GET'])
def get_brands_by_category():
    """
    Получает бренды для категории.
    ДЛЯ ОБУВИ (ID=29): Возвращает ВСЕ бренды из API (быстро!)
    ДЛЯ ДРУГИХ: Ищет товары по ключевым словам и извлекает бренды
    """
    try:
        category_id = request.args.get('category_id', type=int)
        
        if not category_id:
            return jsonify({
                'success': False,
                'error': 'Не указан category_id'
            }), 400
        
        # Проверяем кэш
        cache_key = f'brands_category_{category_id}'
        cached = cache.get(cache_key)
        if cached:
            logger.info(f"[CACHE] Бренды категории {category_id} из кэша ({len(cached)} шт)")
            cache.stats['requests_saved'] += 1
            return jsonify({
                'success': True,
                'brands': cached,
                'total': len(cached)
            })
        
        logger.info(f"[API] Получение брендов для категории {category_id}...")
        
        # СПЕЦИАЛЬНАЯ ЛОГИКА ДЛЯ ОБУВИ (ID=29) - ПОКАЗЫВАЕМ ВСЕ БРЕНДЫ!
        if category_id == 29:
            logger.info(f"[ОБУВЬ] Загружаем ВСЕ бренды (из Redis кэша)")
            
            # Ключ и TTL для кэша брендов
            cache_key_all_brands = "all_brands"
            cache_ttl_seconds = 30 * 24 * 60 * 60  # 30 дней

            # Используем новый Redis кэш
            all_brands_info = cache.get_or_fetch(
                key=cache_key_all_brands,
                fetch_function=lambda: fetch_all_brands_from_api(poizon_client),
                ttl=cache_ttl_seconds
            )
            
            # Сортируем по алфавиту
            brands_list = sorted(all_brands_info, key=lambda x: x['name'])
            
            logger.info(f"[ОБУВЬ] Возвращаем {len(brands_list)} брендов (из Redis кэша)")
            
            # Кэшируем результат для этой категории (ID 29) на 24 часа
            cache.set(cache_key, brands_list, ttl=86400)
            
            return jsonify({
                'success': True,
                'brands': brands_list,
                'total': len(brands_list)
            })
        
        # ДЛЯ ДРУГИХ КАТЕГОРИЙ - СТАРАЯ ЛОГИКА (поиск по ключевым словам)
        
        # Проверяем наличие категории
        if category_id not in CATEGORY_KEYWORDS:
            logger.warning(f"Категория {category_id} не поддерживается")
            return jsonify({
                'success': True,
                'brands': [],
                'total': 0
            })
        
        # Получаем поисковые термины
        search_terms = CATEGORY_KEYWORDS[category_id]['search_terms']
        logger.info(f"[API] Поиск товаров по терминам: {search_terms}")
        
        all_products = []
        
        # Ищем товары по каждому термину
        for term in search_terms:
            products = poizon_client.search_products(keyword=term, limit=100)
            all_products.extend(products)
            logger.info(f"  '{term}': найдено {len(products)} товаров")
        
        # Дедупликация
        unique_products = {}
        for product in all_products:
            spu_id = product.get('spuId', product.get('productId'))
            if spu_id and spu_id not in unique_products:
                unique_products[spu_id] = product
        
        logger.info(f"Уникальных товаров: {len(unique_products)}")
        
        # Фильтруем по категории
        filtered_products = filter_products_by_category(list(unique_products.values()), category_id)
        
        # Извлекаем уникальные бренды
        brands_dict = {}
        for product in filtered_products:
            brand_name = product.get('brandName', product.get('brand', ''))
            if brand_name and brand_name != '热门系列':
                if brand_name not in brands_dict:
                    brands_dict[brand_name] = {
                        'id': 0,
                        'name': brand_name,
                        'logo': '',
                        'products_count': 0
                    }
                brands_dict[brand_name]['products_count'] += 1
        
        logger.info(f"Найдено уникальных брендов: {len(brands_dict)}")
        
        # Получаем инфо о брендах (логотипы)
        all_brands_info = cache.get('all_brands')
        if not all_brands_info:
            all_brands = poizon_client.get_brands(limit=100)
            all_brands_info = []
            for b in all_brands:
                if b.get('name') and b.get('name') != '热门系列':
                    all_brands_info.append({
                        'id': b.get('id'),
                        'name': b.get('name'),
                        'logo': b.get('logo', ''),
                        'products_count': 0
                    })
            cache.set('all_brands', all_brands_info, ttl=43200)
        
        brand_info_map = {b['name']: b for b in all_brands_info}
        
        # Обогащаем данные
        brands_list = []
        for brand_name, brand_data in brands_dict.items():
            if brand_name in brand_info_map:
                full_brand = brand_info_map[brand_name]
                brand_data['id'] = full_brand.get('id', 0)
                brand_data['logo'] = full_brand.get('logo', '')
            brands_list.append(brand_data)
        
        # Сортируем по алфавиту
        brands_list.sort(key=lambda x: x['name'])
        
        # Кэшируем на 6 часов
        cache.set(cache_key, brands_list, ttl=21600)
        logger.info(f"[CACHE] Бренды категории {category_id} сохранены")
        
        return jsonify({
            'success': True,
            'brands': brands_list,
            'total': len(brands_list)
        })
        
    except Exception as e:
        logger.error(f"Ошибка получения брендов категории: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/search/manual', methods=['GET'])
def manual_search():
    """
    Ручной поиск по SPU ID или артикулу
    """
    try:
        query = request.args.get('query', '').strip()
        
        if not query:
            return jsonify({
                'success': False,
                'error': 'Не указан запрос'
            }), 400
        
        logger.info(f"Ручной поиск: '{query}'")
        
        # Поиск по SPU ID (если число)
        if query.isdigit():
            spu_id = int(query)
            logger.info(f"Поиск по SPU ID: {spu_id}")
            
            product_detail = poizon_client.get_product_detail_v3(spu_id)
            
            if product_detail:
                logger.info(f"Найден товар по SPU ID")
                
                return jsonify({
                    'success': True,
                    'products': [{
                        'spuId': spu_id,
                        'sku': str(spu_id),
                        'title': product_detail.get('title', ''),
                        'brand': product_detail.get('brandName', ''),
                        'description': product_detail.get('title', '')[:200],
                        'images': product_detail.get('images', []),
                        'articleNumber': product_detail.get('articleNumber', ''),
                        'price': 0
                    }],
                    'total': 1
                })
        
        # Поиск по ключевому слову
        logger.info(f"Поиск по ключевому слову: '{query}'")
        # Убрано ограничение limit=50, теперь вернет максимум доступных результатов (обычно 100)
        products = poizon_client.search_products(keyword=query)
        
        formatted_products = []
        for product in products:
            spu_id = product.get('spuId', product.get('productId'))
            formatted_products.append({
                'spuId': spu_id,
                'sku': str(spu_id),
                'title': product.get('title', ''),
                'brand': product.get('brandName', product.get('brand', '')),
                'description': product.get('title', '')[:200],
                'images': product.get('images', [product.get('logoUrl')]) if product.get('images') else [product.get('logoUrl', '')],
                'articleNumber': product.get('articleNumber', ''),
                'price': product.get('price', 0)
            })
        
        logger.info(f"Найдено товаров: {len(formatted_products)}")
        
        return jsonify({
            'success': True,
            'products': formatted_products,
            'total': len(formatted_products)
        })
        
    except Exception as e:
        logger.error(f"Ошибка ручного поиска: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/cache/stats', methods=['GET'])
def get_cache_stats():
    """Получить статистику кэша"""
    stats = cache.get_stats()
    return jsonify({
        'success': True,
        'stats': stats
    })


@app.route('/api/cache/clear', methods=['POST'])
def clear_cache_endpoint():
    """Очистить весь кэш"""
    cache.clear()
    return jsonify({
        'success': True,
        'message': 'Кэш очищен'
    })


@app.route('/api/products', methods=['GET'])
def get_products():
    """
    Получает список товаров для выбранного бренда и категории.
    ОБНОВЛЕНО: Поддержка category_id и множественной пагинации!
    
    Query params:
        brand: Название бренда
        category: Название категории (старый формат, для совместимости)
        category_id: ID категории (новый формат)
        page: Номер страницы (начиная с 0)
        limit: Количество товаров (по умолчанию 20)
        
    Returns:
        JSON список товаров с пагинацией
    """
    try:
        brand = request.args.get('brand', '')
        category = request.args.get('category', '')
        category_id = request.args.get('category_id', type=int)
        
        # Безопасная обработка page параметра
        try:
            page = int(request.args.get('page', 0))
        except (ValueError, TypeError):
            page = 0
            
        limit = int(request.args.get('limit', 20))
        
        # Проверка на undefined/null значения
        if brand == 'undefined' or brand == 'null':
            brand = ''
        if category == 'undefined' or category == 'null':
            category = ''
            
        if not brand and not category and not category_id:
            return jsonify({
                'success': False,
                'error': 'Не указан бренд или категория'
            }), 400
        
        # Формируем ключевое слово для поиска
        if brand:
            keyword = brand
        else:
            keyword = category
        
        logger.info(f"Поиск товаров: brand={brand}, category_id={category_id}, page={page}")
        
        # УМНАЯ ПАГИНАЦИЯ: загружаем по 10 страниц API за раз (1000 товаров)
        # Это дает хороший баланс между скоростью и полнотой данных
        all_products = []
        pages_per_batch = 10  # Загружаем по 10 страниц API за раз
        start_page = page * pages_per_batch  # Начальная страница для этого батча
        is_last_batch = False  # Флаг: достигли конца данных API
        
        for p in range(start_page, start_page + pages_per_batch):
            products_page = poizon_client.search_products(keyword=keyword, limit=100, page=p)
            
            if not products_page or len(products_page) == 0:
                logger.info(f"  API страница {p}: пустая, останавливаем загрузку")
                is_last_batch = True
                break
            
            all_products.extend(products_page)
            logger.info(f"  API страница {p}: найдено {len(products_page)} товаров")
            
            # Если API вернул меньше 100 товаров - это последняя страница
            if len(products_page) < 100:
                logger.info(f"  Получена последняя API страница (товаров < 100)")
                is_last_batch = True
                break
        
        logger.info(f"ВСЕГО загружено из API: {len(all_products)} товаров (страницы {start_page}-{start_page + pages_per_batch - 1})")
        
        # Дедупликация
        unique_products = {}
        for product in all_products:
            spu_id = product.get('spuId', product.get('productId'))
            if spu_id and spu_id not in unique_products:
                unique_products[spu_id] = product
        
        products = list(unique_products.values())
        logger.info(f"Уникальных товаров: {len(products)}")
        
        # Фильтруем по категории (если указан category_id)
        if category_id and category_id != 0:
            products = filter_products_by_category(products, category_id)
            logger.info(f"После фильтрации по категории: {len(products)}")
        
        # Форматируем результаты
        formatted_products = []
        for product in products:
            spu_id = product.get('spuId', product.get('productId'))
            
            formatted_products.append({
                'spuId': spu_id,
                'sku': str(spu_id),
                'title': product.get('title', ''),
                'brand': brand,
                'category': category,
                'description': product.get('title', '')[:200],
                'images': product.get('images', [product.get('logoUrl')]) if product.get('images') else [product.get('logoUrl', '')],
                'articleNumber': product.get('articleNumber', ''),
                'price': product.get('price', 0)
            })
        
        # Определяем, есть ли еще товары
        # Если достигли конца API данных (последняя страница или пустой ответ), то has_more=False
        has_more = not is_last_batch
        
        logger.info(f"Возвращаем товаров: {len(formatted_products)}, has_more={has_more}")
        return jsonify({
            'success': True,
            'products': formatted_products,
            'total': len(formatted_products),
            'page': page,
            'has_more': has_more  # Есть ли еще товары для загрузки
        })
        
    except Exception as e:
        logger.error(f"Ошибка получения товаров: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/upload', methods=['POST'])
def upload_products():
    """
    Starts the background processing of selected products using Celery.
    
    Request body:
        {
            "product_ids": [123, 456],
            "settings": { "currency_rate": 13.5, "markup_rubles": 5000 }
        }
        
    Returns:
        JSON with a list of task IDs for the created background jobs.
    """
    try:
        data = request.get_json()
        product_ids = data.get('product_ids', [])
        settings_data = data.get('settings', {})
        
        if not product_ids:
            return jsonify({'success': False, 'error': 'Не выбраны товары'}), 400
        
        logger.info(f"Получен запрос на загрузку {len(product_ids)} товаров. Настройки: {settings_data}")
        
        task_ids = []
        for spu_id in product_ids:
            try:
                # Ensure spu_id is an integer
                spu_id_int = int(spu_id)
                # Dispatch the task to Celery workers
                task = process_product_task.delay(spu_id_int, settings_data)
                task_ids.append(task.id)
                logger.info(f"Товар {spu_id_int} отправлен в очередь. Task ID: {task.id}")
            except (ValueError, TypeError) as e:
                logger.error(f"Неверный SPU ID: {spu_id}. Ошибка: {e}")

        return jsonify({
            'success': True,
            'message': f'Задачи для {len(task_ids)} товаров успешно созданы.',
            'task_ids': task_ids
        })
        
    except Exception as e:
        logger.error(f"Ошибка в /api/upload: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/task_status/<task_id>', methods=['GET'])
def get_task_status(task_id: str):
    """
    Retrieves the status of a Celery background task.
    
    This is polled by the frontend to show real-time progress.
    """
    try:
        task = celery.AsyncResult(task_id)
        
        response_data = {
            'task_id': task_id,
            'state': task.state
        }
        
        if task.state == 'PENDING':
            response_data['status'] = 'Задача в очереди...'
            response_data['progress'] = 0
        elif task.state == 'PROGRESS':
            response_data['status'] = task.info.get('message', 'В обработке...')
            response_data['progress'] = task.info.get('progress', 0)
            response_data['product_id'] = task.info.get('product_id', '')
        elif task.state == 'SUCCESS':
            response_data['status'] = task.info.get('message', 'Завершено')
            response_data['progress'] = 100
            response_data['result'] = task.result
        elif task.state == 'FAILURE':
            response_data['status'] = task.info.get('message', 'Произошла ошибка')
            response_data['progress'] = 0
            # task.result contains the exception
            response_data['error'] = str(task.result)

        return jsonify({'success': True, 'task': response_data})

    except Exception as e:
        logger.error(f"Ошибка получения статуса задачи {task_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500



@app.route('/api/status/<product_id>', methods=['GET'])
def get_product_status(product_id):
    """
    Получает статус обработки товара.
    
    Args:
        product_id: ID товара
        
    Returns:
        JSON со статусом
    """
    # TODO: Реализовать получение статуса из глобального processor
    return jsonify({
        'success': True,
        'status': 'pending'
    })


@app.route('/api/wordpress/categories', methods=['GET'])
def get_wordpress_categories():
    """
    Получает дерево категорий из WordPress для фильтра.
    
    Returns:
        JSON с деревом категорий
    """
    try:
        categories = []
        
        # Строим дерево из загруженных категорий
        for cat_id, cat_data in woocommerce_client.category_tree.items():
            path = woocommerce_client._build_category_path(cat_id)
            categories.append({
                'id': cat_id,
                'name': cat_data['name'],
                'parent': cat_data['parent'],
                'path': path,
                'slug': cat_data['slug']
            })
        
        # Сортируем по пути
        categories.sort(key=lambda x: x['path'])
        
        logger.info(f"Отправлено категорий: {len(categories)}")
        return jsonify({
            'success': True,
            'categories': categories
        })
        
    except Exception as e:
        logger.error(f"Ошибка получения категорий: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/wordpress/products', methods=['GET'])
def get_wordpress_products():
    """
    Получает список товаров из WordPress для обновления (ЛЕГКОВЕСНЫЙ - без загрузки вариаций).
    Товары всегда отсортированы от старых к новым по дате обновления.
    
    Query params:
        page: номер страницы (default=1)
        per_page: товаров на странице (default=20)
        categories: Список ID категорий через запятую (необязательно)
        date_created_after: дата в формате YYYY-MM-DD (фильтр создания)
        product_id: ID конкретного товара для поиска (необязательно)
    
    Returns:
        JSON с товарами, пагинацией, без полной загрузки вариаций
        Товары отсортированы по дате обновления (от старых к новым)
    """
    try:
        # Параметры пагинации
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 20))
        
        # Фильтры
        category_filter = request.args.get('categories', '')
        date_created_after = request.args.get('date_created_after', '')
        product_id = request.args.get('product_id', '')  # Новый параметр для поиска по ID
        
        # Если указан product_id - ищем только этот товар
        if product_id:
            try:
                product_id_int = int(product_id)
                logger.info(f"Поиск товара по ID: {product_id_int}")
                
                # Запрос одного товара
                url = f"{woocommerce_client.url}/wp-json/wc/v3/products/{product_id_int}"
                
                response = requests.get(
                    url,
                    auth=woocommerce_client.auth,
                    verify=False,
                    timeout=30
                )
                
                if response.status_code == 404:
                    return jsonify({
                        'success': False,
                        'error': f'Товар с ID {product_id_int} не найден'
                    }), 404
                
                response.raise_for_status()
                product = response.json()
                
                # Проверяем что это вариативный товар
                if product.get('type') != 'variable':
                    return jsonify({
                        'success': False,
                        'error': f'Товар ID {product_id_int} не является вариативным товаром'
                    }), 400
                
                logger.info(f"Найден товар: {product.get('name')}")
                
                # Формируем ответ для одного товара
                result_product = {
                    'id': product['id'],
                    'sku': product.get('sku', ''),
                    'name': product.get('name', ''),
                    'image': product.get('images', [{}])[0].get('src', '') if product.get('images') else '',
                    'date_created': product.get('date_created', ''),
                    'date_modified': product.get('date_modified', ''),
                }
                
                return jsonify({
                    'success': True,
                    'products': [result_product],
                    'pagination': {
                        'current_page': 1,
                        'per_page': 1,
                        'total_pages': 1,
                        'total_items': 1
                    }
                })
                
            except ValueError:
                return jsonify({
                    'success': False,
                    'error': 'product_id должен быть числом'
                }), 400
        
        # Обычный поиск со списком товаров
        selected_category_ids = []
        if category_filter:
            selected_category_ids = [int(c.strip()) for c in category_filter.split(',') if c.strip().isdigit()]
        
        logger.info(f"Запрос товаров WordPress: page={page}, per_page={per_page}, categories={selected_category_ids}")
        
        # Параметры запроса к WordPress API
        # Всегда сортируем от старых к новым по дате обновления
        params = {
            'type': 'variable',  # Только вариативные товары
            'page': page,
            'per_page': per_page,
            'orderby': 'modified',
            'order': 'asc'  # От старых к новым по дате обновления
        }
        
        if selected_category_ids:
            params['category'] = ','.join(map(str, selected_category_ids))
        
        if date_created_after:
            params['after'] = date_created_after + 'T00:00:00'
        
        # Запрос к WordPress API
        url = f"{woocommerce_client.url}/wp-json/wc/v3/products"
        
        response = requests.get(
            url,
            auth=woocommerce_client.auth,
            params=params,
            verify=False,
            timeout=30
        )
        response.raise_for_status()
        
        products = response.json()
        total_pages = int(response.headers.get('X-WP-TotalPages', 1))
        total_filtered = int(response.headers.get('X-WP-Total', 0))
        
        logger.info(f"Получено товаров: {len(products)}, всего: {total_filtered}, страниц: {total_pages}")
        
        # Формируем легковесный ответ (БЕЗ загрузки вариаций!)
        result_products = []
        for product in products:
            result_products.append({
                'id': product['id'],
                'sku': product.get('sku', ''),
                'name': product.get('name', ''),
                'image': product.get('images', [{}])[0].get('src', '') if product.get('images') else '',
                'date_created': product.get('date_created', ''),
                'date_modified': product.get('date_modified', ''),
                # Информацию о вариациях получим при обновлении
            })
        
        return jsonify({
            'success': True,
            'products': result_products,
            'pagination': {
                'current_page': page,
                'per_page': per_page,
                'total_pages': total_pages,
                'total_items': total_filtered
            }
        })
        
    except Exception as e:
        logger.error(f"Ошибка получения товаров WordPress: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/update-prices', methods=['POST'])
def update_prices_and_stock():
    """
    Обновляет цены и остатки выбранных товаров из Poizon API.
    Возвращает session_id для отслеживания прогресса через SSE.
    
    Request body:
        {
            "product_ids": [123, 456],
            "settings": {
                "currency_rate": 13.5,
                "markup_rubles": 5000
            }
        }
        
    Returns:
        JSON с session_id для подключения к SSE
    """
    try:
        data = request.get_json()
        product_ids = data.get('product_ids', [])
        settings_data = data.get('settings', {})
        
        if not product_ids:
            return jsonify({
                'success': False,
                'error': 'Не выбраны товары'
            }), 400
        
        # Генерируем уникальный session_id
        session_id = str(uuid.uuid4())
        
        # Создаем очередь для этой сессии
        progress_queues[session_id] = queue.Queue()
        
        # Создаем настройки
        settings = SyncSettings(
            currency_rate=settings_data.get('currency_rate', 13.5),
            markup_rubles=settings_data.get('markup_rubles', 5000)
        )
        
        logger.info(f"Обновление цен: товаров={len(product_ids)}, курс={settings.currency_rate}, наценка={settings.markup_rubles}₽")
        
        # Запускаем обновление в отдельном потоке
        def update_prices_thread():
            results = []
            updated_count = 0
            error_count = 0
            
            try:
                # Отправляем начальное сообщение
                progress_queues[session_id].put({
                    'type': 'start',
                    'total': len(product_ids),
                    'message': f'Начинаем обновление {len(product_ids)} товаров...'
                })
                
                for idx, wc_product_id in enumerate(product_ids, 1):
                    # Отправляем событие начала обработки товара
                    progress_queues[session_id].put({
                        'type': 'product_start',
                        'current': idx,
                        'total': len(product_ids),
                        'product_id': wc_product_id,
                        'message': f'[{idx}/{len(product_ids)}] Обработка товара ID {wc_product_id}...'
                    })
                    
                    try:
                        # Получаем товар из WordPress
                        progress_queues[session_id].put({
                            'type': 'status_update',
                            'message': f'  → Загрузка товара из WordPress...'
                        })
                        
                        url = f"{woocommerce_client.url}/wp-json/wc/v3/products/{wc_product_id}"
                        response = requests.get(url, auth=woocommerce_client.auth, verify=False, timeout=30)
                        response.raise_for_status()
                        wc_product = response.json()
                        
                        sku = wc_product.get('sku', '')
                        product_name = wc_product.get('name', '')
                        
                        logger.info(f"Товар WordPress ID {wc_product_id}: SKU='{sku}', Название='{product_name}'")
                        
                        # ВАЖНО: Ищем сохраненный spuId в meta_data (надежнее чем поиск!)
                        spu_id = None
                        meta_data = wc_product.get('meta_data', [])
                        for meta in meta_data:
                            if meta.get('key') == '_poizon_spu_id':
                                spu_id = int(meta.get('value'))
                                logger.info(f"  Найден сохраненный spuId: {spu_id}")
                                break
                        
                        # Если spuId не найден в meta_data - пробуем по SKU (fallback)
                        if not spu_id:
                            if not sku:
                                progress_queues[session_id].put({
                                    'type': 'product_done',
                                    'current': idx,
                                    'status': 'error',
                                    'message': f'SKU и spuId не найдены'
                                })
                                results.append({'product_id': wc_product_id, 'status': 'error', 'message': 'SKU не найден'})
                                error_count += 1
                                continue
                            
                            # Ищем товар в Poizon по SKU (fallback)
                            progress_queues[session_id].put({
                                'type': 'status_update',
                                'message': f'  → Поиск в Poizon по SKU {sku}...'
                            })
                            
                            search_results = poizon_client.search_products(sku, limit=1)
                            
                            logger.info(f"Fallback: поиск по SKU '{sku}' - найдено={len(search_results) if search_results else 0}")
                            
                            if not search_results or len(search_results) == 0:
                                progress_queues[session_id].put({
                                    'type': 'product_done',
                                    'current': idx,
                                    'status': 'error',
                                    'message': f'[{idx}/{len(product_ids)}] Товар не найден в Poizon'
                                })
                                results.append({'product_id': wc_product_id, 'status': 'error', 'message': 'Товар не найден в Poizon'})
                                error_count += 1
                                continue
                            
                            spu_id = search_results[0].get('spuId')
                            logger.warning(f"  Используем spuId из поиска: {spu_id} (может быть неточно!)")
                            
                            # Сохраняем spuId в meta_data для будущих обновлений
                            try:
                                update_url = f"{woocommerce_client.url}/wp-json/wc/v3/products/{wc_product_id}"
                                update_data = {
                                    'meta_data': [{'key': '_poizon_spu_id', 'value': str(spu_id)}]
                                }
                                requests.put(update_url, auth=woocommerce_client.auth, json=update_data, verify=False, timeout=30)
                                logger.info(f"  Сохранен spuId в meta_data для будущих обновлений")
                            except:
                                pass  # Не критично если не удалось
                        else:
                            logger.info(f"  Используем сохраненный spuId: {spu_id} (надежно!)")
                        
                        # ОПТИМИЗАЦИЯ: Обновляем только цены и остатки (без полной загрузки товара!)
                        progress_queues[session_id].put({
                            'type': 'status_update',
                            'message': f'  → Загрузка цен из Poizon (SPU: {spu_id})...'
                        })
                        
                        # Используем быстрый метод - только цены и остатки, без изображений/переводов/категорий
                        updated = woocommerce_client.update_product_prices_only(
                            wc_product_id,
                            spu_id,
                            settings.currency_rate,
                            settings.markup_rubles,
                            poizon_client  # Передаем клиент Poizon
                        )
                        
                        if updated < 0:  # Ошибка получения цен
                            progress_queues[session_id].put({
                                'type': 'product_done',
                                'current': idx,
                                'status': 'error',
                                'message': f'[{idx}/{len(product_ids)}] Не удалось получить цены'
                            })
                            results.append({'product_id': wc_product_id, 'status': 'error', 'message': 'Не удалось получить цены'})
                            error_count += 1
                            continue
                        
                        # Обновляем вариации
                        progress_queues[session_id].put({
                            'type': 'status_update',
                            'message': f'  → Обновление цен и остатков в WordPress...'
                        })
                        
                        if updated > 0:
                            progress_queues[session_id].put({
                                'type': 'status_update',
                                'message': f'  → Успешно обновлено {updated} вариаций'
                            })
                            
                            progress_queues[session_id].put({
                                'type': 'product_done',
                                'current': idx,
                                'status': 'completed',
                                'message': f'[{idx}/{len(product_ids)}] {product_name}: обновлено {updated} вариаций'
                            })
                            results.append({
                                'product_id': wc_product_id,
                                'product_name': product_name,
                                'status': 'completed',
                                'message': f'Обновлено вариаций: {updated}'
                            })
                            updated_count += 1
                        else:
                            progress_queues[session_id].put({
                                'type': 'product_done',
                                'current': idx,
                                'status': 'warning',
                                'message': f'[{idx}/{len(product_ids)}] {product_name}: SKU не совпадают'
                            })
                            results.append({
                                'product_id': wc_product_id,
                                'status': 'warning',
                                'message': 'Нет совпадающих вариаций'
                            })
                        
                        # Пауза для соблюдения rate limits
                        time.sleep(2)
                    
                    except Exception as e:
                        logger.error(f"Ошибка обновления товара {wc_product_id}: {e}")
                        progress_queues[session_id].put({
                            'type': 'product_done',
                            'current': idx,
                            'status': 'error',
                            'message': f'[{idx}/{len(product_ids)}] Ошибка: {str(e)}'
                        })
                        results.append({
                            'product_id': wc_product_id,
                            'status': 'error',
                            'message': str(e)
                        })
                        error_count += 1
                
                # Отправляем финальное сообщение
                progress_queues[session_id].put({
                    'type': 'complete',
                    'results': results,
                    'total': len(results),
                    'updated': updated_count,
                    'errors': error_count,
                    'message': f'Готово! Обновлено: {updated_count}, Ошибок: {error_count}'
                })
                
                # Сигнал завершения
                progress_queues[session_id].put('DONE')
                
            except Exception as e:
                logger.error(f"Критическая ошибка в потоке обновления: {e}")
                if session_id in progress_queues:
                    progress_queues[session_id].put({
                        'type': 'error',
                        'message': f'Критическая ошибка: {str(e)}'
                    })
                    progress_queues[session_id].put('DONE')
        
        # Запускаем поток
        thread = threading.Thread(target=update_prices_thread, daemon=True)
        thread.start()
        
        # Сразу возвращаем session_id клиенту
        return jsonify({
            'success': True,
            'session_id': session_id,
            'total': len(product_ids)
        })
        
    except Exception as e:
        logger.error(f"Ошибка обновления цен: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


# ============================================================================
# ЗАПУСК ПРИЛОЖЕНИЯ
# ============================================================================

if __name__ == '__main__':
    try:
        # Предотвращаем дублирование логов в режиме DEBUG (Flask запускает процесс дважды)
        # Логи инициализации показываем только в главном процессе
        if os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
            logger.info("="*70)
            logger.info("ЗАПУСК ВЕБ-ПРИЛОЖЕНИЯ POIZON → WORDPRESS")
            logger.info("="*70)
        
        # Инициализация сервисов
        init_services()
        
        # Запуск Flask
        port = int(os.getenv('WEB_APP_PORT', 5000))
        debug = os.getenv('WEB_APP_DEBUG', 'True').lower() == 'true'
        
        # Логи запуска показываем только в главном процессе
        if os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
            logger.info(f"Запуск веб-сервера на http://localhost:{port}")
            logger.info("Для остановки нажмите Ctrl+C")
            logger.info("="*70)
        
        # Для продакшена используем 0.0.0.0 для доступа извне
        # В продакшене host должен быть 0.0.0.0, но безопасность обеспечивается Nginx
        app.run(
            host='0.0.0.0',  # Изменено для доступа извне через Nginx
            port=port,
            debug=debug
        )
        
    except Exception as e:
        logger.error(f"[ERROR] Критическая ошибка при запуске: {e}")
        raise

