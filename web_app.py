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
from flask import Flask, render_template, jsonify, request, Response, stream_with_context
from dotenv import load_dotenv
from dataclasses import dataclass, asdict
import time
import uuid
from datetime import datetime
import queue
import threading

# Импорт существующих модулей
from poizon_to_wordpress_service import (
    WooCommerceService,
    SyncSettings
)
from poizon_api_fixed import PoisonAPIClientFixed as PoisonAPIService

# Настройка логирования
logging.basicConfig(
    level=logging.DEBUG,  # Включаем DEBUG для детальной отладки
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('web_app.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
load_dotenv()

# Инициализация Flask
app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24)
app.config['JSON_AS_ASCII'] = False

# ============================================================================
# КЭШИРОВАНИЕ (для минимизации запросов к API)
# ============================================================================

class SimpleCache:
    """
    Простой in-memory кэш с TTL для минимизации запросов к Poizon API.
    
    Кэширует результаты API запросов в памяти с временем жизни (TTL).
    Это значительно ускоряет повторные запросы и экономит лимиты API.
    
    Attributes:
        cache (dict): Хранилище данных {ключ: (данные, timestamp, ttl)}
        stats (dict): Статистика использования кэша (hits, misses)
        
    Example:
        >>> cache = SimpleCache()
        >>> cache.set('brands', brands_data, ttl=3600)  # Кэш на 1 час
        >>> brands = cache.get('brands')  # Получение из кэша
    """
    
    def __init__(self):
        """Инициализация пустого кэша со статистикой."""
        self.cache = {}
        self.stats = {
            'hits': 0,
            'misses': 0,
            'requests_saved': 0
        }
    
    def get(self, key):
        """Получить значение из кэша"""
        if key in self.cache:
            data, timestamp, ttl = self.cache[key]
            if time.time() - timestamp < ttl:
                self.stats['hits'] += 1
                logger.debug(f"[CACHE HIT] {key}")
                return data
            else:
                del self.cache[key]
        
        self.stats['misses'] += 1
        logger.debug(f"[CACHE MISS] {key}")
        return None
    
    def set(self, key, value, ttl=3600):
        """Сохранить значение в кэш (TTL в секундах)"""
        self.cache[key] = (value, time.time(), ttl)
        logger.debug(f"[CACHE SET] {key} (TTL: {ttl}s)")
    
    def get_stats(self):
        """Получить статистику кэша"""
        total = self.stats['hits'] + self.stats['misses']
        hit_rate = (self.stats['hits'] / total * 100) if total > 0 else 0
        return {
            'hits': self.stats['hits'],
            'misses': self.stats['misses'],
            'hit_rate': f"{hit_rate:.1f}%",
            'requests_saved': self.stats['requests_saved'],
            'cached_items': len(self.cache)
        }
    
    def clear(self):
        """Очистить весь кэш"""
        self.cache.clear()
        logger.info("[CACHE] Кэш очищен")


# Создаем глобальный кэш
cache = SimpleCache()

# ============================================================================
# КАТЕГОРИИ И ФИЛЬТРАЦИЯ
# ============================================================================

# Словарь категорий и ключевых слов (на основе анализа dewu.com)
CATEGORY_KEYWORDS = {
    # ОБУВЬ (29)
    29: {
        'keywords': ['鞋', '运动鞋', '板鞋', '跑鞋', '篮球鞋', '足球鞋', 
                    'shoes', 'sneakers', 'boots', 'sandals', 'trainers', 'loafers'],
        'search_terms': ['sneakers', 'shoes', 'boots', 'trainers', 'sandals', 'loafers']
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

# Очередь для прогресс-событий (SSE)
progress_queues = {}  # {session_id: queue.Queue()}


@dataclass
class ProcessingStatus:
    """Статус обработки товара"""
    product_id: str
    status: str  # pending, processing, gigachat, wordpress, completed, error
    progress: int
    message: str
    timestamp: str


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
        
        logger.info("[OK] Инициализирован GigaChat клиент")
    
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
            logger.info("[OK] Получен access token GigaChat")
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
2. ЗАТЕМ составь торговое название ТОЛЬКО из латиницы (A-Z) и цифр
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

ИНСТРУКЦИЯ ПО ПЕРЕВОДУ:
- "定制球鞋" → "Custom Sneakers" (или просто убери)
- "阿卡丽" → транслитерация "Akali" или убери если непонятно
- "男款" → "Men's" или "Мужские"
- "黑白" → "Black White" или "Черно-белые"
- Если не можешь перевести - УБЕРИ это слово, НЕ копируй иероглифы!

ДОПОЛНИТЕЛЬНЫЕ ПРАВИЛА:
✓ Никогда не придумывай характеристики, которых нет в данных.
✓ Названия моделей, линейки (Air Jordan 1, Dunk Low, Yeezy 350 V2, Samba OG и т.д.) пиши латиницей и не переводи.
✓ Не упоминай другие бренды.
✓ Пиши живым, разговорным языком: избегай канцелярита «высококачественный, многофункциональный, превосходный».
✓ Используй конкретику: вместо «удобные» – «мягкий воротник не натирает ахилл», вместо «лёгкие» – «вес одной кроссовки 320 г (42 размер)».
✓ Увеличь объём: краткое описание 280-320 зн., полное 650-850 зн.

В ПОЛНОМ ОПИСАНИИ обязательно раскрой:
→ визуальный образ (цвет, фактуры, контрасты);
→ материалы и их тактильные ощущения;
→ технологии (если указаны в attributes: Air, Boost, GORE-TEX и т.д.);
→ с чем носить и куда надевается модель;
→ выгода для покупателя (лёгкость, устойчивость к погоде, легко чистится, идёт в комплекте доп. шнурки и т.д.).

SEO-заголовок ≤ 60 зн., включает бренд и ключевую модель.
Мета-описание 150-160 зн., заканчивается призывом «Купить с доставкой» / «Закажи онлайн».
Ключевые слова: 7-10 слов, без повторов, в именительном падеже, через точку с запятой.

ФОРМАТ ОТВЕТА (ровно 6 строк, без пустых, без комментариев):
1. Название модели СТРОГО в формате: Бренд Модель Артикул (БЕЗ иероглифов, БЕЗ эмодзи, БЕЗ скобок)
2. Краткое описание
3. Полное описание
4. SEO Title (только латиница + кириллица, БЕЗ иероглифов)
5. Meta Description
6. Ключевые слова

КРИТИЧЕСКИ ВАЖНО для строк 1 и 4:
- ❌ ЗАПРЕЩЕНО использовать китайские/японские иероглифы (定制球鞋、阿卡丽、时尚 и т.д.)
- ❌ ЗАПРЕЩЕНО использовать спецсимволы (【】、（）等)
- ✅ ТОЛЬКО латиница (A-Z, a-z) и кириллица (А-Я, а-я)
- ✅ ОБЯЗАТЕЛЬНО начинай с бренда: {brand}
- ✅ Формат строки 1: "{brand} Модель Артикул" (например: Nike Court Borough BQ5448-115)
- ✅ Формат строки 4: "{brand} Модель - купить оригинал" (например: Nike Court Borough - купить оригинал)

Пример ПРАВИЛЬНОГО перевода с китайского:
Исходное название: "【定制球鞋】 Jordan Air Jordan 1 Mid 阿卡丽2 中帮 复古篮球鞋 男款 黑白"
                        ↓ ПЕРЕВОДИМ ↓
1. Jordan Air Jordan 1 Mid Akali 2 Black White DQ8426-154
   (убрали "定制球鞋", перевели "阿卡丽"→"Akali", "黑白"→"Black White", убрали лишнее)

Пример полного ответа (опирайся на стиль):
1. Nike Dunk Low White Black DD1391-103
2. Классический двухцветный Dunk Low: белая кожаная основа + чёрные замши на оверлеях. Подошва средней толщины, отличная плотность строчки.
3. Данный Dunk Low выпущен в 2021 году и повторяет оригинальный цветовой блок 1985-го. Верх полностью из натуральной кожи: гладкая белая на toe-box и медиальной стороне, чёрная замша на swoosh и пяточном ремне. Перфорация в носочной зоне обеспечивает вентиляцию, а внутри – текстильная сетка, приятная к ноге и не вытягивается после 50+ носков. Промежуточная подошва из пеноматериала EVA весит 30 % меньше оригинала, поэтому кроссовок подходит для целодневного городского ношения. Резиновая подметка с концентрическим рисунком держит асфальт и плитку даже в дождь. Идут в комплекте белые шнурки flat, при желании можно заменить на чёрные – в коробке есть вторая пара. Идеальны для джинсов-скинни, карго и летних шорт: универсальный white/black всегда в тренде.
4. Nike Dunk Low White Black – купить оригинал
5. Оригинальные Nike Dunk Low white/black в наличии. Бесплатная примерка, доставка по РФ в день заказа.
6. Nike; Dunk Low; белые; чёрные; кожа; DD1391-103; оригинал; кроссовки"""

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
                """ИЗВЛЕКАЕТ только латиницу, цифры и базовые символы из текста"""
                if not text:
                    return ""
                
                original = text
                logger.info(f"Очистка ШАГ 1 (оригинал): '{text[:80]}'")
                
                # Проверим первые символы (для отладки)
                if len(text) > 0:
                    first_chars = [f"{c}(U+{ord(c):04X})" for c in text[:10]]
                    logger.info(f"Первые 10 символов: {' '.join(first_chars)}")
                
                # НОВЫЙ ПОДХОД: ИЗВЛЕКАЕМ только нужные символы вместо удаления ненужных!
                # Оставляем: A-Z, a-z, 0-9, пробел, тире, апостроф, точку, запятую
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
                        code == 0x002C):               # запятая ,
                        result.append(char)
                    # Полноширинные латинские (FF21-FF5A)
                    elif 0xFF21 <= code <= 0xFF3A:  # Ａ-Ｚ
                        result.append(chr(code - 0xFEE0))
                    elif 0xFF41 <= code <= 0xFF5A:  # ａ-ｚ
                        result.append(chr(code - 0xFEE0))
                    elif 0xFF10 <= code <= 0xFF19:  # ０-９
                        result.append(chr(code - 0xFEE0))
                    # Все остальное игнорируем (иероглифы, спецсимволы и т.д.)
                
                text = ''.join(result)
                logger.info(f"Очистка ШАГ 2 (извлечены нужные символы): '{text[:80]}'")
                
                # Убираем множественные пробелы
                text = re.sub(r'\s+', ' ', text).strip()
                text = text.strip(' -.,')
                logger.info(f"Очистка ШАГ 3 (финальная): '{text[:80]}'")
                
                # Если осталось меньше 3 символов - пустая строка
                if not text or len(text) < 3:
                    logger.warning(f"Очистка ИТОГ: '{original[:50]}' → пустая строка (длина={len(text)})")
                    return ""
                
                logger.info(f"Очистка ИТОГ: '{original[:50]}' → '{text[:80]}'")
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
            
            result = {
                "title_ru": title_clean,
                "short_description": parsed_lines[1] if len(parsed_lines) > 1 else f"Товар {brand}",
                "full_description": parsed_lines[2] if len(parsed_lines) > 2 else f"Описание {title}",
                "seo_title": seo_title_clean,
                "meta_description": parsed_lines[4] if len(parsed_lines) > 4 else f"{brand} - купить онлайн",
                "keywords": parsed_lines[5] if len(parsed_lines) > 5 else f"{brand}, {category}"
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


class ProductProcessor:
    """Обработчик товаров: Poizon → GigaChat → WordPress"""
    
    def __init__(
        self,
        poizon: PoisonAPIService,
        gigachat: GigaChatService,
        woocommerce: WooCommerceService,
        settings: SyncSettings,
        session_id: str = None
    ):
        """Инициализация процессора"""
        self.poizon = poizon
        self.gigachat = gigachat
        self.woocommerce = woocommerce
        self.settings = settings
        self.session_id = session_id
        self.processing_status = {}
    
    def process_product(self, spu_id: int) -> ProcessingStatus:
        """
        Обрабатывает один товар через весь pipeline.
        
        Args:
            spu_id: ID товара в Poizon
            
        Returns:
            Статус обработки
        """
        product_key = str(spu_id)
        
        try:
            # Шаг 1: Получение данных из Poizon
            self._update_status(product_key, 'processing', 10, 'Загрузка из Poizon API...')
            
            product = self.poizon.get_product_full_info(spu_id)
            if not product:
                return self._update_status(product_key, 'error', 0, 'Не удалось загрузить товар')
            
            # КРИТИЧЕСКИ ВАЖНО: ВСЕГДА очищаем бренд из API, НЕ используем override_brand!
            # override_brand используется только для ПОИСКА товаров, но НЕ для названия!
            import re
            def extract_latin_only(text: str) -> str:
                """Извлекает только латиницу, цифры, тире, точку и слэш"""
                if not text:
                    return ""
                result = []
                for char in text:
                    code = ord(char)
                    if (0x0041 <= code <= 0x005A or   # A-Z
                        0x0061 <= code <= 0x007A or   # a-z
                        0x0030 <= code <= 0x0039 or   # 0-9
                        code == 0x0020 or              # пробел
                        code == 0x002D or              # тире -
                        code == 0x002F or              # слэш /
                        code == 0x002E):               # точка .
                        result.append(char)
                return ''.join(result).strip()
            
            original_brand = product.brand
            original_article = product.article_number
            
            # ВСЕГДА очищаем бренд из API (он может содержать иероглифы)
            product.brand = extract_latin_only(product.brand) or "Brand"
            logger.info(f"Бренд из API: '{original_brand}' → '{product.brand}'")
            
            product.article_number = extract_latin_only(product.article_number) or product.article_number
            logger.info(f"Артикул: '{original_article}' → '{product.article_number}'")
            
            # Шаг 2: Обработка через GigaChat
            self._update_status(product_key, 'gigachat', 40, 'Обработка через GigaChat...')
            
            seo_data = self.gigachat.translate_and_generate_seo(
                title=product.title,
                description=product.description,
                category=product.category,
                brand=product.brand,  # Теперь это очищенный бренд!
                attributes=product.attributes,
                article_number=product.article_number
            )
            
            # Обновляем данные товара ВСЕМИ полями из GigaChat
            product.title = seo_data['title_ru']  # Название модели
            product.description = seo_data['full_description']  # Полное описание
            product.short_description = seo_data.get('short_description', '')  # Краткое описание
            product.seo_title = seo_data.get('seo_title', seo_data['title_ru'])  # SEO Title
            product.meta_description = seo_data.get('meta_description', '')  # Meta Description
            product.keywords = seo_data.get('keywords', '')  # Ключевые слова
            
            logger.info(f"Обновленные поля товара:")
            logger.info(f"  product.title: {product.title[:80]}")
            logger.info(f"  product.seo_title: {product.seo_title[:80]}")
            
            # Шаг 3: Проверка существования в WordPress
            self._update_status(product_key, 'wordpress', 70, 'Проверка существования в WordPress...')
            
            existing_id = self.woocommerce.product_exists(product.sku)
            
            if existing_id:
                # Товар уже существует - обновляем только цены и остатки
                logger.info(f"  Товар существует (ID {existing_id}), обновляем цены и остатки...")
                self._update_status(product_key, 'wordpress', 75, f'Обновление товара ID {existing_id}...')
                updated = self.woocommerce.update_product_variations(existing_id, product, self.settings)
                self._update_status(product_key, 'wordpress', 90, f'Обновлено {updated} вариаций товара ID {existing_id}')
                message = f'Обновлен товар ID {existing_id} ({updated} вариаций)'
            else:
                # Создаем новый товар
                logger.info(f"  Создаем новый товар...")
                self._update_status(product_key, 'wordpress', 75, 'Создание нового товара в WordPress...')
                
                self._update_status(product_key, 'wordpress', 80, f'Загрузка основной информации (название, цена, категория)...')
                new_id = self.woocommerce.create_product(product, self.settings)
                
                if new_id:
                    self._update_status(product_key, 'wordpress', 95, f'Товар успешно создан (ID {new_id})')
                    message = f'Создан товар ID {new_id}'
                else:
                    return self._update_status(product_key, 'error', 0, 'Ошибка создания товара в WordPress')
            
            # Шаг 4: Завершено
            return self._update_status(product_key, 'completed', 100, message)
            
        except Exception as e:
            logger.error(f"Ошибка обработки товара {spu_id}: {e}")
            return self._update_status(product_key, 'error', 0, f'Ошибка: {str(e)}')
    
    def _update_status(
        self,
        product_id: str,
        status: str,
        progress: int,
        message: str
    ) -> ProcessingStatus:
        """Обновляет статус обработки товара и отправляет событие в SSE"""
        status_obj = ProcessingStatus(
            product_id=product_id,
            status=status,
            progress=progress,
            message=message,
            timestamp=datetime.now().isoformat()
        )
        self.processing_status[product_id] = status_obj
        
        # Отправляем событие в SSE если есть session_id
        if self.session_id and self.session_id in progress_queues:
            progress_queues[self.session_id].put({
                'type': 'status_update',
                'product_id': product_id,
                'status': status,
                'progress': progress,
                'message': message
            })
        
        return status_obj
    
    def get_status(self, product_id: str) -> Optional[ProcessingStatus]:
        """Получает статус обработки товара"""
        return self.processing_status.get(product_id)


# Инициализация при запуске
def init_services():
    """Инициализация всех сервисов"""
    global poizon_client, woocommerce_client, gigachat_client
    
    try:
        poizon_client = PoisonAPIService()
        woocommerce_client = WooCommerceService()
        gigachat_client = GigaChatService()
        logger.info("[OK] Все сервисы инициализированы")
    except Exception as e:
        logger.error(f"[ERROR] Ошибка инициализации сервисов: {e}")
        raise


# ============================================================================
# API ENDPOINTS
# ============================================================================

@app.route('/')
def index():
    """Главная страница"""
    return render_template('index.html')


@app.route('/api/brands', methods=['GET'])
def get_brands():
    """
    Получает список всех доступных брендов.
    
    Returns:
        JSON список брендов
    """
    try:
        # Получаем бренды через API
        brands_data = poizon_client.get_brands(limit=100)
        
        # Извлекаем названия брендов
        brands_list = []
        for brand in brands_data:
            brand_name = brand.get('name', '')
            if brand_name and brand_name != '热门系列':  # Пропускаем "Горячие серии"
                brands_list.append({
                    'id': brand.get('id'),
                    'name': brand_name,
                    'logo': brand.get('logo', '')
                })
        
        logger.info(f"Найдено брендов: {len(brands_list)}")
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
            logger.info(f"[ОБУВЬ] Загружаем ВСЕ бренды из API (быстрая загрузка)")
            
            # Получаем все бренды
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
                logger.info(f"[CACHE] Информация о брендах сохранена")
            
            # Сортируем по алфавиту
            brands_list = sorted(all_brands_info, key=lambda x: x['name'])
            
            logger.info(f"[ОБУВЬ] Возвращаем {len(brands_list)} брендов (все бренды API)")
            
            # Кэшируем на 24 часа (для обуви долгий кэш)
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
        products = poizon_client.search_products(keyword=query, limit=50)
        
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
        page = int(request.args.get('page', 0))
        limit = int(request.args.get('limit', 20))
        
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
        
        logger.info(f"Поиск товаров: brand={brand}, category_id={category_id}")
        
        # МНОЖЕСТВЕННАЯ ПАГИНАЦИЯ для получения ВСЕХ товаров!
        all_products = []
        max_pages = 5  # До 500 товаров
        
        for p in range(max_pages):
            products_page = poizon_client.search_products(keyword=keyword, limit=100, page=p)
            
            if not products_page or len(products_page) == 0:
                break
            
            all_products.extend(products_page)
            logger.info(f"  Страница {p}: найдено {len(products_page)} товаров")
            
            if len(products_page) < 100:
                break
        
        logger.info(f"ВСЕГО найдено товаров: {len(all_products)}")
        
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
        
        logger.info(f"Возвращаем товаров: {len(formatted_products)}")
        return jsonify({
            'success': True,
            'products': formatted_products,
            'total': len(formatted_products),
            'page': page,
            'has_more': False  # Все товары загружены
        })
        
    except Exception as e:
        logger.error(f"Ошибка получения товаров: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/progress/<session_id>')
def progress_stream(session_id):
    """
    SSE endpoint для получения прогресса обработки в реальном времени
    """
    def generate():
        # Создаем очередь для этой сессии если ее нет
        if session_id not in progress_queues:
            progress_queues[session_id] = queue.Queue()
        
        q = progress_queues[session_id]
        
        try:
            while True:
                # Ждем сообщение из очереди (timeout 30 сек)
                try:
                    message = q.get(timeout=30)
                    
                    # Если получили 'DONE' - завершаем
                    if message == 'DONE':
                        yield f"data: {json.dumps({'type': 'done'})}\n\n"
                        break
                    
                    # Отправляем сообщение клиенту
                    yield f"data: {json.dumps(message)}\n\n"
                    
                except queue.Empty:
                    # Отправляем keepalive каждые 30 секунд
                    yield f"data: {json.dumps({'type': 'keepalive'})}\n\n"
                    
        finally:
            # Очищаем очередь после завершения
            if session_id in progress_queues:
                del progress_queues[session_id]
    
    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no'
        }
    )


@app.route('/api/upload', methods=['POST'])
def upload_products():
    """
    Загружает выбранные товары в WordPress через GigaChat.
    Возвращает session_id для отслеживания прогресса через SSE.
    
    Request body:
        {
            "product_ids": [123, 456, 789],
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
        
        logger.info(f"Загрузка товаров: ids={product_ids}")
        
        # Запускаем обработку в отдельном потоке
        def process_products_thread():
            try:
                # Создаем процессор с передачей session_id
                processor = ProductProcessor(
                    poizon_client,
                    gigachat_client,
                    woocommerce_client,
                    settings,
                    session_id  # Передаем session_id в процессор
                )
                
                # Отправляем начальное сообщение
                progress_queues[session_id].put({
                    'type': 'start',
                    'total': len(product_ids),
                    'message': f'Начинаем обработку {len(product_ids)} товаров...'
                })
                
                # Обрабатываем товары
                results = []
                for idx, spu_id in enumerate(product_ids, 1):
                    progress_queues[session_id].put({
                        'type': 'product_start',
                        'current': idx,
                        'total': len(product_ids),
                        'spu_id': spu_id,
                        'message': f'[{idx}/{len(product_ids)}] Обработка товара {spu_id}...'
                    })
                    
                    # Обрабатываем товар (бренд будет взят из Poizon API и очищен)
                    status = processor.process_product(spu_id)
                    results.append(asdict(status))
                    
                    # Отправляем результат обработки товара
                    progress_queues[session_id].put({
                        'type': 'product_done',
                        'current': idx,
                        'total': len(product_ids),
                        'spu_id': spu_id,
                        'status': status.status,
                        'message': status.message
                    })
                
                # Отправляем финальное сообщение
                completed = sum(1 for r in results if r['status'] == 'completed')
                errors = sum(1 for r in results if r['status'] == 'error')
                
                progress_queues[session_id].put({
                    'type': 'complete',
                    'results': results,
                    'total': len(results),
                    'completed': completed,
                    'errors': errors,
                    'message': f'Готово! Успешно: {completed}, Ошибок: {errors}'
                })
                
                # Сигнал завершения
                progress_queues[session_id].put('DONE')
                
            except Exception as e:
                logger.error(f"Ошибка в потоке обработки: {e}")
                if session_id in progress_queues:
                    progress_queues[session_id].put({
                        'type': 'error',
                        'message': f'Критическая ошибка: {str(e)}'
                    })
                    progress_queues[session_id].put('DONE')
        
        # Запускаем поток
        thread = threading.Thread(target=process_products_thread, daemon=True)
        thread.start()
        
        # Сразу возвращаем session_id клиенту
        return jsonify({
            'success': True,
            'session_id': session_id,
            'total': len(product_ids)
        })
        
    except Exception as e:
        logger.error(f"Ошибка загрузки товаров: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


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
    Получает список товаров из WordPress для обновления.
    
    Query params:
        categories: Список ID категорий через запятую (необязательно)
    
    Returns:
        JSON список товаров с текущими ценами и остатками
    """
    try:
        # Получаем фильтр по категориям
        category_filter = request.args.get('categories', '')
        selected_category_ids = []
        if category_filter:
            selected_category_ids = [int(c.strip()) for c in category_filter.split(',') if c.strip().isdigit()]
            logger.info(f"Фильтр по категориям: {selected_category_ids}")
        
        # Загружаем товары
        # Получаем товары из WordPress
        wc_products = woocommerce_client.get_all_products()
        
        # Фильтруем только variable товары
        variable_products = []
        for product in wc_products:
            if product.get('type') == 'variable':
                # Фильтр по категориям (если указан)
                if selected_category_ids:
                    product_categories = product.get('categories', [])
                    product_category_ids = [cat['id'] for cat in product_categories]
                    
                    # Проверяем что товар в одной из выбранных категорий
                    if not any(cat_id in selected_category_ids for cat_id in product_category_ids):
                        continue  # Пропускаем товар если он не в выбранных категориях
                
                # Получаем вариации
                product_id = product['id']
                variations = woocommerce_client.get_product_variations(product_id)
                
                # Подсчитываем общий остаток
                total_stock = sum(int(v.get('stock_quantity', 0) or 0) for v in variations)
                
                # Находим мин/макс цены
                prices = [float(v.get('regular_price', 0) or 0) for v in variations if v.get('regular_price')]
                min_price = min(prices) if prices else 0
                max_price = max(prices) if prices else 0
                
                variable_products.append({
                    'id': product_id,
                    'sku': product.get('sku', ''),
                    'name': product.get('name', ''),
                    'image': product.get('images', [{}])[0].get('src', '') if product.get('images') else '',
                    'variations_count': len(variations),
                    'total_stock': total_stock,
                    'min_price': min_price,
                    'max_price': max_price,
                    'date_created': product.get('date_created', ''),
                    'date_modified': product.get('date_modified', '')
                })
        
        logger.info(f"Найдено товаров в WordPress: {len(variable_products)}")
        return jsonify({
            'success': True,
            'products': variable_products,
            'total': len(variable_products)
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
                        
                        # Получаем полную информацию о товаре
                        progress_queues[session_id].put({
                            'type': 'status_update',
                            'message': f'  → Загрузка вариаций из Poizon (SPU: {spu_id})...'
                        })
                        
                        full_product = poizon_client.get_product_full_info(spu_id)
                        
                        if not full_product or not full_product.variations:
                            progress_queues[session_id].put({
                                'type': 'product_done',
                                'current': idx,
                                'status': 'error',
                                'message': f'[{idx}/{len(product_ids)}] Вариаций не найдено'
                            })
                            results.append({'product_id': wc_product_id, 'status': 'error', 'message': 'Вариаций не найдено'})
                            error_count += 1
                            continue
                        
                        # Обновляем вариации
                        progress_queues[session_id].put({
                            'type': 'status_update',
                            'message': f'  → Найдено {len(full_product.variations)} вариаций'
                        })
                        
                        progress_queues[session_id].put({
                            'type': 'status_update',
                            'message': f'  → Обновление цен и остатков в WordPress...'
                        })
                        
                        updated = woocommerce_client.update_product_variations(
                            wc_product_id,
                            full_product,
                            settings
                        )
                        
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
        logger.info("="*70)
        logger.info("ЗАПУСК ВЕБ-ПРИЛОЖЕНИЯ POIZON → WORDPRESS")
        logger.info("="*70)
        
        # Инициализация сервисов
        init_services()
        
        # Запуск Flask
        port = int(os.getenv('WEB_APP_PORT', 5000))
        debug = os.getenv('WEB_APP_DEBUG', 'True').lower() == 'true'
        
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

