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
import time

# Импортируем рабочий клиент Poizon API
from poizon_api_fixed import PoisonAPIClientFixed

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
        
        if not all([self.url, self.consumer_key, self.consumer_secret]):
            raise ValueError("WC_URL, WC_CONSUMER_KEY, WC_CONSUMER_SECRET должны быть в .env")
        
        self.auth = (self.consumer_key, self.consumer_secret)
        self.category_cache = {}  # Кеш категорий {name: id}
        self.category_tree = {}  # Дерево категорий {id: {name, parent, slug}}
        
        # Загружаем существующие категории при инициализации
        self._load_categories()
        
        logger.info(f"[OK] Инициализирован WooCommerce клиент: {self.url}")
    
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
                
                logger.info(f"[OK] Загружено категорий из WordPress: {len(categories)}")
                logger.info(f"  Примеры: {list(self.category_cache.keys())[:5]}")
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
                    logger.info(f"  Загружена страница {page}: {len(products)} товаров")
                    
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
                logger.info(f"  Найдено вариаций для товара {product_id}: {len(variations)}")
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
                logger.warning(f"Категория не найдена в WordPress, товар попадет в Uncategorized")
                logger.warning(f"Проверьте что в WordPress есть категория: '{category_path}'")
                categories_data = []  # Пустой список = Uncategorized
            else:
                categories_data = [{'id': category_id}]  # Используем ID категории!
            
            # Формируем теги из ключевых слов (если есть)
            tags = []
            keywords = getattr(product, 'keywords', '')
            if keywords:
                tags = [{'name': kw.strip()} for kw in keywords.split(';')[:10] if kw.strip()]
            
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
            
            data = {
                'name': product_name,
                'type': 'variable',
                'sku': product.sku,
                'description': product.description,
                'short_description': getattr(product, 'short_description', ''),
                'categories': categories_data,  # Используем ID категорий!
                'tags': tags,
                'images': [{'src': img} for img in product.images[:5]],  # Первые 5 изображений
                'meta_data': meta_data,
                'attributes': [],
                'status': 'publish'
            }
            
            # Формируем атрибуты
            # 1. Бренд (не для вариаций)
            data['attributes'].append({
                'name': 'Brand',
                'visible': True,
                'variation': False,
                'options': [product.brand]
            })
            
            # 2. Цвет (ДЛЯ ВАРИАЦИЙ, СНАЧАЛА!)
            unique_colors = list(set([v['color'] for v in product.variations if 'color' in v]))
            
            logger.info(f"  [DEBUG] Собрано цветов из вариаций: {len(unique_colors)}")
            logger.info(f"  [DEBUG] Уникальные цвета: {unique_colors}")
            logger.info(f"  [DEBUG] Первая вариация: size={product.variations[0].get('size')}, color={product.variations[0].get('color', 'НЕТ')}")
            logger.info(f"  [DEBUG] Вторая вариация: size={product.variations[1].get('size') if len(product.variations) > 1 else 'N/A'}, color={product.variations[1].get('color', 'НЕТ') if len(product.variations) > 1 else 'N/A'}")
            
            if unique_colors:
                # Сортируем цвета для удобства
                unique_colors.sort()
                data['attributes'].append({
                    'name': 'Цвет',
                    'visible': True,
                    'variation': True,
                    'options': unique_colors
                })
                logger.info(f"  Цвета в атрибутах для WordPress: {unique_colors}")
            
            # 3. Размер (ДЛЯ ВАРИАЦИЙ, ВТОРЫМ!)
            unique_sizes = list(set([str(v['size']) for v in product.variations]))
            # Сортируем размеры в правильном порядке
            size_order = ['XS', 'S', 'M', 'L', 'XL', '2XL', '3XL', '4XL', '5XL']
            sorted_sizes = []
            for s in size_order:
                if s in unique_sizes:
                    sorted_sizes.append(s)
            # Добавляем числовые размеры (обувь)
            numeric_sizes = sorted([s for s in unique_sizes if s not in size_order], key=lambda x: float(x) if x.replace('.', '').isdigit() else 999)
            sorted_sizes.extend(numeric_sizes)
            
            data['attributes'].append({
                'name': 'Размер',
                'visible': True,
                'variation': True,
                'options': sorted_sizes if sorted_sizes else unique_sizes
            })
            
            # Добавляем ВСЕ дополнительные атрибуты (КРОМЕ Цвета!)
            for attr_name, attr_value in product.attributes.items():
                if attr_name not in ['Brand', 'Бренд', 'Размер', 'Size', 'Цвет', 'Color']:
                    data['attributes'].append({
                        'name': attr_name,
                        'visible': True,
                        'variation': False,
                        'options': [str(attr_value)]
                    })
            
            # Логируем данные для отладки
            logger.info(f"  [DEBUG] ======== АТРИБУТЫ ДЛЯ ОТПРАВКИ В WORDPRESS ========")
            for attr in data['attributes']:
                logger.info(f"  [DEBUG] Атрибут '{attr['name']}': variation={attr.get('variation', False)}, options={attr['options']}")
            logger.info(f"  [DEBUG] =================================================")
            
            logger.debug(f"Создание товара в WordPress:")
            logger.debug(f"  Название: {data['name'][:50]}")
            logger.debug(f"  SKU: {data['sku']}")
            logger.debug(f"  Категория: {data['categories']}")
            logger.debug(f"  Вариаций: {len(product.variations)}")
            
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
            self._create_variations(product_id, product, settings)
            
            return product_id
            
        except Exception as e:
            logger.error(f"[ERROR] Ошибка создания товара {product.sku}: {e}")
            return None
    
    def _create_variations(self, product_id: int, product: PoisonProduct, settings: SyncSettings):
        """Создает вариации для товара"""
        url = f"{self.url}/wp-json/wc/v3/products/{product_id}/variations"
        
        for idx, variation in enumerate(product.variations, 1):
            try:
                # Применяем курс и наценку к цене
                final_price = settings.apply_price_transformation(variation['price'])
                
                # Формируем атрибуты вариации (ВАЖНО: в том же порядке что и в основном товаре!)
                var_attributes = []
                
                # Сначала цвет (если есть)
                if 'color' in variation and variation['color']:
                    var_attributes.append({
                        'name': 'Цвет',
                        'option': str(variation['color'])
                    })
                
                # Потом размер
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
                
                # Добавляем изображение для вариации (если есть)
                if 'images' in variation and variation['images']:
                    var_data['image'] = {
                        'src': variation['images'][0]  # Первое изображение для этого цвета
                    }
                    logger.info(f"  [DEBUG] Добавлено изображение для вариации: {variation['images'][0][:50]}...")
                
                color_info = f", цвет={variation.get('color', 'нет')}" if 'color' in variation else ""
                logger.info(f"  Создание вариации {idx}/{len(product.variations)}: размер={variation['size']}{color_info}, SKU={variation['sku_id']}, цена={final_price}₽, остаток={variation['stock']}")
                
                # Retry logic для вариаций
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        response = requests.post(url, auth=self.auth, json=var_data, verify=False, timeout=60)
                        response.raise_for_status()
                        created_var = response.json()
                        created_sku = created_var.get('sku', 'NO_SKU')
                        color_log = f", цвет={variation.get('color', 'нет')}" if 'color' in variation else ""
                        logger.info(f"  [OK] Создана вариация размер {variation['size']}{color_log}, отправлен SKU={variation['sku_id']}, сохранен SKU={created_sku}, цена {final_price}₽")
                        break
                    except Exception as retry_error:
                        if attempt < max_retries - 1:
                            logger.warning(f"  Повтор создания вариации (попытка {attempt+2})")
                            time.sleep(1)
                        else:
                            raise retry_error
                
                # Пауза убрана для ускорения (большинство хостингов нормально обрабатывают запросы)
                # time.sleep(0.3)
                
            except Exception as e:
                logger.error(f"  [ERROR] Ошибка создания вариации: {e}")
    
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
                logger.info(f"  Примеры SKU из Poizon: {poizon_skus}")
            
            if existing_variations:
                wc_skus = [v.get('sku') for v in existing_variations[:3]]
                logger.info(f"  Примеры SKU из WooCommerce: {wc_skus}")
            
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
                        
                        logger.info(f"  Обновляем SKU {sku_id}: цена {final_price}₽, остаток {variation['stock']}, размер={variation.get('size', 'N/A')}")
                        
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

