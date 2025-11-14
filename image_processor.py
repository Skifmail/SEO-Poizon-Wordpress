"""
Модуль для обработки изображений товаров
"""
import io
import logging
import requests
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)


def resize_image_to_square(image_url: str, size: int = 600, bg_color: tuple = (255, 255, 255)) -> bytes:
    """
    Изменяет размер изображения до квадрата с сохранением пропорций.
    
    Args:
        image_url: URL изображения
        size: Размер квадрата (по умолчанию 600x600)
        bg_color: Цвет фона для заполнения (RGB, по умолчанию белый)
        
    Returns:
        bytes: Обработанное изображение в формате JPEG
        
    Raises:
        Exception: Если не удалось загрузить или обработать изображение
    """
    try:
        # Загружаем изображение
        response = requests.get(image_url, timeout=30, verify=False)
        response.raise_for_status()
        
        # Открываем изображение
        img = Image.open(io.BytesIO(response.content))
        
        # Конвертируем в RGB (если RGBA или другой формат)
        if img.mode != 'RGB':
            # Создаем белый фон для прозрачных изображений
            background = Image.new('RGB', img.size, bg_color)
            if img.mode == 'RGBA':
                background.paste(img, mask=img.split()[3])  # Используем альфа-канал
            else:
                background.paste(img)
            img = background
        
        # Вариант 1: RESIZE с сохранением пропорций + белый фон (рекомендуемый)
        # Уменьшаем изображение так, чтобы большая сторона стала = size
        img.thumbnail((size, size), Image.Resampling.LANCZOS)
        
        # Создаем квадратный холст с белым фоном
        new_img = Image.new('RGB', (size, size), bg_color)
        
        # Вставляем уменьшенное изображение по центру
        offset = ((size - img.width) // 2, (size - img.height) // 2)
        new_img.paste(img, offset)
        
        # Сохраняем в буфер
        output = io.BytesIO()
        new_img.save(output, format='JPEG', quality=95, optimize=True)
        output.seek(0)
        
        logger.info(f"Изображение обработано: {img.size} → {size}x{size}px")
        return output.getvalue()
        
    except Exception as e:
        logger.error(f"Ошибка обработки изображения {image_url}: {e}")
        raise


def resize_image_crop_center(image_url: str, size: int = 600) -> bytes:
    """
    Альтернативный вариант: обрезка изображения по центру до квадрата.
    ВНИМАНИЕ: может обрезать важные части товара!
    
    Args:
        image_url: URL изображения
        size: Размер квадрата (по умолчанию 600x600)
        
    Returns:
        bytes: Обработанное изображение в формате JPEG
    """
    try:
        response = requests.get(image_url, timeout=30, verify=False)
        response.raise_for_status()
        
        img = Image.open(io.BytesIO(response.content))
        
        if img.mode != 'RGB':
            img = img.convert('RGB')
        
        # Обрезаем по центру до квадрата
        img = ImageOps.fit(img, (size, size), Image.Resampling.LANCZOS, centering=(0.5, 0.5))
        
        output = io.BytesIO()
        img.save(output, format='JPEG', quality=95, optimize=True)
        output.seek(0)
        
        logger.info(f"Изображение обрезано: → {size}x{size}px (crop)")
        return output.getvalue()
        
    except Exception as e:
        logger.error(f"Ошибка обрезки изображения {image_url}: {e}")
        raise
