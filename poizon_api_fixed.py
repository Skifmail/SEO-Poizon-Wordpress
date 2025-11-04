"""
Клиент для работы с Poizon API (poizon-api.com).

Этот модуль предоставляет клиент для взаимодействия с Poizon API - 
платформой для получения данных о товарах с китайского маркетплейса DEWU/Poizon.

Основные функции:
    - Получение списка брендов
    - Получение категорий товаров
    - Поиск товаров по ключевым словам
    - Получение детальной информации о товаре (изображения, вариации, цены)
    
API Documentation: https://poizon-api.com/docs

Требования:
    - POIZON_API_KEY: API ключ от poizon-api.com
    - POIZON_CLIENT_ID: Client ID от poizon-api.com
    
Переменные окружения должны быть указаны в файле .env

"""
import os
import logging
import requests
from typing import Dict, List, Optional
from dotenv import load_dotenv
import urllib3

# Отключаем SSL предупреждения для работы с API
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

load_dotenv()

logger = logging.getLogger(__name__)


class PoisonAPIClientFixed:
    """
    Клиент для работы с Poizon API (исправленная версия).
    
    Предоставляет методы для получения информации о товарах, брендах, 
    категориях и ценах с платформы DEWU/Poizon через poizon-api.com.
    
    Attributes:
        api_key (str): API ключ для аутентификации
        client_id (str): Client ID для аутентификации
        base_url (str): Базовый URL API
        headers (dict): Заголовки HTTP для всех запросов
        
    Raises:
        ValueError: Если не указаны POIZON_API_KEY или POIZON_CLIENT_ID в .env
        
    Example:
        >>> client = PoisonAPIClientFixed()
        >>> products = client.search_products("Nike", limit=10)
        >>> for product in products:
        ...     print(product['title'])
    """
    
    def __init__(self):
        """Инициализация клиента"""
        self.api_key = os.getenv('POIZON_API_KEY')
        self.client_id = os.getenv('POIZON_CLIENT_ID')
        self.base_url = "https://poizon-api.com/api/dewu"
        
        if not self.api_key or not self.client_id:
            raise ValueError("POIZON_API_KEY и POIZON_CLIENT_ID должны быть в .env")
        
        self.headers = {
            'x-api-key': self.api_key,
            'client-id': self.client_id,
            'Content-Type': 'application/json'
        }
        
        logger.info("[OK] Инициализирован Poizon API клиент (исправленный)")
    
    def get_brands(self, limit: int = 100, page: int = 0) -> List[Dict]:
        """
        Получает список брендов.
        
        Args:
            limit: Максимальное количество брендов
            page: Номер страницы
            
        Returns:
            Список брендов
        """
        try:
            url = f"{self.base_url}/getBrands"
            data = {"limit": limit, "page": page}
            
            logger.debug(f"Запрос брендов: limit={limit}, page={page}")
            response = requests.post(url, json=data, headers=self.headers, timeout=60)
            response.raise_for_status()
            
            result = response.json()
            brands = result.get('data', [])
            
            logger.info(f"[OK] Загружено брендов: {len(brands)}")
            return brands
            
        except Exception as e:
            logger.error(f"[ERROR] Ошибка загрузки брендов: {e}")
            return []
    
    def get_categories(self, lang: str = "RU") -> List[Dict]:
        """
        Получает список категорий.
        
        Args:
            lang: Язык (RU, EN, CN)
            
        Returns:
            Список категорий
        """
        try:
            url = f"{self.base_url}/getCategories"
            params = {"lang": lang}
            
            logger.debug(f"Запрос категорий: lang={lang}")
            response = requests.get(url, params=params, headers=self.headers, timeout=60)
            response.raise_for_status()
            
            result = response.json()
            # API возвращает массив напрямую
            categories = result if isinstance(result, list) else result.get('categories', [])
            
            logger.info(f"[OK] Загружено категорий: {len(categories)}")
            return categories
            
        except Exception as e:
            logger.error(f"[ERROR] Ошибка загрузки категорий: {e}")
            return []
    
    def search_products(self, keyword: str, limit: int = 50, page: int = 0) -> List[Dict]:
        """
        Поиск товаров по ключевому слову.
        
        Args:
            keyword: Ключевое слово для поиска
            limit: Максимальное количество товаров
            page: Номер страницы
            
        Returns:
            Список товаров
        """
        try:
            url = f"{self.base_url}/searchProducts"
            params = {
                "keyword": keyword,
                "limit": min(limit, 100),
                "page": page
            }
            
            logger.debug(f"Поиск товаров: keyword={keyword}, limit={limit}")
            response = requests.get(url, params=params, headers=self.headers, timeout=60)
            response.raise_for_status()
            
            result = response.json()
            # API возвращает ключ productList
            products = result.get('productList') or result.get('list') or []
            
            # Дополнительная проверка
            if products is None:
                products = []
            
            logger.info(f"[OK] Найдено товаров: {len(products)}")
            return products
            
        except Exception as e:
            logger.error(f"[ERROR] Ошибка поиска товаров: {e}")
            return []
    
    def get_product_detail_v3(self, spu_id: int) -> Optional[Dict]:
        """
        Получает детальную информацию о товаре.
        
        Args:
            spu_id: ID товара
            
        Returns:
            Данные товара
        """
        try:
            url = f"{self.base_url}/productDetailV3"
            params = {"spuId": spu_id}
            
            response = requests.get(url, params=params, headers=self.headers, timeout=60)
            response.raise_for_status()
            
            return response.json()
            
        except Exception as e:
            logger.error(f"[ERROR] Ошибка получения товара {spu_id}: {e}")
            return None
    
    def get_price_info(self, spu_id: int) -> Dict:
        """
        Получает информацию о ценах товара.
        
        Args:
            spu_id: ID товара
            
        Returns:
            Словарь {skuId: {price, stock}}
        """
        try:
            url = f"{self.base_url}/priceInfo"
            params = {"spuId": spu_id}
            
            response = requests.get(url, params=params, headers=self.headers, timeout=60)
            response.raise_for_status()
            
            data = response.json()
            skus_dict = data.get('skus', {})
            
            # Парсим цены
            result = {}
            for sku_id, sku_info in skus_dict.items():
                prices_array = sku_info.get('prices', [])
                quantity = sku_info.get('quantity', 0)
                
                if prices_array and len(prices_array) > 0:
                    first_price = prices_array[0]
                    price = first_price.get('price')
                    
                    if price:
                        result[str(sku_id)] = {
                            'price': float(price),
                            'stock': int(quantity)
                        }
            
            return result
            
        except Exception as e:
            logger.error(f"[ERROR] Ошибка получения цен {spu_id}: {e}")
            return {}
    
    def get_product_full_info(self, spu_id: int):
        """
        Получает полную информацию о товаре для загрузки в WordPress.
        
        Этот метод объединяет данные из нескольких API endpoints:
        - productDetailV3: основная информация, изображения, атрибуты
        - priceInfo: актуальные цены и остатки по размерам
        
        Выполняет сложную обработку:
        1. Парсинг китайских атрибутов (размеры, цвета)
        2. Сопоставление изображений с цветами
        3. Формирование вариаций товара (размер + цвет + цена)
        4. Перевод атрибутов и категорий
        
        Args:
            spu_id: Уникальный идентификатор товара в системе Poizon
            
        Returns:
            SimpleNamespace объект с полными данными товара или None при ошибке
            
        Note:
            Результат совместим с классом PoisonProduct из poizon_to_wordpress_service
        """
        try:
            # === ШАГ 1: Получаем детали товара через productDetailV3 ===
            detail_data = self.get_product_detail_v3(spu_id)
            
            if not detail_data:
                return None
            
            # === ШАГ 2: Получаем актуальные цены и остатки через priceInfo ===
            prices = self.get_price_info(spu_id)
            logger.info(f"  [DEBUG] Получено цен из priceInfo: {len(prices)}")
            if prices:
                first_three = list(prices.items())[:3]
                logger.info(f"  [DEBUG] Первые 3 цены: {first_three}")
            
            # Парсим данные
            logger.info(f"  [DEBUG] ======== КЛЮЧИ ВЕРХНЕГО УРОВНЯ detail_data ========")
            logger.info(f"  [DEBUG] detail_data.keys(): {detail_data.keys()}")
            logger.info(f"  [DEBUG] =================================================")
            
            detail = detail_data.get('detail', {})
            skus_array = detail_data.get('skus', [])
            logger.info(f"  [DEBUG] Получено SKU из productDetailV3: {len(skus_array)}")
            
            # Проверяем структуру image
            image_root = detail_data.get('image', {})
            logger.info(f"  [DEBUG] image.keys(): {image_root.keys() if image_root else 'EMPTY'}")
            
            # Проверяем sortList - может там изображения по цветам?
            sort_list = image_root.get('sortList', [])
            logger.info(f"  [DEBUG] sortList: {len(sort_list)} элементов")
            if sort_list and len(sort_list) > 0:
                logger.info(f"  [DEBUG] Первый элемент sortList: {sort_list[0]}")
            
            image_data = image_root.get('spuImage', {})
            brand_data = detail_data.get('brand', {})
            sale_properties = detail_data.get('saleProperties', {}).get('list', [])
            
            # === ШАГ 3: Создаем маппинг размеров и цветов ===
            # Парсим китайские атрибуты из saleProperties
            # '尺码' (chǐmǎ) = размер, '颜色' (yánsè) = цвет
            size_value_map = {}  # {propertyValueId: размер}
            color_value_map = {}  # {propertyValueId: название цвета}
            
            for prop in sale_properties:
                prop_name = prop.get('name', '')
                size_value = prop.get('value', '')
                property_value_id = prop.get('propertyValueId')
                
                # Ищем размеры (尺码 = размер)
                if '尺码' in prop_name and size_value and property_value_id:
                    size_value_map[property_value_id] = size_value
                    
                # Ищем цвета (颜色 = цвет)
                if '颜色' in prop_name and size_value and property_value_id:
                    color_value_map[property_value_id] = size_value
            
            logger.info(f"  [DEBUG] size_value_map создан: {len(size_value_map)} размеров")
            logger.info(f"  [DEBUG] color_value_map создан: {len(color_value_map)} цветов")
            
            if size_value_map:
                first_three = dict(list(size_value_map.items())[:3])
                logger.info(f"  [DEBUG] Первые 3 размера: {first_three}")
            if color_value_map:
                first_three_colors = dict(list(color_value_map.items())[:3])
                logger.info(f"  [DEBUG] Первые 3 цвета: {first_three_colors}")
            
            # ТЕПЕРЬ извлекаем изображения
            images = []
            images_list = image_data.get('images', [])
            
            # DEBUG: логируем структуру изображений
            logger.info(f"  [DEBUG] image_data.keys(): {image_data.keys() if image_data else 'EMPTY'}")
            logger.info(f"  [DEBUG] Всего изображений в spuImage.images: {len(images_list)}")
            
            for img in images_list:
                img_url = img.get('url', '')
                if img_url:
                    images.append(img_url)
            
            # Извлекаем изображения по цветам из colorBlockImages
            color_images_map = {}  # propertyValueId → список изображений
            color_block_images = image_data.get('colorBlockImages', {})
            
            logger.info(f"  [DEBUG] colorBlockImages: {color_block_images}")
            logger.info(f"  [DEBUG] Тип colorBlockImages: {type(color_block_images)}")
            logger.info(f"  [DEBUG] Пустой? {not bool(color_block_images)}")
            
            if color_block_images and isinstance(color_block_images, dict) and len(color_block_images) > 0:
                logger.info(f"  [DEBUG] ✅ Найдено colorBlockImages! Ключи: {list(color_block_images.keys())}")
                
                for prop_id_str, img_list in color_block_images.items():
                    logger.info(f"  [DEBUG] Обработка colorBlockImages[{prop_id_str}]: тип={type(img_list)}, содержимое={img_list}")
                    prop_id = int(prop_id_str)
                    color_urls = []
                    
                    if isinstance(img_list, list):
                        for img_item in img_list:
                            if isinstance(img_item, dict):
                                img_url = img_item.get('url', '')
                                if img_url:
                                    color_urls.append(img_url)
                            elif isinstance(img_item, str):
                                color_urls.append(img_item)
                    
                    if color_urls:
                        color_images_map[prop_id] = color_urls
                        logger.info(f"  [DEBUG] Цвет propertyValueId={prop_id}: {len(color_urls)} изображений")
                    else:
                        logger.warning(f"  [DEBUG] Цвет propertyValueId={prop_id}: НЕТ изображений в списке")
            else:
                logger.info(f"  [DEBUG] ❌ colorBlockImages пустой или отсутствует")
                
                # Пробуем разбить общие изображения на группы по цветам
                # Если есть 4 цвета и 20 изображений, то по 5 изображений на цвет
                if images and len(color_value_map) > 0:
                    images_per_color = len(images) // len(color_value_map)
                    logger.info(f"  [DEBUG] Попытка разбить {len(images)} изображений на {len(color_value_map)} цветов = {images_per_color} изображений/цвет")
                    
                    color_ids = sorted(color_value_map.keys())
                    for idx, color_id in enumerate(color_ids):
                        start_idx = idx * images_per_color
                        end_idx = start_idx + images_per_color
                        color_specific_imgs = images[start_idx:end_idx]
                        
                        if color_specific_imgs:
                            color_images_map[color_id] = color_specific_imgs
                            logger.info(f"  [DEBUG] → Цвет {color_value_map[color_id]} (ID={color_id}): изображения [{start_idx}:{end_idx}] = {len(color_specific_imgs)} шт")
                
                if not color_images_map:
                    logger.info(f"  [DEBUG] Используем только общие изображения SPU для всех вариаций")
            
            # DEBUG: логируем полную структуру первых 3 SKU для изучения
            if skus_array and len(skus_array) > 0:
                logger.info(f"  [DEBUG] ======== ПОЛНАЯ СТРУКТУРА ПЕРВЫХ 3 SKU ========")
                for i in range(min(3, len(skus_array))):
                    sku = skus_array[i]
                    logger.info(f"  [DEBUG] SKU #{i+1} ПОЛНАЯ СТРУКТУРА:")
                    for key, value in sku.items():
                        if key not in ['properties']:  # properties логируем отдельно
                            logger.info(f"    {key}: {value}")
                    logger.info(f"    properties: {sku.get('properties', [])}")
                logger.info(f"  [DEBUG] =================================================")
            
            # Формируем вариации
            variations = []
            logger.info(f"  [DEBUG] Начинаем формировать вариации из {len(prices)} цен")
            for idx_price, (sku_id_str, price_data) in enumerate(prices.items()):
                logger.info(f"  [DEBUG] Вариация {idx_price+1}/{len(prices)}: SKU={sku_id_str}, цена={price_data.get('price')}¥, остаток={price_data.get('stock')}")
                # Ищем соответствующий SKU в skus_array для получения размера
                size = None
                
                # Находим SKU в массиве skus_array
                for idx, sku_item in enumerate(skus_array):
                    if str(sku_item.get('skuId')) == sku_id_str:
                        properties = sku_item.get('properties', [])
                        
                        logger.info(f"  [DEBUG] SKU {sku_id_str}: properties={properties}")
                        
                        # Извлекаем размер и цвет из properties
                        # properties может содержать [level 1 = цвет, level 2 = размер] или только размер
                        color = None
                        
                        for prop in properties:
                            property_value_id = prop.get('propertyValueId')
                            
                            # Проверяем в каком маппинге находится этот propertyValueId
                            if property_value_id in size_value_map:
                                size = size_value_map[property_value_id]
                                logger.info(f"  [DEBUG] → Размер найден: propertyValueId={property_value_id} → size={size}")
                            elif property_value_id in color_value_map:
                                color = color_value_map[property_value_id]
                                logger.info(f"  [DEBUG] → Цвет найден: propertyValueId={property_value_id} → color={color}")
                        
                        # Если размер не найден через properties, используем fallback
                        if not size:
                            logger.warning(f"  [DEBUG] Размер не найден через properties, используем fallback")
                            size_props = [p for p in sale_properties if '尺码' in p.get('name', '')]
                            if idx < len(size_props):
                                size = size_props[idx].get('value', '')
                                logger.info(f"  [DEBUG] → Размер из saleProperties[{idx}]: {size}")
                        
                        break
                
                # Если размер не найден, используем SKU ID
                if not size or size == 'None':
                    logger.warning(f"  SKU {sku_id_str}: размер не найден, используем SKU ID")
                    size = sku_id_str
                
                # Переводим цвет с китайского на русский
                color_translations = {
                    # === Базовые цвета ===
                    '黑': 'Черный', '黑色': 'Черный',
                    '白': 'Белый', '白色': 'Белый',
                    '灰': 'Серый', '灰色': 'Серый',
                    '红': 'Красный', '红色': 'Красный',
                    '蓝': 'Синий', '蓝色': 'Синий',
                    '绿': 'Зеленый', '绿色': 'Зеленый',
                    '黄': 'Желтый', '黄色': 'Желтый',
                    '橙': 'Оранжевый', '橙色': 'Оранжевый',
                    '粉': 'Розовый', '粉色': 'Розовый',
                    '紫': 'Фиолетовый', '紫色': 'Фиолетовый',
                    '棕': 'Коричневый', '棕色': 'Коричневый',
                    '咖啡色': 'Коричневый',
                    '褐色': 'Коричневый',
                    '米色': 'Бежевый',
                    '银色': 'Серебристый',
                    '金色': 'Золотой',
                    '青色': 'Бирюзовый',
                    '青绿': 'Бирюзовый',
                    '青蓝': 'Бирюзово-синий',
                    '湖蓝': 'Голубой',
                    '天蓝': 'Небесно-голубой',
                    '藏蓝': 'Темно-синий',
                    '深蓝': 'Темно-синий',
                    '浅蓝': 'Голубой',
                    '海军蓝': 'Темно-синий',
                    '宝蓝': 'Королевский синий',
                    '蓝灰': 'Сине-серый',
                    '墨绿': 'Темно-зеленый',
                    '军绿': 'Хаки',
                    '卡其': 'Хаки', '卡其色': 'Хаки',
                    '橄榄绿': 'Оливковый',
                    '草绿': 'Травяной зеленый',
                    '苹果绿': 'Яблочно-зеленый',
                    '嫩绿': 'Салатовый',
                    '薄荷绿': 'Мятный',
                    '枣红': 'Бордовый',
                    '酒红': 'Бордовый',
                    '深红': 'Темно-красный',
                    '浅红': 'Светло-красный',
                    '玫红': 'Малиновый',
                    '粉红': 'Розовый',
                    '浅粉': 'Светло-розовый',
                    '桃红': 'Персиковый',
                    '橘红': 'Оранжево-красный',
                    '柠檬黄': 'Желтый',
                    '姜黄': 'Горчичный',
                    '金黄': 'Золотистый',
                    '奶白': 'Молочный белый',
                    '象牙白': 'Слоновая кость',
                    '米白': 'Молочно-белый',
                    '烟灰': 'Дымчато-серый',
                    '银灰': 'Серебристо-серый',
                    '石墨灰': 'Графитовый',
                    '苍岩灰': 'Серый',
                    '探险棕': 'Коричневый',
                    '桦木': 'Бежевый',
                    '桦木绿': 'Зеленый',
                    '耀夜紫': 'Фиолетовый',
                    '骑士黑': 'Черный',
                    
                    # === Комбинации цветов (двухцветные и более) ===
                    '黑白': 'Черно-белый', '黑白色': 'Черно-белый',
                    '红白': 'Красно-белый', '红白色': 'Красно-белый',
                    '蓝白': 'Сине-белый', '蓝白色': 'Сине-белый',
                    '黑红': 'Черно-красный', '黑红色': 'Черно-красный',
                    '黑蓝': 'Черно-синий', '黑蓝色': 'Черно-синий',
                    '黑灰': 'Черно-серый', '黑灰色': 'Черно-серый',
                    '黑金': 'Черно-золотой', '黑金色': 'Черно-золотой',
                    '黑银': 'Черно-серебристый', '黑银色': 'Черно-серебристый',
                    '红黑': 'Красно-черный', '红黑色': 'Красно-черный',
                    '红蓝': 'Красно-синий', '红蓝色': 'Красно-синий',
                    '红黄': 'Красно-желтый', '红黄色': 'Красно-желтый',
                    '红绿': 'Красно-зеленый', '红绿色': 'Красно-зеленый',
                    '蓝黑': 'Сине-черный', '蓝黑色': 'Сине-черный',
                    '蓝灰': 'Сине-серый', '蓝灰色': 'Сине-серый',
                    '蓝绿': 'Сине-зеленый', '蓝绿色': 'Сине-зеленый',
                    '蓝金': 'Сине-золотой', '蓝金色': 'Сине-золотой',
                    '蓝银': 'Сине-серебристый', '蓝银色': 'Сине-серебристый',
                    '白金': 'Белый с золотом', '白金色': 'Белый с золотом',
                    '白银': 'Белый с серебром', '白银色': 'Белый с серебром',
                    '灰白': 'Серо-белый', '灰白色': 'Серо-белый',
                    '灰蓝': 'Серо-синий', '灰蓝色': 'Серо-синий',
                    '灰黑': 'Серо-черный', '灰黑色': 'Серо-черный',
                    '棕白': 'Коричнево-белый', '棕白色': 'Коричнево-белый',
                    '棕黑': 'Коричнево-черный', '棕黑色': 'Коричнево-черный',
                    '粉白': 'Розово-белый', '粉白色': 'Розово-белый',
                    '粉蓝': 'Розово-голубой', '粉蓝色': 'Розово-голубой',
                    '粉紫': 'Розово-фиолетовый', '粉紫色': 'Розово-фиолетовый',
                    '紫白': 'Фиолетово-белый', '紫白色': 'Фиолетово-белый',
                    '紫黑': 'Фиолетово-черный', '紫黑色': 'Фиолетово-черный',
                    '紫蓝': 'Фиолетово-синий', '紫蓝色': 'Фиолетово-синий',
                    '金黑': 'Золотисто-черный', '金黑色': 'Золотисто-черный',
                    '金白': 'Золотисто-белый', '金白色': 'Золотисто-белый',
                    '金银': 'Золото-серебристый', '金银色': 'Золото-серебристый',
                    '绿白': 'Зелено-белый', '绿白色': 'Зелено-белый',
                    '绿黑': 'Зелено-черный', '绿黑色': 'Зелено-черный',
                    '绿蓝': 'Зелено-синий', '绿蓝色': 'Зелено-синий',
                    '黄黑': 'Желто-черный', '黄黑色': 'Желто-черный',
                    '黄白': 'Желто-белый', '黄白色': 'Желто-белый',
                    '黄蓝': 'Желто-синий', '黄蓝色': 'Желто-синий',
                    '黄绿': 'Желто-зеленый', '黄绿色': 'Желто-зеленый',
                    '银黑': 'Серебристо-черный', '银黑色': 'Серебристо-черный',
                    '银白': 'Серебристо-белый', '银白色': 'Серебристо-белый',
                    '银蓝': 'Серебристо-синий', '银蓝色': 'Серебристо-синий',
                    '银灰': 'Серебристо-серый', '银灰色': 'Серебристо-серый',
                    '彩色': 'Разноцветный',
                    '多色': 'Многоцветный',
                    '撞色': 'Контрастный цвет',
                    '渐变色': 'Градиентный цвет'
                }
                
                color_ru = color_translations.get(color, color) if color else None
                if color and color_ru != color:
                    logger.info(f"  [DEBUG] → Цвет переведен: {color} → {color_ru}")
                
                # Находим propertyValueId цвета для извлечения изображений
                color_prop_id = None
                if color:
                    for prop in properties:
                        prop_id = prop.get('propertyValueId')
                        if prop_id in color_value_map:
                            color_prop_id = prop_id
                            break
                
                # Получаем изображения для этого цвета
                color_specific_images = []
                if color_prop_id and color_prop_id in color_images_map:
                    color_specific_images = color_images_map[color_prop_id]
                    logger.info(f"  [DEBUG] → Найдено {len(color_specific_images)} изображений для цвета {color}")
                
                logger.info(f"  [DEBUG] → Итог: SKU={sku_id_str}, размер={size}, цвет={color_ru or 'нет'}, цена={price_data.get('price')}¥, остаток={price_data.get('stock')}, изображений={len(color_specific_images)}")
                
                # Проверяем цену (должна быть адекватной)
                price_yuan = price_data['price']
                # Цены в Poizon API обычно указаны в фенях (1/100 юаня)
                if price_yuan > 10000:  # Если больше 10000, скорее всего это фени
                    price_yuan = price_yuan / 100
                
                variation_data = {
                    'sku_id': sku_id_str,
                    'size': str(size),  # Размер БЕЗ цвета
                    'price': price_yuan,
                    'stock': price_data['stock']
                }
                
                # Добавляем цвет отдельно (если есть)
                if color_ru:
                    variation_data['color'] = color_ru  # Переведенный цвет
                
                # Добавляем изображения для этого цвета (если есть)
                if color_specific_images:
                    variation_data['images'] = color_specific_images
                
                variations.append(variation_data)
            
            logger.info(f"  Создано вариаций: {len(variations)}")
            if variations:
                sizes = [v['size'] for v in variations[:5]]
                logger.info(f"  Примеры размеров: {sizes}")
            else:
                logger.warning(f"  ВАРИАЦИЙ НЕТ! prices={len(prices)}, skus_array={len(skus_array)}, sale_properties={len(sale_properties)}")
            
            # Формируем атрибуты (переводим китайские названия)
            from category_mapper import translate_attribute_name
            
            attributes = {}
            for prop in sale_properties:
                attr_name = prop.get('name', '')
                attr_value = prop.get('value', '')
                if attr_name and attr_value and '尺码' not in attr_name:  # Пропускаем размер (он уже в вариациях)
                    # Переводим название атрибута
                    translated_name = translate_attribute_name(attr_name)
                    attributes[translated_name] = attr_value
            
            # Добавляем атрибуты из baseProperties если есть
            base_properties = detail_data.get('baseProperties', {}).get('list', [])
            for prop in base_properties:
                attr_key = prop.get('key', '')
                attr_value = prop.get('value', '')
                if attr_key and attr_value:
                    translated_key = translate_attribute_name(attr_key)
                    if translated_key not in attributes:
                        attributes[translated_key] = attr_value
            
            # Извлекаем бренд (пробуем разные источники)
            brand_name = (
                brand_data.get('brandName') or
                detail.get('brandName') or
                detail.get('title', '').split()[0] if detail.get('title') else 'Unknown'
            )
            
            # Маппим категорию в WordPress категорию
            # Перезагружаем модуль category_mapper для актуальных изменений
            import importlib
            import category_mapper
            importlib.reload(category_mapper)
            from category_mapper import map_category_to_wordpress
            
            poizon_category = detail.get('categoryName', '')
            wordpress_category = map_category_to_wordpress(poizon_category, detail.get('title', ''))
            
            logger.info(f"Категория Poizon: '{poizon_category}'")
            logger.info(f"Категория WordPress: '{wordpress_category}'")
            
            # Создаем объект товара (простой dict вместо dataclass)
            from types import SimpleNamespace
            
            product = SimpleNamespace(
                spu_id=detail.get('spuId'),
                dewu_id=detail.get('spuId'),
                poizon_id=str(detail.get('spuId')),
                sku=str(detail.get('spuId')),
                title=detail.get('title', ''),
                article_number=detail.get('articleNumber', ''),
                brand=brand_name,
                category=poizon_category,
                wordpress_category=wordpress_category,
                images=images,
                variations=variations,
                attributes=attributes,
                description=detail.get('desc', '')
            )
            
            logger.info(f"[OK] Загружена полная информация о товаре {spu_id}")
            return product
            
        except Exception as e:
            logger.error(f"[ERROR] Ошибка загрузки полной информации {spu_id}: {e}")
            return None


# Тестирование
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    client = PoisonAPIClientFixed()
    
    print("\n=== Тест 1: Получение брендов ===")
    brands = client.get_brands(limit=10)
    if brands:
        print(f"Найдено брендов: {len(brands)}")
        for i, brand in enumerate(brands[:5], 1):
            print(f"  {i}. {brand.get('name', 'N/A')} (ID: {brand.get('id')})")
    
    print("\n=== Тест 2: Получение категорий ===")
    categories = client.get_categories()
    if categories:
        print(f"Найдено категорий: {len(categories)}")
        # Фильтруем только главные категории (level=1)
        main_cats = [c for c in categories if c.get('level') == 1][:10]
        for i, cat in enumerate(main_cats, 1):
            print(f"  {i}. {cat.get('name', 'N/A')} (ID: {cat.get('id')}, Level: {cat.get('level')})")
    
    print("\n=== Тест 3: Поиск товаров ===")
    products = client.search_products("Nike", limit=5)
    if products:
        print(f"Найдено товаров: {len(products)}")
        for i, product in enumerate(products, 1):
            print(f"  {i}. {product.get('title', 'N/A')} (spuId: {product.get('spuId', 'N/A')})")

