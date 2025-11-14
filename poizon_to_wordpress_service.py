"""
Сервис синхронизации товаров из Poizon в WordPress WooCommerce.

Этот модуль содержит классы для работы с WooCommerce API и синхронизации
товаров из Poizon (через poizon_api_fixed.py) в интернет-магазин на WordPress.

Основные компоненты:
    - SyncSettings: Настройки синхронизации (курс валюты, наценка, фильтры)
    - PoisonProduct: Структура данных товара из Poizon
    - WooCommerceService: Клиент для работы с WooCommerce REST API
    - PoisonToWordPressService: Главный сервис синхронизации

"""
import os
import logging
import requests
from typing import Dict, List, Optional, Tuple
from pathlib import Path
from dotenv import load_dotenv
from dataclasses import dataclass
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import time

# Импортируем рабочий клиент Poizon API
from poizon_api_fixed import PoisonAPIClientFixed

# Импортируем обработчик изображений
from image_processor import resize_image_to_square

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('poizon_sync_service.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


@dataclass
class SyncSettings:
    """Настройки синхронизации"""
    currency_rate: float = 13.5  # Курс валюты (юань → рубль)
    markup_rubles: float = 0.0  # Наценка в РУБЛЯХ (не процентах!)
    selected_categories: List[str] = None  # Фильтр по категориям
    selected_brands: List[str] = None  # Фильтр по брендам
    selected_spu_ids: List[int] = None  # Конкретные spuId для синхронизации
    min_price: float = 0.0  # Минимальная цена товара
    max_price: float = 0.0  # Максимальная цена товара (0 = без лимита)
    
    def apply_price_transformation(self, price_yuan: float) -> float:
        """
        Применяет курс валюты и наценку к цене.
        
        Args:
            price_yuan: Исходная цена в юанях
            
        Returns:
            Итоговая цена в рублях с наценкой
        """
        # Конвертируем по курсу
        price_rub = price_yuan * self.currency_rate
        
        # Добавляем наценку в рублях
        if self.markup_rubles > 0:
            price_rub = price_rub + self.markup_rubles
        
        # Округляем до целых рублей
        return round(price_rub)


@dataclass
class PoisonProduct:
    """
    Структура данных товара из Poizon API.
    
    Используется для передачи информации о товаре между модулями.
    
    Attributes:
        spu_id: Уникальный идентификатор товара в системе Poizon
        dewu_id: ID товара в системе DEWU (обычно совпадает с spu_id)
        poizon_id: Строковое представление ID товара
        sku: SKU товара (артикул для учета)
        title: Название товара
        article_number: Артикул производителя
        brand: Название бренда товара
        category: Категория товара
        images: Список URL изображений товара
        variations: Список вариаций товара (размеры, цвета с ценами)
        attributes: Словарь атрибутов товара (материал, сезон и т.д.)
        description: Описание товара
    """
    spu_id: int
    dewu_id: int
    poizon_id: str
    sku: str
    title: str
    article_number: str
    brand: str
    category: str
    images: List[str]
    variations: List[Dict]
    attributes: Dict
    description: str = ""


class WooCommerceService:
    """
    Клиент для работы с WordPress WooCommerce REST API.
    
    Предоставляет методы для создания и обновления товаров в интернет-магазине
    на базе WooCommerce. Поддерживает вариативные товары с множественными 
    размерами и цветами.
    
    Основные возможности:
        - Создание вариативных товаров (variable products)
        - Управление вариациями (размеры, цвета, цены)
        - Загрузка изображений из URL
        - Автоматическое создание атрибутов
        - Управление категориями
        
    Attributes:
        url (str): URL WordPress сайта
        consumer_key (str): WooCommerce API Consumer Key
        consumer_secret (str): WooCommerce API Consumer Secret
        auth (tuple): Кортеж для HTTP Basic Auth
        category_cache (dict): Кэш категорий {имя: id}
        category_tree (dict): Дерево категорий для навигации
        
    API Documentation: https://woocommerce.github.io/woocommerce-rest-api-docs/
    
    Raises:
        ValueError: Если не указаны обязательные переменные окружения
    """
    
    def __init__(self):
        """
        Инициализирует клиент WooCommerce и загружает категории.
        
        Читает настройки из переменных окружения:
            - WC_URL: адрес WordPress сайта
            - WC_CONSUMER_KEY: ключ API WooCommerce
            - WC_CONSUMER_SECRET: секрет API WooCommerce
        """
        load_dotenv()
        
        self.url = os.getenv('WC_URL', '').rstrip('/')
        self.consumer_key = os.getenv('WC_CONSUMER_KEY')
        self.consumer_secret = os.getenv('WC_CONSUMER_SECRET')
        
        # Для загрузки изображений через WordPress REST API
        self.wp_user = os.getenv('WORDPRESS_USER')
        self.wp_password = os.getenv('WORDPRESS_APP_PASSWORD')
        
        if not all([self.url, self.consumer_key, self.consumer_secret]):
            raise ValueError("WC_URL, WC_CONSUMER_KEY, WC_CONSUMER_SECRET должны быть в .env")
        
        self.auth = (self.consumer_key, self.consumer_secret)
        
        # Авторизация для загрузки изображений (WordPress REST API)
        if self.wp_user and self.wp_password:
            self.wp_auth = (self.wp_user, self.wp_password)
            # Убрано: логи инициализации (дублируются в режиме DEBUG)
        else:
            self.wp_auth = None
            logger.warning("[WARNING] WORDPRESS_USER и WORDPRESS_APP_PASSWORD не указаны - загрузка изображений может не работать")
        
        self.category_cache = {}  # Кеш категорий {name: id}
        self.category_tree = {}  # Дерево категорий {id: {name, parent, slug}}
        self.attribute_cache = {}  # Кеш атрибутов {name: {id, slug}}
        self.term_cache = {}  # Кеш терминов атрибутов {(attr_id, term_name): {id, name, slug}}
        
        # Загружаем существующие категории и атрибуты при инициализации
        self._load_categories()
        self._load_attributes()
        
        # Убрано: логи инициализации (дублируются в режиме DEBUG)
    
    def _load_categories(self):
        """Загружает все категории из WordPress"""
        try:
            url = f"{self.url}/wp-json/wc/v3/products/categories"
            params = {'per_page': 100}  # Загружаем до 100 категорий
            
            response = requests.get(url, auth=self.auth, params=params, verify=False, timeout=30)
            
            if response.status_code == 200:
                categories = response.json()
                
                # Строим дерево категорий
                for cat in categories:
                    cat_id = cat['id']
                    self.category_tree[cat_id] = {
                        'name': cat['name'],
                        'parent': cat['parent'],
                        'slug': cat['slug']
                    }
                    
                    # Строим полный путь для каждой категории
                    path = self._build_category_path(cat_id)
                    self.category_cache[path] = cat_id
                    # Также кешируем по имени последней категории
                    self.category_cache[cat['name']] = cat_id
                
                # Убрано: логи инициализации (дублируются в режиме DEBUG)
            else:
                logger.warning(f"Не удалось загрузить категории: {response.status_code}")
                
        except Exception as e:
            logger.error(f"Ошибка загрузки категорий: {e}")
    
    def _build_category_path(self, category_id: int) -> str:
        """Строит полный путь категории от корня"""
        if category_id not in self.category_tree:
            return ""
        
        path_parts = []
        current_id = category_id
        
        # Идем вверх по дереву до корня
        while current_id > 0 and current_id in self.category_tree:
            cat = self.category_tree[current_id]
            path_parts.insert(0, cat['name'])  # Добавляем в начало
            current_id = cat['parent']
        
        return ' > '.join(path_parts)
    
    def _load_attributes(self):
        """Загружает все глобальные атрибуты товаров из WordPress"""
        try:
            url = f"{self.url}/wp-json/wc/v3/products/attributes"
            
            response = requests.get(url, auth=self.auth, verify=False, timeout=30)
            
            if response.status_code == 200:
                attributes = response.json()
                
                for attr in attributes:
                    attr_id = attr['id']
                    attr_name = attr['name']
                    attr_slug = attr['slug']
                    
                    self.attribute_cache[attr_name] = {
                        'id': attr_id,
                        'slug': attr_slug
                    }
                
                # Убрано: логи инициализации (дублируются в режиме DEBUG)
            else:
                logger.warning(f"Не удалось загрузить атрибуты: {response.status_code}")
                
        except Exception as e:
            logger.error(f"Ошибка загрузки атрибутов: {e}")
    
    def ensure_attribute_exists(self, attribute_name: str) -> Optional[Dict]:
        """
        Проверяет существование глобального атрибута товара и создает его если нужно.
        
        Args:
            attribute_name: Название атрибута (например "Бренд", "Цвет", "Размер")
            
        Returns:
            Словарь с id и slug атрибута или None при ошибке
        """
        # Проверяем кеш
        if attribute_name in self.attribute_cache:
            # Убрано DEBUG: атрибут уже существует
            return self.attribute_cache[attribute_name]
        
        # Создаем новый атрибут
        try:
            url = f"{self.url}/wp-json/wc/v3/products/attributes"
            
            # Генерируем slug из имени
            import re
            import unicodedata
            
            # Транслитерация для русских названий
            translit_map = {
                'Ц': 'ts', 'ц': 'ts', 'Ч': 'ch', 'ч': 'ch', 'Ш': 'sh', 'ш': 'sh',
                'Щ': 'sch', 'щ': 'sch', 'Ю': 'yu', 'ю': 'yu', 'Я': 'ya', 'я': 'ya',
                'А': 'a', 'а': 'a', 'Б': 'b', 'б': 'b', 'В': 'v', 'в': 'v',
                'Г': 'g', 'г': 'g', 'Д': 'd', 'д': 'd', 'Е': 'e', 'е': 'e',
                'Ё': 'yo', 'ё': 'yo', 'Ж': 'zh', 'ж': 'zh', 'З': 'z', 'з': 'z',
                'И': 'i', 'и': 'i', 'Й': 'y', 'й': 'y', 'К': 'k', 'к': 'k',
                'Л': 'l', 'л': 'l', 'М': 'm', 'м': 'm', 'Н': 'n', 'н': 'n',
                'О': 'o', 'о': 'o', 'П': 'p', 'п': 'p', 'Р': 'r', 'р': 'r',
                'С': 's', 'с': 's', 'Т': 't', 'т': 't', 'У': 'u', 'у': 'u',
                'Ф': 'f', 'ф': 'f', 'Х': 'h', 'х': 'h', 'Ы': 'y', 'ы': 'y',
                'Э': 'e', 'э': 'e', 'Ъ': '', 'ъ': '', 'Ь': '', 'ь': ''
            }
            
            slug = attribute_name
            for cyr, lat in translit_map.items():
                slug = slug.replace(cyr, lat)
            
            # Убираем все кроме букв, цифр и дефисов
            slug = re.sub(r'[^a-z0-9-]', '-', slug.lower())
            slug = re.sub(r'-+', '-', slug).strip('-')
            
            data = {
                'name': attribute_name,
                'slug': slug,
                'type': 'select',
                'order_by': 'menu_order',
                'has_archives': False
            }
            
            response = requests.post(url, auth=self.auth, json=data, verify=False, timeout=30)
            
            if response.status_code == 201:
                result = response.json()
                attr_info = {
                    'id': result['id'],
                    'slug': result['slug']
                }
                self.attribute_cache[attribute_name] = attr_info
                logger.info(f"[OK] Создан глобальный атрибут '{attribute_name}': ID={result['id']}, slug='{result['slug']}'")
                return attr_info
            else:
                logger.error(f"Ошибка создания атрибута '{attribute_name}': {response.status_code}")
                logger.error(f"Ответ: {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"Ошибка создания атрибута '{attribute_name}': {e}")
            return None
    
    def create_attribute_term(self, attribute_id: int, term_name: str) -> Optional[Dict]:
        """
        Создает термин (значение) для глобального атрибута с кешированием.
        
        Args:
            attribute_id: ID атрибута
            term_name: Название термина (например "Nike", "Красный", "42")
            
        Returns:
            Словарь с id, name и slug термина или None
        """
        # Проверяем кеш
        cache_key = (attribute_id, term_name)
        if cache_key in self.term_cache:
            # Убрано DEBUG: термин найден в кеше
            return self.term_cache[cache_key]
        
        try:
            url = f"{self.url}/wp-json/wc/v3/products/attributes/{attribute_id}/terms"
            
            # Проверяем существует ли такой термин
            check_response = requests.get(url, auth=self.auth, params={'search': term_name}, verify=False, timeout=30)
            if check_response.status_code == 200:
                existing = check_response.json()
                for term in existing:
                    if term['name'] == term_name:
                        result = {
                            'id': term['id'],
                            'name': term['name'],
                            'slug': term['slug']
                        }
                        # Сохраняем в кеш
                        self.term_cache[cache_key] = result
                        # Убрано DEBUG: термин уже существует
                        return result
            
            # Создаем новый термин
            data = {
                'name': term_name
            }
            
            response = requests.post(url, auth=self.auth, json=data, verify=False, timeout=30)
            
            if response.status_code == 201:
                result_data = response.json()
                result = {
                    'id': result_data['id'],
                    'name': result_data['name'],
                    'slug': result_data['slug']
                }
                # Сохраняем в кеш
                self.term_cache[cache_key] = result
                logger.info(f"  [OK] Создан термин '{term_name}' для атрибута ID={attribute_id}, slug='{result_data['slug']}'")
                return result
            elif response.status_code == 400:
                # Возможно термин уже существует, ищем его
                check_response = requests.get(url, auth=self.auth, verify=False, timeout=30)
                if check_response.status_code == 200:
                    all_terms = check_response.json()
                    for term in all_terms:
                        if term['name'] == term_name:
                            result = {
                                'id': term['id'],
                                'name': term['name'],
                                'slug': term['slug']
                            }
                            # Сохраняем в кеш
                            self.term_cache[cache_key] = result
                            # Убрано DEBUG: термин найден при повторной проверке
                            return result
                return None
            else:
                logger.error(f"Ошибка создания термина '{term_name}': {response.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"Ошибка создания термина '{term_name}': {e}")
            return None
    
    def get_category_id(self, category_path: str) -> int:
        """
        Получает ID категории по пути из загруженных категорий.
        Например: "Каталог > Обувь > Кроссовки" или просто "Каталог"
        
        Args:
            category_path: Путь категории через " > "
            
        Returns:
            ID категории или 0 если не найдена
        """
        # Проверяем точное совпадение пути
        if category_path in self.category_cache:
            cat_id = self.category_cache[category_path]
            logger.info(f"[OK] Найдена категория: '{category_path}' → ID {cat_id}")
            return cat_id
        
        # Пробуем найти по последнему элементу пути (если полный путь не найден)
        parts = [p.strip() for p in category_path.split('>')]
        if parts:
            last_part = parts[-1]
            if last_part in self.category_cache:
                cat_id = self.category_cache[last_part]
                logger.info(f"[OK] Найдена категория по имени: '{last_part}' → ID {cat_id}")
                return cat_id
        
        # Не найдена
        logger.warning(f"Категория не найдена: '{category_path}'")
        logger.warning(f"Доступные категории (первые 10): {list(self.category_cache.keys())[:10]}")
        logger.info(f"ВАЖНО: Убедитесь, что в WordPress существует категория '{category_path}'")
        return 0
    
    def get_all_products(self, limit: int = 100) -> List[Dict]:
        """
        Получает все товары из WordPress (с пагинацией).
        
        Args:
            limit: Максимальное количество товаров на странице
            
        Returns:
            Список товаров
        """
        try:
            url = f"{self.url}/wp-json/wc/v3/products"
            all_products = []
            page = 1
            
            while True:
                params = {
                    'per_page': limit,
                    'page': page,
                    'type': 'variable'  # Только вариативные товары
                }
                
                response = requests.get(url, auth=self.auth, params=params, verify=False, timeout=30)
                
                if response.status_code == 200:
                    products = response.json()
                    if not products:
                        break
                    
                    all_products.extend(products)
                    # Убрано DEBUG: загружена страница
                    
                    # Проверяем есть ли еще страницы
                    total_pages = int(response.headers.get('X-WP-TotalPages', 1))
                    if page >= total_pages:
                        break
                    
                    page += 1
                else:
                    logger.error(f"Ошибка загрузки товаров: {response.status_code}")
                    break
            
            logger.info(f"[OK] Всего загружено товаров из WordPress: {len(all_products)}")
            return all_products
            
        except Exception as e:
            logger.error(f"Ошибка загрузки товаров: {e}")
            return []
    
    def get_product_variations(self, product_id: int) -> List[Dict]:
        """
        Получает все вариации товара.
        
        Args:
            product_id: ID родительского товара
            
        Returns:
            Список вариаций
        """
        try:
            url = f"{self.url}/wp-json/wc/v3/products/{product_id}/variations"
            params = {'per_page': 100}
            
            response = requests.get(url, auth=self.auth, params=params, verify=False, timeout=30)
            
            if response.status_code == 200:
                variations = response.json()
                # Убрано DEBUG: найдено вариаций
                return variations
            else:
                logger.error(f"Ошибка загрузки вариаций: {response.status_code}")
                return []
                
        except Exception as e:
            logger.error(f"Ошибка получения вариаций: {e}")
            return []
    
    def product_exists(self, sku: str) -> Optional[int]:
        """
        Проверяет существует ли товар с таким SKU.
        
        Args:
            sku: SKU товара
            
        Returns:
            ID товара или None
        """
        try:
            url = f"{self.url}/wp-json/wc/v3/products"
            params = {'sku': sku}
            
            response = requests.get(url, auth=self.auth, params=params, verify=False, timeout=30)
            response.raise_for_status()
            
            products = response.json()
            if products:
                return products[0]['id']
            return None
            
        except Exception as e:
            logger.error(f"[ERROR] Ошибка проверки товара {sku}: {e}")
            return None
    
    def create_product(self, product: PoisonProduct, settings: SyncSettings = None) -> Optional[int]:
        """
        Создает новый товар в WooCommerce.
        
        Args:
            product: Объект товара из Poizon
            
        Returns:
            ID созданного товара или None
        """
        try:
            url = f"{self.url}/wp-json/wc/v3/products"
            
            # Формируем данные товара
            # Используем wordpress_category если доступна, иначе product.category
            category_path = getattr(product, 'wordpress_category', product.category)
            
            logger.info(f"Категория для WordPress:")
            logger.info(f"  product.category: '{product.category}'")
            logger.info(f"  product.wordpress_category: '{getattr(product, 'wordpress_category', 'НЕТ')}'")
            logger.info(f"  Используем: '{category_path}'")
            
            # Получаем ID существующей категории
            category_id = self.get_category_id(category_path)
            if category_id == 0:
                # Попытка перезагрузить категории, если кэш пустой (могло быть из-за таймаута)
                if not self.category_cache:
                    logger.warning("⚠️ Кэш категорий пустой, пробуем перезагрузить...")
                    self._load_categories()
                    # Повторная попытка найти категорию
                    category_id = self.get_category_id(category_path)
                
                if category_id == 0:
                    logger.warning(f"Категория не найдена в WordPress, товар попадет в Uncategorized")
                    logger.warning(f"Проверьте что в WordPress есть категория: '{category_path}'")
                    categories_data = []  # Пустой список = Uncategorized
                else:
                    categories_data = [{'id': category_id}]  # Используем ID категории!
            else:
                categories_data = [{'id': category_id}]  # Используем ID категории!
            
            # Формируем теги только из бренда и модели (без лишнего мусора)
            tags = []
            keywords = getattr(product, 'keywords', '')
            
            # Добавляем только бренд
            if product.brand:
                tags.append({'name': product.brand.strip()})
            
            # Извлекаем название модели из первых 2-3 ключевых слов (пропускаем бренд и описательные слова)
            if keywords:
                # Разбиваем ключевые слова
                kw_list = [kw.strip() for kw in keywords.split(';') if kw.strip()]
                
                # Ищем модель: пропускаем бренд и берём только короткие названия (не описательные)
                for kw in kw_list[:5]:  # Проверяем первые 5 ключевых слов
                    # Пропускаем бренд (уже добавлен)
                    if product.brand and kw.lower() == product.brand.lower():
                        continue
                    
                    # Пропускаем длинные описательные фразы (кроссовки, обувь, беговые и т.д.)
                    if len(kw.split()) > 3:  # Более 3 слов = описание
                        continue
                    
                    # Пропускаем очевидные описательные термины
                    descriptive_terms = ['кроссовки', 'обувь', 'ботинки', 'сандалии', 'сланцы', 
                                       'женская', 'мужская', 'детская', 'унисекс',
                                       'белый', 'черный', 'красный', 'синий', 'зеленый', 'желтый',
                                       'спортивная', 'повседневная', 'беговая', 'баскетбольная']
                    if any(term in kw.lower() for term in descriptive_terms):
                        continue
                    
                    # Если дошли сюда - это скорее всего название модели
                    tags.append({'name': kw})
                    break  # Берём только первую подходящую модель
            
            # Используем SEO title если есть, иначе обычный title
            product_name = getattr(product, 'seo_title', product.title) or product.title
            logger.info(f"Название ДО очистки: {product_name[:100]}")
            
            # КРИТИЧЕСКИ ВАЖНО: Финальная очистка названия от иероглифов!
            import re
            
            def clean_chinese_final(text: str) -> str:
                """ИЗВЛЕКАЕТ только латиницу, цифры и базовые символы из текста"""
                if not text:
                    return ""
                
                # НОВЫЙ ПОДХОД: ИЗВЛЕКАЕМ только нужные символы вместо удаления
                result = []
                for char in text:
                    code = ord(char)
                    # ASCII латиница и цифры
                    if (0x0041 <= code <= 0x005A or   # A-Z
                        0x0061 <= code <= 0x007A or   # a-z
                        0x0030 <= code <= 0x0039 or   # 0-9
                        code == 0x0020 or              # пробел
                        code == 0x002D or              # тире -
                        code == 0x0027 or              # апостроф '
                        code == 0x002E or              # точка .
                        code == 0x002C):               # запятая ,
                        result.append(char)
                    # Полноширинные латинские (конвертируем в обычные)
                    elif 0xFF21 <= code <= 0xFF3A:  # Ａ-Ｚ
                        result.append(chr(code - 0xFEE0))
                    elif 0xFF41 <= code <= 0xFF5A:  # ａ-ｚ
                        result.append(chr(code - 0xFEE0))
                    elif 0xFF10 <= code <= 0xFF19:  # ０-９
                        result.append(chr(code - 0xFEE0))
                    # Все остальное игнорируем (иероглифы, спецсимволы)
                
                text = ''.join(result)
                
                # Убираем множественные пробелы
                text = re.sub(r'\s+', ' ', text).strip()
                text = text.strip(' -.,')
                
                # Если осталось меньше 3 символов - пустая строка
                if not text or len(text) < 3:
                    return ""
                
                return text
            
            # Применяем очистку к названию
            product_name = clean_chinese_final(product_name)
            logger.info(f"Название ПОСЛЕ очистки: '{product_name}'")
            
            # Очищаем бренд от иероглифов (на случай если он еще содержит их)
            brand_clean = clean_chinese_final(product.brand) if product.brand else "Brand"
            
            # Если после очистки пусто или мусор - используем очищенный бренд + артикул
            if not product_name or len(product_name.strip()) < 3 or product_name.strip() in ['-', '-(', '-(-', '(', ')']:
                product_name = f"{brand_clean} {product.article_number}".strip() if hasattr(product, 'article_number') and product.article_number else brand_clean
                logger.warning(f"Название после очистки пустое/мусор, используем бренд+артикул: {product_name}")
            else:
                # Проверяем что бренд уже есть в названии (не обязательно в начале)
                if brand_clean.upper() not in product_name.upper():
                    logger.info(f"Бренд '{brand_clean}' не найден в названии, добавляем")
                    product_name = f"{brand_clean} {product_name}"
            
            logger.info(f"ФИНАЛЬНОЕ название для WordPress: {product_name}")
            
            # Формируем meta_data
            meta_data = [
                {'key': '_poizon_spu_id', 'value': str(product.spu_id)},  # ВАЖНО: сохраняем spuId!
            ]
            if hasattr(product, 'meta_description') and product.meta_description:
                meta_data.append({'key': '_yoast_wpseo_metadesc', 'value': product.meta_description})
            if keywords:
                meta_data.append({'key': '_yoast_wpseo_focuskw', 'value': keywords})
            
            # Загружаем изображения с изменением размера до 600x600
            logger.info(f"  Загрузка изображений для товара...")
            processed_images = []
            article_number = getattr(product, 'article_number', '')
            
            for idx, img_url in enumerate(product.images[:5], 1):  # Первые 5 изображений
                # Формируем имя файла
                filename = f"{product.brand}_{product.title.replace(' ', '_')}_{article_number}_{idx}.jpg"
                filename = filename.replace('/', '_').replace('\\', '_')  # Убираем слэши
                
                # Загружаем изображение с ресайзом (возвращает ID медиафайла)
                media_id = self.upload_resized_image(img_url, filename, size=600)
                
                if media_id:
                    # Используем ID медиафайла вместо URL (избегаем проблем с SSL)
                    processed_images.append({'id': media_id})
                else:
                    # Если не удалось загрузить - используем оригинальный URL
                    logger.warning(f"  Не удалось загрузить изображение {idx}, используем оригинальный URL")
                    processed_images.append({
                        'src': img_url,
                        'alt': f"{product.brand} {product.title} {article_number}"
                    })
            
            data = {
                'name': product_name,
                'type': 'variable',
                'sku': product.sku,
                'description': product.description,
                'short_description': getattr(product, 'short_description', ''),
                'categories': categories_data,  # Используем ID категорий!
                'tags': tags,
                'images': processed_images,  # Загруженные изображения 600x600
                'meta_data': meta_data,
                'attributes': [],
                'status': 'publish'
            }
            
            # Формируем атрибуты
            # ВАЖНО: Используем ГЛОБАЛЬНЫЕ атрибуты WordPress, а не локальные!
            # Это критически важно для корректной работы вариаций в WooCommerce
            
            # 1. Бренд (не для вариаций)
            brand_attr = self.ensure_attribute_exists('Бренд')
            if brand_attr:
                # Создаем термин для бренда и получаем его slug
                brand_term = self.create_attribute_term(brand_attr['id'], product.brand)
                
                if brand_term:
                    # Для глобальных атрибутов используем название термина (не slug!)
                    data['attributes'].append({
                        'id': brand_attr['id'],  # ИСПОЛЬЗУЕМ ГЛОБАЛЬНЫЙ АТРИБУТ!
                        'visible': True,
                        'variation': False,
                        'options': [brand_term['name']]  # NAME, не slug!
                    })
                    
                    # ВАЖНО: Привязываем товар к taxonomy бренда через поле brands
                    # Это автоматически выберет бренд галочкой в админке WordPress
                    data['brands'] = [{
                        'id': brand_term['id'],
                        'name': brand_term['name'],
                        'slug': brand_term['slug']
                    }]
                    
                    logger.info(f"  Бренд привязан: '{brand_term['name']}' (ID: {brand_term['id']})")
                else:
                    logger.warning(f"  Не удалось создать термин для бренда '{product.brand}', используем название")
                    data['attributes'].append({
                        'id': brand_attr['id'],
                        'visible': True,
                        'variation': False,
                        'options': [product.brand]
                    })
            else:
                logger.warning("  Не удалось создать атрибут Бренд, используем локальный")
                data['attributes'].append({
                    'name': 'Бренд',
                    'visible': True,
                    'variation': False,
                    'options': [product.brand]
                })
            
            # 2. Цвет (ДЛЯ ВАРИАЦИЙ, СНАЧАЛА!)
            # УМНАЯ ЛОГИКА: Используем атрибут Цвет только если цветов больше 1
            unique_colors = list(set([v['color'] for v in product.variations if 'color' in v]))
            
            # Убрано DEBUG: цвета из вариаций
            
            # Проверяем: если цвет один - НЕ создаем атрибут Цвет (только Размер)
            use_color_attribute = len(unique_colors) > 1
            
            if unique_colors and use_color_attribute:
                logger.info(f"  ✓ Используем атрибут Цвет ({len(unique_colors)} цветов)")
                # Сортируем цвета для удобства
                unique_colors.sort()
                
                # Создаем глобальный атрибут "Цвет"
                color_attr = self.ensure_attribute_exists('Цвет')
                if color_attr:
                    # ПАРАЛЛЕЛЬНОЕ создание терминов для ускорения!
                    start_time = time.time()
                    color_names = []
                    
                    # Используем ThreadPoolExecutor для параллельных запросов
                    with ThreadPoolExecutor(max_workers=5) as executor:
                        # Создаем задачи для всех цветов
                        future_to_color = {
                            executor.submit(self.create_attribute_term, color_attr['id'], color): color 
                            for color in unique_colors
                        }
                        
                        # Собираем результаты
                        for future in as_completed(future_to_color):
                            color = future_to_color[future]
                            try:
                                color_term = future.result()
                                if color_term:
                                    color_names.append(color_term['name'])
                                else:
                                    color_names.append(color)  # Fallback
                            except Exception as e:
                                logger.error(f"  Ошибка создания термина '{color}': {e}")
                                color_names.append(color)  # Fallback
                    
                    elapsed = time.time() - start_time
                    logger.info(f"  Цвета созданы параллельно за {elapsed:.1f}с: {color_names}")
                    
                    data['attributes'].append({
                        'id': color_attr['id'],  # ИСПОЛЬЗУЕМ ГЛОБАЛЬНЫЙ АТРИБУТ!
                        'visible': True,
                        'variation': True,
                        'options': color_names  # NAMES, не slug'и!
                    })
                else:
                    logger.warning("  Не удалось создать глобальный атрибут Цвет, используем локальный")
                    data['attributes'].append({
                        'name': 'Цвет',
                        'visible': True,
                        'variation': True,
                        'options': unique_colors
                    })
            elif unique_colors and not use_color_attribute:
                logger.info(f"  ⊗ Пропускаем атрибут Цвет (только 1 цвет: '{unique_colors[0]}')")
            else:
                logger.info(f"  ⊗ Нет цветов у вариаций")
            
            # 3. Размер (ДЛЯ ВАРИАЦИЙ, ВТОРЫМ!)
            unique_sizes = list(set([str(v['size']) for v in product.variations]))
            
            # ПРОВЕРЯЕМ: Есть ли размеры вообще?
            if unique_sizes:
                # Сортируем размеры в правильном порядке
                size_order = ['XS', 'S', 'M', 'L', 'XL', '2XL', '3XL', '4XL', '5XL']
                sorted_sizes = []
                for s in size_order:
                    if s in unique_sizes:
                        sorted_sizes.append(s)
                # Добавляем числовые размеры (обувь)
                numeric_sizes = sorted([s for s in unique_sizes if s not in size_order], key=lambda x: float(x) if x.replace('.', '').isdigit() else 999)
                sorted_sizes.extend(numeric_sizes)
                
                final_sizes = sorted_sizes if sorted_sizes else unique_sizes
            else:
                final_sizes = []
                logger.info("  ⊗ Нет размеров у вариаций (товар без вариаций)")
            
            # Создаем глобальный атрибут "Размер" ТОЛЬКО если есть размеры
            if final_sizes:
                size_attr = self.ensure_attribute_exists('Размер')
            else:
                size_attr = None
                
            if size_attr and final_sizes:
                # ПАРАЛЛЕЛЬНОЕ создание терминов для ускорения!
                start_time = time.time()
                size_names = []
                
                # Используем ThreadPoolExecutor для параллельных запросов
                with ThreadPoolExecutor(max_workers=10) as executor:
                    # Создаем задачи для всех размеров
                    future_to_size = {
                        executor.submit(self.create_attribute_term, size_attr['id'], size): size 
                        for size in final_sizes
                    }
                    
                    # Собираем результаты в правильном порядке
                    size_results = {}
                    for future in as_completed(future_to_size):
                        size = future_to_size[future]
                        try:
                            size_term = future.result()
                            if size_term:
                                size_results[size] = size_term['name']
                            else:
                                size_results[size] = size  # Fallback
                        except Exception as e:
                            logger.error(f"  Ошибка создания термина '{size}': {e}")
                            size_results[size] = size  # Fallback
                    
                    # Восстанавливаем правильный порядок
                    size_names = [size_results[size] for size in final_sizes]
                
                elapsed = time.time() - start_time
                logger.info(f"  Размеры созданы параллельно за {elapsed:.1f}с: {size_names}")
                
                data['attributes'].append({
                    'id': size_attr['id'],  # ИСПОЛЬЗУЕМ ГЛОБАЛЬНЫЙ АТРИБУТ!
                    'visible': True,
                    'variation': True,
                    'options': size_names  # NAMES, не slug'и!
                })
            elif final_sizes:
                # Fallback: если не удалось создать глобальный атрибут, используем локальный
                logger.warning("  Не удалось создать глобальный атрибут Размер, используем локальный")
                data['attributes'].append({
                    'name': 'Размер',
                    'visible': True,
                    'variation': True,
                    'options': final_sizes
                })
            # Если final_sizes пустой - просто не добавляем атрибут Размер
            
            # Добавляем ВСЕ дополнительные атрибуты (КРОМЕ Цвета, Размера и Бренда!)
            for attr_name, attr_value in product.attributes.items():
                if attr_name not in ['Бренд', 'Размер', 'Size', 'Цвет', 'Color']:
                    data['attributes'].append({
                        'name': attr_name,
                        'visible': True,
                        'variation': False,
                        'options': [str(attr_value)]
                    })
            
            # Убрано DEBUG: атрибуты для отправки в WordPress
            
            # Пробуем создать товар с retry logic (SSL может глючить)
            max_retries = 3
            last_error = None
            
            for attempt in range(max_retries):
                try:
                    response = requests.post(url, auth=self.auth, json=data, verify=False, timeout=60)
                    response.raise_for_status()
                    
                    result = response.json()
                    product_id = result['id']
                    break  # Успешно создали
                    
                except Exception as e:
                    last_error = e
                    error_msg = str(e)
                    # Логируем детали ошибки
                    if hasattr(e, 'response') and e.response is not None:
                        try:
                            error_detail = e.response.json()
                            logger.error(f"  WordPress ответ: {error_detail}")
                        except:
                            logger.error(f"  WordPress ответ: {e.response.text[:200]}")
                    logger.warning(f"  Попытка {attempt+1}/{max_retries} не удалась: {error_msg}")
                    if attempt < max_retries - 1:
                        time.sleep(2)  # Пауза перед повтором
                        continue
                    else:
                        raise  # Последняя попытка - пробрасываем ошибку
            
            logger.info(f"[OK] Создан товар ID {product_id}: {product.title[:50]}")
            
            # Создаем вариации
            if settings is None:
                settings = SyncSettings()
            
            # Передаем информацию о том, используется ли атрибут Цвет
            self._create_variations(product_id, product, settings, use_color_attribute)
            
            return product_id
            
        except Exception as e:
            logger.error(f"[ERROR] Ошибка создания товара {product.sku}: {e}")
            return None
    
    def _create_variations(self, product_id: int, product: PoisonProduct, settings: SyncSettings, use_color_attribute: bool = True):
        """
        Создает вариации для товара ПАРАЛЛЕЛЬНО для ускорения.
        
        Args:
            product_id: ID родительского товара в WordPress
            product: Объект товара из Poizon
            settings: Настройки синхронизации (курс, наценка)
            use_color_attribute: Использовать ли атрибут Цвет (False если цвет один)
        """
        url_base = f"{self.url}/wp-json/wc/v3/products/{product_id}/variations"
        
        # Получаем slug'и для атрибутов вариаций
        color_slug = None
        size_slug = None
        
        if 'Цвет' in self.attribute_cache:
            color_slug = self.attribute_cache['Цвет']['slug']
        if 'Размер' in self.attribute_cache:
            size_slug = self.attribute_cache['Размер']['slug']
        
        logger.info(f"  Создание {len(product.variations)} вариаций параллельно...")
        start_time = time.time()
        
        def create_single_variation(idx_var_tuple):
            """Вспомогательная функция для создания одной вариации"""
            idx, variation = idx_var_tuple
            try:
                # Применяем курс и наценку к цене
                final_price = settings.apply_price_transformation(variation['price'])
                
                # Формируем атрибуты вариации
                var_attributes = []
                
                # Цвет (ТОЛЬКО если используется атрибут Цвет - т.е. цветов > 1)
                if use_color_attribute and 'color' in variation and variation['color']:
                    if color_slug:
                        var_attributes.append({
                            'id': self.attribute_cache['Цвет']['id'],
                            'option': str(variation['color'])
                        })
                    else:
                        var_attributes.append({
                            'name': 'Цвет',
                            'option': str(variation['color'])
                        })
                
                # Размер (всегда добавляем)
                if size_slug:
                    var_attributes.append({
                        'id': self.attribute_cache['Размер']['id'],
                        'option': str(variation['size'])
                    })
                else:
                    var_attributes.append({
                        'name': 'Размер',
                        'option': str(variation['size'])
                    })
                
                var_data = {
                    'sku': variation['sku_id'],
                    'regular_price': str(final_price),
                    'stock_quantity': variation['stock'],
                    'manage_stock': True,
                    'attributes': var_attributes
                }
                
                # Добавляем изображение для вариации (если есть) - загружаем с ресайзом 600x600
                if 'images' in variation and variation['images']:
                    size_str = variation.get('size', '')
                    color_str = variation.get('color', '')
                    
                    # Формируем имя файла для вариации
                    var_filename = f"{product.brand}_{product.title.replace(' ', '_')}_{color_str}_{size_str}.jpg"
                    var_filename = var_filename.replace('/', '_').replace('\\', '_')
                    
                    # Загружаем изображение вариации с ресайзом (возвращает ID медиафайла)
                    var_image_id = self.upload_resized_image(
                        variation['images'][0], 
                        var_filename, 
                        size=600
                    )
                    
                    if var_image_id:
                        # Используем ID медиафайла вместо URL
                        var_data['image'] = {'id': var_image_id}
                    else:
                        # Fallback - используем оригинальный URL
                        var_data['image'] = {
                            'src': variation['images'][0],
                            'alt': f"{product.brand} {product.title} {color_str} {size_str} размер"
                        }
                
                # Отправляем запрос с retry
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        response = requests.post(url_base, auth=self.auth, json=var_data, verify=False, timeout=60)
                        response.raise_for_status()
                        created_var = response.json()
                        created_sku = created_var.get('sku', 'NO_SKU')
                        color_log = f", цвет={variation.get('color', 'нет')}" if 'color' in variation else ""
                        return {
                            'success': True,
                            'idx': idx,
                            'size': variation['size'],
                            'color': variation.get('color'),
                            'sku': created_sku,
                            'price': final_price
                        }
                    except requests.exceptions.HTTPError as e:
                        if attempt == max_retries - 1:
                            raise
                        time.sleep(1)
                        continue
                
            except Exception as e:
                logger.error(f"  ❌ Ошибка создания вариации {idx}: {e}")
                return {
                    'success': False,
                    'idx': idx,
                    'error': str(e)
                }
        
        # ПАРАЛЛЕЛЬНОЕ создание всех вариаций
        with ThreadPoolExecutor(max_workers=5) as executor:
            # Enumerate variations for indexing
            indexed_variations = list(enumerate(product.variations, 1))
            
            # Submit all tasks
            futures = [executor.submit(create_single_variation, iv) for iv in indexed_variations]
            
            # Collect results
            results = []
            for future in as_completed(futures):
                try:
                    result = future.result()
                    results.append(result)
                    
                    if result['success']:
                        color_info = f", цвет={result['color']}" if result.get('color') else ""
                        logger.info(f"  ✓ Вариация {result['idx']}/{len(product.variations)}: "
                                  f"размер={result['size']}{color_info}, SKU={result['sku']}, "
                                  f"цена={result['price']}₽")
                except Exception as e:
                    logger.error(f"  ❌ Исключение при получении результата: {e}")
        
        elapsed = time.time() - start_time
        success_count = sum(1 for r in results if r['success'])
        logger.info(f"  Создано вариаций: {success_count}/{len(product.variations)} за {elapsed:.1f}с")
    
    def update_product_variations(self, product_id: int, product: PoisonProduct, settings: SyncSettings = None) -> int:
        """
        Обновляет цены и остатки для вариаций товара.
        
        Args:
            product_id: ID товара в WooCommerce
            product: Объект товара из Poizon
            
        Returns:
            Количество обновленных вариаций
        """
        if settings is None:
            settings = SyncSettings()
        
        try:
            # Получаем существующие вариации
            url = f"{self.url}/wp-json/wc/v3/products/{product_id}/variations"
            response = requests.get(url, auth=self.auth, verify=False, timeout=30)
            response.raise_for_status()
            
            existing_variations = response.json()
            updated_count = 0
            
            logger.info(f"  Poizon вариаций: {len(product.variations)}")
            logger.info(f"  WooCommerce вариаций: {len(existing_variations)}")
            
            # Логируем SKU для отладки
            if product.variations:
                poizon_skus = [v['sku_id'] for v in product.variations[:3]]
                # Убрано DEBUG: примеры SKU из Poizon
            
            if existing_variations:
                wc_skus = [v.get('sku') for v in existing_variations[:3]]
                # Убрано DEBUG: примеры SKU из WooCommerce
            
            # Обновляем по SKU
            for variation in product.variations:
                sku_id = variation['sku_id']
                
                # Применяем курс и наценку к цене
                final_price = settings.apply_price_transformation(variation['price'])
                
                # Ищем соответствующую вариацию в WC
                found = False
                for wc_var in existing_variations:
                    if wc_var.get('sku') == sku_id:
                        var_id = wc_var['id']
                        
                        # Убрано DEBUG: обновляем SKU
                        
                        # Обновляем цену и остаток
                        update_url = f"{self.url}/wp-json/wc/v3/products/{product_id}/variations/{var_id}"
                        update_data = {
                            'regular_price': str(final_price),
                            'stock_quantity': variation['stock']
                        }
                        
                        update_response = requests.put(
                            update_url,
                            auth=self.auth,
                            json=update_data,
                            verify=False,
                            timeout=30
                        )
                        update_response.raise_for_status()
                        
                        updated_count += 1
                        found = True
                        logger.info(f"  [OK] Обновлена вариация SKU={sku_id}, размер={variation.get('size', 'N/A')}")
                        break
                
                if not found:
                    logger.warning(f"  SKU {sku_id} не найден в WooCommerce")
            
            logger.info(f"[OK] Обновлено вариаций: {updated_count} из {len(product.variations)}")
            return updated_count
            
        except Exception as e:
            logger.error(f"[ERROR] Ошибка обновления вариаций товара {product_id}: {e}")
            return 0
    
    def update_product_prices_only(self, product_id: int, spu_id: int, currency_rate: float, markup_rubles: float, poizon_client) -> int:
        """
        Обновляет ТОЛЬКО цены и остатки товара без полной загрузки данных.
        
        Быстрый метод для массового обновления цен:
        - Получает только priceInfo из Poizon API (легкий запрос)
        - Загружает только вариации из WordPress
        - Обновляет только цену и остаток каждой вариации
        
        Args:
            product_id: ID товара в WordPress
            spu_id: ID товара в Poizon
            currency_rate: Курс юаня к рублю
            markup_rubles: Наценка в рублях
            poizon_client: Клиент Poizon API для получения цен
            
        Returns:
            Количество обновленных вариаций
        """
        try:
            # 1. Получаем только цены из Poizon (быстро!)
            prices = poizon_client.get_price_info(spu_id)
            
            if not prices:
                logger.warning(f"  Нет цен для товара {spu_id}")
                return 0
            
            # 2. Получаем только вариации из WooCommerce (без фото и прочего)
            variations_url = f"{self.url}/wp-json/wc/v3/products/{product_id}/variations"
            params = {'per_page': 100}
            
            response = requests.get(
                variations_url,
                auth=self.auth,
                params=params,
                verify=False,
                timeout=30
            )
            response.raise_for_status()
            wc_variations = response.json()
            
            # 3. Обновляем цены параллельно
            updated_count = 0
            
            for wc_var in wc_variations:
                sku_id = wc_var.get('sku')
                
                if not sku_id or sku_id not in prices:
                    continue
                
                price_data = prices[sku_id]
                poizon_price_yuan = price_data['price']
                stock = price_data['stock']
                
                # Рассчитываем финальную цену
                price_rub = poizon_price_yuan * currency_rate
                final_price = int(price_rub + markup_rubles)
                
                # Обновляем вариацию
                var_id = wc_var['id']
                update_url = f"{self.url}/wp-json/wc/v3/products/{product_id}/variations/{var_id}"
                update_data = {
                    'regular_price': str(final_price),
                    'stock_quantity': stock
                }
                
                update_response = requests.put(
                    update_url,
                    auth=self.auth,
                    json=update_data,
                    verify=False,
                    timeout=30
                )
                update_response.raise_for_status()
                
                updated_count += 1
                logger.info(f"  ✓ SKU {sku_id}: {final_price}₽ (остаток: {stock})")
            
            logger.info(f"[OK] Обновлено {updated_count} вариаций для товара {product_id}")
            return updated_count
            
        except Exception as e:
            logger.error(f"[ERROR] Ошибка обновления цен товара {product_id}: {e}")
            return 0
    
    def upload_resized_image(self, image_url: str, filename: str, size: int = 600) -> Optional[str]:
        """
        Загружает изображение с изменением размера до 600x600 с сохранением пропорций.
        
        Args:
            image_url: URL исходного изображения
            filename: Имя файла для сохранения
            size: Размер квадрата (по умолчанию 600x600)
            
        Returns:
            URL загруженного изображения в WordPress Media Library или None при ошибке
        """
        try:
            # 1. Скачиваем и обрабатываем изображение
            # Убрано DEBUG: обработка изображения
            image_bytes = resize_image_to_square(image_url, size=size)
            
            # 2. Загружаем в WordPress Media Library
            upload_url = f"{self.url}/wp-json/wp/v2/media"
            
            # Транслитерация имени файла для HTTP заголовка (только ASCII символы)
            import re
            import unicodedata
            
            # Убираем кириллицу и спецсимволы, оставляем только ASCII
            safe_filename = unicodedata.normalize('NFKD', filename)
            safe_filename = safe_filename.encode('ascii', 'ignore').decode('ascii')
            safe_filename = re.sub(r'[^\w\s.-]', '', safe_filename)
            safe_filename = re.sub(r'[-\s]+', '_', safe_filename)
            
            # Если после очистки имя пустое - генерируем из timestamp
            if not safe_filename or len(safe_filename) < 3:
                import time
                safe_filename = f"product_image_{int(time.time())}.jpg"
            
            headers = {
                'Content-Disposition': f'attachment; filename={safe_filename}',
                'Content-Type': 'image/jpeg'
            }
            
            # Используем WordPress авторизацию для загрузки изображений
            auth_to_use = self.wp_auth if self.wp_auth else self.auth
            
            response = requests.post(
                upload_url,
                auth=auth_to_use,
                headers=headers,
                data=image_bytes,
                verify=False,
                timeout=60  # Увеличиваем таймаут для загрузки
            )
            
            if response.status_code == 201:
                media_data = response.json()
                media_id = media_data.get('id')
                media_url = media_data.get('source_url')
                logger.info(f"  ✓ Изображение загружено: {media_url} (ID: {media_id})")
                # Возвращаем ID медиафайла для привязки к товару
                return media_id
            else:
                logger.error(f"  ✗ Ошибка загрузки изображения: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"  ✗ Ошибка обработки изображения {image_url}: {e}")
            return None


class PoisonToWordPressService:
    """
    Главный сервис для синхронизации товаров Poizon → WordPress.
    
    Координирует работу между Poizon API и WooCommerce API,
    применяет настройки синхронизации, фильтрует товары.
    
    Attributes:
        poizon: Клиент для работы с Poizon API
        woocommerce: Клиент для работы с WooCommerce API
        settings: Настройки синхронизации (курс, наценка, фильтры)
    """
    
    def __init__(self, settings: SyncSettings = None):
        """
        Инициализирует сервис синхронизации.
        
        Args:
            settings: Настройки синхронизации. Если None, используются настройки по умолчанию
        """
        self.poizon = PoisonAPIClientFixed()
        self.woocommerce = WooCommerceService()
        self.settings = settings or SyncSettings()
        logger.info("[OK] Инициализирован сервис синхронизации Poizon → WordPress")
        logger.info(f"  Курс: {self.settings.currency_rate} юань/руб")
        logger.info(f"  Наценка: {self.settings.markup_rubles} руб")
    
    def filter_products(self, products_list: List[Dict]) -> List[Dict]:
        """
        Фильтрует список товаров по заданным критериям.
        
        Args:
            products_list: Список товаров из Poizon API
            
        Returns:
            Отфильтрованный список товаров
        """
        filtered = products_list
        
        # Фильтр по конкретным spuId
        if self.settings.selected_spu_ids:
            filtered = [p for p in filtered if p.get('spuId') in self.settings.selected_spu_ids]
            logger.info(f"Фильтр по spuId: осталось {len(filtered)} товаров")
        
        # Фильтр по категориям
        if self.settings.selected_categories:
            filtered = [
                p for p in filtered 
                if any(cat.lower() in p.get('categoryName', '').lower() 
                       for cat in self.settings.selected_categories)
            ]
            logger.info(f"Фильтр по категориям: осталось {len(filtered)} товаров")
        
        # Фильтр по брендам
        if self.settings.selected_brands:
            filtered = [
                p for p in filtered 
                if any(brand.lower() in p.get('title', '').lower() 
                       for brand in self.settings.selected_brands)
            ]
            logger.info(f"Фильтр по брендам: осталось {len(filtered)} товаров")
        
        return filtered
    
    def sync_all_products(self, limit: int = 100, update_existing: bool = True):
        """
        Синхронизирует все товары из Poizon в WordPress.
        
        Args:
            limit: Максимальное количество товаров для синхронизации
            update_existing: Обновлять ли существующие товары
        """
        logger.info("="*70)
        logger.info("НАЧАЛО СИНХРОНИЗАЦИИ POIZON → WORDPRESS")
        logger.info("="*70)
        
        # Получаем товары из Poizon
        products_list = self.poizon.get_all_products(limit=limit)
        
        if not products_list:
            logger.warning("Нет товаров для синхронизации")
            return
        
        # Применяем фильтры
        products_list = self.filter_products(products_list)
        
        created_count = 0
        updated_count = 0
        skipped_count = 0
        error_count = 0
        
        for idx, product_basic in enumerate(products_list, 1):
            spu_id = product_basic.get('spuId')
            
            if not spu_id:
                logger.warning(f"Товар {idx}: нет spuId, пропускаем")
                skipped_count += 1
                continue
            
            try:
                logger.info(f"\n[{idx}/{len(products_list)}] Обработка товара spuId {spu_id}")
                
                # Получаем полную информацию о товаре
                product = self.poizon.get_product_full_info(spu_id)
                
                if not product:
                    logger.warning(f"  Не удалось загрузить товар {spu_id}")
                    error_count += 1
                    continue
                
                # Проверяем существует ли товар
                existing_id = self.woocommerce.product_exists(product.sku)
                
                if existing_id:
                    if update_existing:
                        logger.info(f"  Товар существует (ID {existing_id}), обновляем...")
                        self.woocommerce.update_product_variations(existing_id, product, self.settings)
                        updated_count += 1
                    else:
                        logger.info(f"  Товар существует (ID {existing_id}), пропускаем")
                        skipped_count += 1
                else:
                    logger.info(f"  Создаем новый товар...")
                    new_id = self.woocommerce.create_product(product, self.settings)
                    
                    if new_id:
                        created_count += 1
                    else:
                        error_count += 1
                
                # Пауза для соблюдения rate limits (уменьшена для ускорения)
                time.sleep(0.5)  # Было 2 секунды, стало 0.5
                
            except Exception as e:
                logger.error(f"  [ERROR] Ошибка обработки товара {spu_id}: {e}")
                error_count += 1
        
        # Итоги
        logger.info("\n" + "="*70)
        logger.info("СИНХРОНИЗАЦИЯ ЗАВЕРШЕНА")
        logger.info("="*70)
        logger.info(f"  Всего обработано: {len(products_list)}")
        logger.info(f"  Создано новых: {created_count}")
        logger.info(f"  Обновлено: {updated_count}")
        logger.info(f"  Пропущено: {skipped_count}")
        logger.info(f"  Ошибок: {error_count}")
        logger.info("="*70)


def get_sync_settings() -> SyncSettings:
    """
    Интерактивное создание настроек синхронизации.
    
    Returns:
        Объект SyncSettings с настройками пользователя
    """
    print("\n" + "="*70)
    print("НАСТРОЙКИ СИНХРОНИЗАЦИИ")
    print("="*70)
    
    # Курс валюты
    print("\n1. КУРС ВАЛЮТЫ")
    print("   Укажите курс юаня к рублю для пересчета цен")
    currency_rate = float(input("   Курс (например, 13.5): ") or "13.5")
    
    # Наценка
    print("\n2. НАЦЕНКА")
    print("   Укажите процент наценки")
    markup_percent = float(input("   Наценка в % (например, 20): ") or "0")
    
    # Фильтр по товарам
    print("\n3. ФИЛЬТР ПО ТОВАРАМ")
    print("   Выберите способ фильтрации:")
    print("   1 - Все товары")
    print("   2 - Конкретные spuId (через запятую)")
    print("   3 - По категориям")
    print("   4 - По брендам")
    
    filter_choice = input("   Ваш выбор (1-4): ").strip()
    
    selected_spu_ids = None
    selected_categories = None
    selected_brands = None
    
    if filter_choice == "2":
        spu_ids_str = input("   Введите spuId через запятую: ")
        selected_spu_ids = [int(x.strip()) for x in spu_ids_str.split(',') if x.strip()]
    elif filter_choice == "3":
        categories_str = input("   Введите категории через запятую (например: Кроссовки, Ботинки): ")
        selected_categories = [x.strip() for x in categories_str.split(',') if x.strip()]
    elif filter_choice == "4":
        brands_str = input("   Введите бренды через запятую (например: Nike, Adidas): ")
        selected_brands = [x.strip() for x in brands_str.split(',') if x.strip()]
    
    # Фильтр по цене
    print("\n4. ФИЛЬТР ПО ЦЕНЕ (опционально)")
    min_price_str = input("   Минимальная цена в юанях (Enter для пропуска): ").strip()
    max_price_str = input("   Максимальная цена в юанях (Enter для пропуска): ").strip()
    
    min_price = float(min_price_str) if min_price_str else 0.0
    max_price = float(max_price_str) if max_price_str else 0.0
    
    settings = SyncSettings(
        currency_rate=currency_rate,
        markup_percent=markup_percent,
        selected_categories=selected_categories,
        selected_brands=selected_brands,
        selected_spu_ids=selected_spu_ids,
        min_price=min_price,
        max_price=max_price
    )
    
    # Показываем итоговые настройки
    print("\n" + "="*70)
    print("ИТОГОВЫЕ НАСТРОЙКИ:")
    print("="*70)
    print(f"Курс валюты: {settings.currency_rate} юань/руб")
    print(f"Наценка: {settings.markup_percent}%")
    
    # Пример расчета цены
    example_price_yuan = 100
    example_price_rub = settings.apply_price_transformation(example_price_yuan)
    print(f"Пример: 100 юаней = {example_price_rub} рублей")
    
    if settings.selected_spu_ids:
        print(f"Фильтр по spuId: {len(settings.selected_spu_ids)} товаров")
    if settings.selected_categories:
        print(f"Фильтр по категориям: {', '.join(settings.selected_categories)}")
    if settings.selected_brands:
        print(f"Фильтр по брендам: {', '.join(settings.selected_brands)}")
    if settings.min_price > 0:
        print(f"Минимальная цена: {settings.min_price} юаней")
    if settings.max_price > 0:
        print(f"Максимальная цена: {settings.max_price} юаней")
    
    print("="*70)
    
    confirm = input("\nПродолжить с этими настройками? (y/n): ").strip().lower()
    if confirm != 'y':
        print("Отменено")
        exit(0)
    
    return settings


def main():
    """Главная функция"""
    print("\n" + "="*70)
    print("СЕРВИС СИНХРОНИЗАЦИИ POIZON API → WORDPRESS")
    print("="*70)
    
    try:
        # Получаем настройки от пользователя
        settings = get_sync_settings()
        
        # Создаем сервис с настройками
        service = PoisonToWordPressService(settings)
        
        # Выбор режима синхронизации
        print("\n" + "="*70)
        print("РЕЖИМ СИНХРОНИЗАЦИИ")
        print("="*70)
        print("1. Синхронизировать все товары (создать новые)")
        print("2. Обновить существующие товары (цены и остатки)")
        print("3. Полная синхронизация (создать + обновить)")
        
        choice = input("\nВведите номер (1-3): ").strip()
        
        # Максимальное количество товаров
        limit = int(input("Максимум товаров для обработки (100): ") or "100")
        
        # Запускаем синхронизацию
        if choice == "1":
            service.sync_all_products(limit=limit, update_existing=False)
        elif choice == "2":
            service.sync_all_products(limit=limit, update_existing=True)
        elif choice == "3":
            service.sync_all_products(limit=limit, update_existing=True)
        else:
            print("Неверный выбор")
    
    except KeyboardInterrupt:
        print("\n\n[!] Прервано пользователем")
    except Exception as e:
        logger.error(f"[ERROR] Критическая ошибка: {e}")


if __name__ == "__main__":
    main()

