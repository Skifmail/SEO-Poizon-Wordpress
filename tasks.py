"""
Celery tasks for the Poizon-WordPress integration.

This module contains the background tasks that are executed by Celery workers,
such as processing and uploading products.
"""
import os
import logging
from dataclasses import dataclass, asdict
from datetime import datetime
import re

# Import services and settings
from poizon_to_wordpress_service import WooCommerceService, SyncSettings
from poizon_api_fixed import PoisonAPIClientFixed as PoisonAPIService
from web_app import GigaChatService, init_services, poizon_client, woocommerce_client, gigachat_client

# Import the Celery app instance
from celery_app import celery

# Get the logger
logger = logging.getLogger(__name__)

@dataclass
class ProcessingStatus:
    """A serializable status for product processing."""
    product_id: str
    status: str  # e.g., 'PROGRESS', 'SUCCESS', 'FAILURE'
    progress: int
    message: str
    timestamp: str

class ProductProcessor:
    """
    Handles the processing of a single product through the Poizon -> GigaChat -> WordPress pipeline.
    This class is designed to be used within a Celery task.
    """
    def __init__(self, celery_task, settings: SyncSettings):
        """
        Initializes the processor.
        
        Args:
            celery_task: The Celery task instance (`self` from a bound task).
            settings: The synchronization settings.
        """
        self.celery_task = celery_task
        self.settings = settings
        # The service clients are initialized globally in web_app.py
        self.poizon = poizon_client
        self.gigachat = gigachat_client
        self.woocommerce = woocommerce_client

    def process_product(self, spu_id: int) -> dict:
        """
        Processes a single product and reports progress back to the Celery task.
        
        Args:
            spu_id: The product SPU ID from Poizon.
            
        Returns:
            A dictionary with the final status of the processing.
        """
        product_key = str(spu_id)
        
        try:
            # Step 1: Get data from Poizon
            self._update_status(product_key, 'PROGRESS', 10, 'Загрузка данных из Poizon API...')
            product = self.poizon.get_product_full_info(spu_id)
            if not product:
                raise ValueError('Не удалось загрузить информацию о товаре из Poizon API.')

            # Clean brand and article number
            original_brand = product.brand
            product.brand = self._extract_latin_only(product.brand) or "Brand"
            logger.info(f"Бренд из API: '{original_brand}' → '{product.brand}'")
            
            original_article = product.article_number
            product.article_number = self._extract_latin_only(product.article_number) or product.article_number
            logger.info(f"Артикул: '{original_article}' → '{product.article_number}'")

            # Step 2: Process through GigaChat
            self._update_status(product_key, 'PROGRESS', 40, 'Обработка через GigaChat...')
            seo_data = self.gigachat.translate_and_generate_seo(
                title=product.title,
                description=product.description,
                category=product.category,
                brand=product.brand,
                attributes=product.attributes,
                article_number=product.article_number
            )
            product.title = seo_data['title_ru']
            product.description = seo_data['full_description']
            product.short_description = seo_data.get('short_description', '')
            product.seo_title = seo_data.get('seo_title', seo_data['title_ru'])
            product.meta_description = seo_data.get('meta_description', '')
            product.keywords = seo_data.get('keywords', '')

            # Step 2.5: Translate variation colors
            if product.variations:
                self._translate_variation_colors(product.variations)

            # Step 3: Check and upload to WordPress
            self._update_status(product_key, 'PROGRESS', 70, 'Проверка товара в WordPress...')
            existing_id = self.woocommerce.product_exists(product.sku)
            
            if existing_id:
                self._update_status(product_key, 'PROGRESS', 75, f'Обновление товара ID {existing_id}...')
                updated_count = self.woocommerce.update_product_variations(existing_id, product, self.settings)
                message = f'Обновлен товар ID {existing_id} ({updated_count} вариаций)'
            else:
                self._update_status(product_key, 'PROGRESS', 75, 'Создание нового товара...')
                new_id = self.woocommerce.create_product(product, self.settings)
                if not new_id:
                    raise ValueError('Ошибка создания товара в WordPress.')
                message = f'Создан товар ID {new_id}'

            # Step 4: Done
            final_status = self._update_status(product_key, 'SUCCESS', 100, message)
            return asdict(final_status)

        except Exception as e:
            logger.error(f"Ошибка обработки товара {spu_id}: {e}", exc_info=True)
            final_status = self._update_status(product_key, 'FAILURE', 0, f'Ошибка: {str(e)}')
            return asdict(final_status)

    def _update_status(self, product_id: str, status: str, progress: int, message: str) -> ProcessingStatus:
        """Updates the Celery task state with the current progress."""
        status_obj = ProcessingStatus(
            product_id=product_id,
            status=status,
            progress=progress,
            message=message,
            timestamp=datetime.now().isoformat()
        )
        
        # Update Celery task state
        self.celery_task.update_state(
            state='PROGRESS',
            meta=asdict(status_obj)
        )
        return status_obj

    def _translate_variation_colors(self, variations: list):
        """Translate colors for a list of variations."""
        unique_colors = {v['color'] for v in variations if 'color' in v and v['color']}
        if not unique_colors:
            return
            
        logger.info(f"Переводим {len(unique_colors)} уникальных цветов...")
        color_translations = {color: self.gigachat.translate_color(color) for color in unique_colors}
        
        for variation in variations:
            if 'color' in variation and variation['color']:
                original_color = variation['color']
                variation['color'] = color_translations.get(original_color, original_color)

    def _extract_latin_only(self, text: str) -> str:
        """Extracts Latin letters, numbers, and basic punctuation."""
        if not text:
            return ""
        return "".join(re.findall(r"[a-zA-Z0-9\s\-\./]+", text)).strip()


@celery.task(bind=True)
def process_product_task(self, spu_id: int, settings_data: dict):
    """
    Celery background task to process a single product.
    
    Args:
        self: The task instance (automatically passed with `bind=True`).
        spu_id: The Poizon SPU ID of the product to process.
        settings_data: A dictionary with sync settings (currency_rate, markup_rubles).
    """
    # Ensure services are initialized in the worker process
    # This is a simple way to do it; a more robust solution might use Celery signals
    if not poizon_client or not woocommerce_client or not gigachat_client:
        logger.info("Один из клиентов не инициализирован. Выполняется init_services() в воркере...")
        init_services()

    logger.info(f"Запуск задачи для товара {spu_id} с настройками: {settings_data}")
    
    settings = SyncSettings(
        currency_rate=settings_data.get('currency_rate', 13.5),
        markup_rubles=settings_data.get('markup_rubles', 5000)
    )
    
    processor = ProductProcessor(celery_task=self, settings=settings)
    result = processor.process_product(spu_id)
    
    # The final result of the task will be the last status update
    return result
