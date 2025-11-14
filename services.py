"""
This module initializes shared services to prevent circular imports.
"""
import os
import logging
import requests
import uuid

from poizon_to_wordpress_service import WooCommerceService
from poizon_api_fixed import PoisonAPIClientFixed as PoisonAPIService

logger = logging.getLogger(__name__)

# Global service clients, initialized once
poizon_client = None
woocommerce_client = None
gigachat_client = None

class GigaChatService:
    """Client for GigaChat API."""
    
    def __init__(self):
        self.auth_key = os.getenv('GIGACHAT_AUTH_KEY')
        self.client_id = os.getenv('GIGACHAT_CLIENT_ID')
        self.base_url = 'https://gigachat.devices.sberbank.ru/api/v1'
        self.access_token = None
        
        if not self.auth_key or not self.client_id:
            logger.warning("GIGACHAT_AUTH_KEY or GIGACHAT_CLIENT_ID not found in .env. GigaChat is disabled.")
            self.enabled = False
        else:
            self.enabled = True
            self._get_access_token()

    def _get_access_token(self):
        if not self.enabled:
            return
        
        url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
        rq_uid = str(uuid.uuid4())
        
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "RqUID": rq_uid,
            "Authorization": f"Basic {self.auth_key}",
        }
        
        data = {"scope": "GIGACHAT_API_PERS"}
        
        try:
            # In some environments, client_id needs to be in headers, in others in the body.
            # This is a workaround for GigaChat's inconsistent API behavior.
            if self.client_id:
                 headers["X-Client-ID"] = str(self.client_id)

            response = requests.post(url, headers=headers, data=data, verify=False, timeout=30)
            response.raise_for_status()
            self.access_token = response.json()["access_token"]
        except Exception as e:
            logger.error(f"Error getting GigaChat token: {e}")
            if hasattr(e, 'response') and e.response:
                logger.error(f"Server response: {e.response.text}")
            self.enabled = False

    def translate_color(self, color_chinese: str) -> str:
        if not self.enabled or not color_chinese or not any('\u4e00' <= char <= '\u9fff' for char in color_chinese):
            return color_chinese
        
        try:
            # Simplified prompt for color translation
            prompt = f"Translate the following color from Chinese to Russian. Respond with only the translated color name. Chinese: '{color_chinese}'"
            
            return self._make_chat_completion(prompt, temperature=0.2, max_tokens=50)
        except Exception as e:
            logger.warning(f"Error translating color '{color_chinese}': {e}, using original.")
            return color_chinese

    def translate_and_generate_seo(self, title: str, description: str, category: str, brand: str, attributes: dict = None, article_number: str = '') -> dict:
        if not self.enabled:
            logger.warning("GigaChat is not configured, using basic processing.")
            return self._get_basic_seo(title, brand, category, description)

        try:
            # Using a more structured and robust prompt
            prompt = self._build_seo_prompt(title, description, category, brand, attributes, article_number)
            
            response_text = self._make_chat_completion(prompt, temperature=0.7, max_tokens=1500)
            
            return self._parse_seo_response(response_text, title, brand, category, description)
            
        except Exception as e:
            logger.error(f"Error in GigaChat SEO generation: {e}")
            return self._get_basic_seo(title, brand, category, description)

    def _make_chat_completion(self, content: str, temperature: float, max_tokens: int) -> str:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "GigaChat",
            "messages": [{"role": "user", "content": content}],
            "temperature": temperature,
            "max_tokens": max_tokens
        }
        
        response = requests.post(url, headers=headers, json=payload, verify=False, timeout=120)
        
        if response.status_code == 401:
            logger.warning("GigaChat access token expired, refreshing...")
            self._get_access_token()
            headers["Authorization"] = f"Bearer {self.access_token}"
            response = requests.post(url, headers=headers, json=payload, verify=False, timeout=120)
            
        response.raise_for_status()
        return response.json()['choices'][0]['message']['content'].strip()

    def _get_basic_seo(self, title, brand, category, description):
        return {
            "title_ru": title,
            "seo_title": f"{brand} {title[:50]}",
            "short_description": f"Качественный товар {brand} из категории {category}",
            "full_description": f"Описание товара {title}. {description[:200] if description else 'Подробное описание будет добавлено позже.'}",
            "meta_description": f"{brand} - {title[:80]}"
        }

    def _build_seo_prompt(self, title, description, category, brand, attributes, article_number):
        # This prompt can be refined over time.
        # Keeping it simple to avoid complex parsing logic.
        return f"""
        Generate SEO content for a product.
        - Brand: {brand}
        - Original Title: {title}
        - Category: {category}
        - Article: {article_number}
        - Attributes: {attributes}

        Translate Chinese to English in the title. Create a Russian SEO Title, short description, and a full description (at least 800 characters).
        Respond in the following format, with each field on a new line:
        1. Russian Title
        2. SEO Title
        3. Short Description
        4. Full Description
        5. Meta Description
        6. Keywords (semicolon-separated)
        """

    def _parse_seo_response(self, response_text, fallback_title, brand, category, description):
        lines = response_text.split('\n')
        # Clean up lines from numbering like "1. "
        cleaned_lines = [re.sub(r'^\d+\.\s*', '', line).strip() for line in lines if line.strip()]

        if len(cleaned_lines) < 6:
            logger.warning("GigaChat returned an incomplete response. Using fallback.")
            return self._get_basic_seo(fallback_title, brand, category, description)

        return {
            "title_ru": cleaned_lines[0],
            "seo_title": cleaned_lines[1],
            "short_description": cleaned_lines[2],
            "full_description": cleaned_lines[3],
            "meta_description": cleaned_lines[4],
            "keywords": cleaned_lines[5]
        }


def init_services():
    """Initializes all shared services and clients."""
    global poizon_client, woocommerce_client, gigachat_client
    
    # This function will be called once per process (Gunicorn worker, Celery worker)
    if poizon_client is None:
        logger.info("Initializing PoizonAPIService...")
        poizon_client = PoisonAPIService()
    
    if woocommerce_client is None:
        logger.info("Initializing WooCommerceService...")
        woocommerce_client = WooCommerceService()
        
    if gigachat_client is None:
        logger.info("Initializing GigaChatService...")
        gigachat_client = GigaChatService()
    
    logger.info("All services initialized.")
