from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
import json
import os
import re
import traceback
from html import unescape

import requests
from PIL import Image
from playwright.sync_api import sync_playwright, Page

BASE_DIR = Path(__file__).resolve().parent
SESSION_FILE = BASE_DIR / "session.json"
SESSION_CONTENT_KEYS = ("SESSION_JSON_CONTENT", "session_json_content")
DOWNLOADS_DIR = BASE_DIR / "downloads"
OUTPUT_DIR = BASE_DIR / "output"
LOGS_DIR = BASE_DIR / "logs"
SCREENSHOTS_DIR = BASE_DIR / "screenshots"
PRODUCTS_URL_WITH_ML_ACTIVE_FILTER = "https://app.producteca.com/products?isArchived=false&salesChannel=2"
PICTURE_DELETE_BUTTON_SELECTOR = 'div[class*="delete-button__pictureUploader"], div[class*="_delete-button_"]'
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
    "Referer": "https://app.producteca.com/",
}
KINDERLAND_BASE_URL = "https://www.kinderland.com.ar"

for folder in [DOWNLOADS_DIR, OUTPUT_DIR, LOGS_DIR, SCREENSHOTS_DIR]:
    folder.mkdir(parents=True, exist_ok=True)

@dataclass
class ProductStatus:
    index: int
    sku: str
    href: str = ""
    status: str = "Pendiente"
    step: str = ""
    images_detected: int = 0
    last_event: str = ""
    error: str = ""
    screenshot: str = ""
    product_name: str = ""

LogCallback = Optional[Callable[[str], None]]
ProgressCallback = Optional[Callable[[list[dict]], None]]

class RunLogger:
    def __init__(self, callback: LogCallback = None) -> None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = LOGS_DIR / f"run_{timestamp}.log"
        self.callback = callback

    def write(self, message: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        print(line)
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if self.callback:
            self.callback(line)

def _notify(progress_callback: ProgressCallback, statuses: list[ProductStatus]) -> None:
    if progress_callback:
        progress_callback([asdict(s) for s in statuses])

def parse_skus(raw_text: str) -> list[str]:
    items = [line.strip() for line in raw_text.splitlines() if line.strip()]
    seen = set()
    result = []
    for sku in items:
        if sku not in seen:
            seen.add(sku)
            result.append(sku)
    return result

def _get_session_json_content() -> tuple[str, str]:
    for key in SESSION_CONTENT_KEYS:
        value = os.getenv(key, "").strip()
        if value:
            return value, key

    try:
        import streamlit as st

        for key in SESSION_CONTENT_KEYS:
            value = st.secrets.get(key, "")
            if isinstance(value, str) and value.strip():
                return value.strip(), f"st.secrets[{key}]"
    except Exception:
        pass

    return "", ""

def load_session_from_env_if_needed() -> tuple[bool, str]:
    if SESSION_FILE.exists():
        return True, f"session.json detectado en {SESSION_FILE}"

    env_value, source = _get_session_json_content()
    if not env_value:
        keys = ", ".join(SESSION_CONTENT_KEYS)
        return False, f"No existe session.json y no se encontró ninguna variable/secreto: {keys}"

    try:
        data = json.loads(env_value)
        SESSION_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return True, f"session.json creado desde {source}"
    except Exception as e:
        return False, f"No se pudo crear session.json desde {source}: {e}"

def clear_temp_files() -> None:
    for folder in [DOWNLOADS_DIR, OUTPUT_DIR]:
        for entry in folder.iterdir():
            try:
                if entry.is_file():
                    entry.unlink()
            except Exception:
                pass

def download_image(url: str, path: Path) -> None:
    response = requests.get(url, headers=REQUEST_HEADERS, timeout=30)
    response.raise_for_status()
    path.write_bytes(response.content)

def convert_to_1000(input_path: Path, output_path: Path) -> None:
    with Image.open(input_path) as img:
        img = img.convert("RGB")
        width, height = img.size
        scale = min(1000 / width, 1000 / height)
        new_w = int(width * scale)
        new_h = int(height * scale)
        resized = img.resize((new_w, new_h), Image.LANCZOS)
        canvas = Image.new("RGB", (1000, 1000), (255, 255, 255))
        x = (1000 - new_w) // 2
        y = (1000 - new_h) // 2
        canvas.paste(resized, (x, y))
        canvas.save(output_path, quality=95)

def normalize_ml_url(url: str | None) -> str | None:
    if not url or "mlstatic.com" not in url or url.endswith(".svg"):
        return None
    if not re.search(r"\.(jpg|jpeg|webp|png)(\?|$)", url, re.IGNORECASE):
        return None
    replacements = [("-N.", "-O."), ("-F.", "-O."), ("-I.", "-O."), ("-G.", "-O."), ("-R.", "-O."), ("-P.", "-O."), ("-T.", "-O."), ("-V.", "-O."), ("-X.", "-O."), ("_R.", "_O."), ("_N.", "_O."), ("_T.", "_O."), ("_P.", "_O.")]
    for old, new in replacements:
        url = url.replace(old, new)
    return url

def extract_ml_item_id(url: str) -> str | None:
    match = re.search(r"\b(MLA)-?(\d{5,})\b", url or "", re.IGNORECASE)
    if not match:
        return None
    return f"{match.group(1).upper()}{match.group(2)}"

def get_ml_image_urls_from_api(ml_url: str, logger: RunLogger) -> list[str]:
    item_id = extract_ml_item_id(ml_url)
    if not item_id:
        logger.write(f"ML API: no se pudo detectar item_id desde link: {ml_url}")
        return []

    logger.write(f"ML API: item_id detectado = {item_id}")
    api_url = f"https://api.mercadolibre.com/items/{item_id}"
    try:
        response = requests.get(api_url, headers=REQUEST_HEADERS, timeout=30)
        if response.status_code != 200:
            logger.write(f"ML API: error HTTP {response.status_code} consultando {api_url}")
            return []
        data = response.json()
    except Exception as e:
        logger.write(f"ML API: error consultando {api_url}: {e}")
        return []

    pictures = data.get("pictures") or []
    urls = []
    for picture in pictures:
        if not isinstance(picture, dict):
            continue
        url = picture.get("secure_url") or picture.get("url")
        if url:
            urls.append(url)

    logger.write(f"ML API: imágenes encontradas = {len(urls)}")
    return urls

def get_ml_image_urls_from_page(ml_url: str, logger: RunLogger) -> list[str]:
    logger.write("ML WEB: intentando extraer imágenes desde la publicación...")
    try:
        response = requests.get(ml_url, headers=REQUEST_HEADERS, timeout=30)
        if response.status_code != 200:
            logger.write(f"ML WEB: error HTTP {response.status_code} consultando publicación")
            return []
        html = unescape(response.text.replace("\\/", "/"))
    except Exception as e:
        logger.write(f"ML WEB: error consultando publicación: {e}")
        return []

    candidates = re.findall(
        r"https?://[^\"'<>\\\s]+mlstatic\.com[^\"'<>\\\s]+\.(?:jpg|jpeg|webp|png)(?:\?[^\"'<>\\\s]*)?",
        html,
        re.IGNORECASE,
    )

    urls = []
    seen = set()
    for candidate in candidates:
        if "/D_" not in candidate and "/D_NQ" not in candidate:
            continue
        normalized = normalize_ml_url(candidate)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        urls.append(normalized)

    logger.write(f"ML WEB: imágenes encontradas = {len(urls)}")
    return urls

def get_ml_image_urls_from_browser(page: Page, ml_url: str, logger: RunLogger) -> list[str]:
    logger.write("ML BROWSER: intentando extraer imágenes desde la publicación...")
    ml_page = None
    try:
        ml_page = page.context.new_page()
        ml_page.bring_to_front()
        ml_page.goto(ml_url, wait_until="domcontentloaded", timeout=45000)
        ml_page.wait_for_timeout(10000)

        candidates = []
        for attempt in range(31):
            try:
                candidates = ml_page.evaluate(
                    """
                    () => {
                      const readImage = (el) => {
                        if (el.tagName === 'META') return [el.content].filter(Boolean);
                        const values = [
                          el.currentSrc,
                          el.src,
                          el.getAttribute('src'),
                          el.getAttribute('data-src'),
                          el.getAttribute('data-zoom'),
                          el.getAttribute('data-srcset'),
                          el.getAttribute('srcset')
                        ].filter(Boolean);
                        return values.flatMap(value => String(value).split(',').map(part => part.trim().split(/\s+/)[0]));
                      };

                      const preferredSelectors = [
                        'figure img',
                        'img.ui-pdp-image',
                        '.ui-pdp-gallery img',
                        'meta[property="og:image"]'
                      ];
                      for (const selector of preferredSelectors) {
                        const values = Array.from(document.querySelectorAll(selector)).flatMap(readImage).filter(Boolean);
                        if (values.length > 0) return values;
                      }
                      return Array.from(document.querySelectorAll('img')).flatMap(readImage).filter(Boolean);
                    }
                    """
                )
            except Exception as e:
                logger.write(f"ML BROWSER: la página cambió mientras leía imágenes, reintento ({attempt + 1}/31): {e}")
                candidates = []
                try:
                    ml_page.wait_for_load_state(state="domcontentloaded", timeout=10000)
                except Exception:
                    pass
            if candidates:
                break
            if attempt == 0 and ("account-verification" in ml_page.url or "captcha" in ml_page.url):
                logger.write("ML BROWSER: Mercado Libre mostró verificación. Resolvela en la ventana abierta; espero hasta 90s.")
            if attempt < 30:
                ml_page.wait_for_timeout(3000)

        if not candidates:
            if "account-verification" in ml_page.url or "captcha" in ml_page.url:
                logger.write("ML BROWSER: no se resolvió la verificación de Mercado Libre.")
            else:
                logger.write("ML BROWSER: no se detectó galería de imágenes en la publicación.")
            return []

        urls = []
        seen = set()
        for candidate in candidates:
            normalized = normalize_ml_url(candidate)
            if not normalized or normalized in seen:
                continue
            if "/D_" not in normalized and "/D_NQ" not in normalized:
                continue
            seen.add(normalized)
            urls.append(normalized)

        if len(urls) == 1:
            more_candidates = ml_page.evaluate(
                """
                () => Array.from(document.querySelectorAll('img'))
                  .map(img => img.currentSrc || img.src || img.getAttribute('src') || img.getAttribute('data-src') || '')
                  .filter(Boolean)
                """
            )
            for candidate in more_candidates:
                normalized = normalize_ml_url(candidate)
                if not normalized or normalized in seen:
                    continue
                if "/D_" not in normalized and "/D_NQ" not in normalized:
                    continue
                seen.add(normalized)
                urls.append(normalized)
                if len(urls) >= 12:
                    break

        logger.write(f"ML BROWSER: imágenes encontradas = {len(urls)}")
        return urls
    except Exception as e:
        logger.write(f"ML BROWSER: error extrayendo imágenes: {e}")
        return []
    finally:
        try:
            if ml_page is not None:
                ml_page.close()
        except Exception:
            pass

def get_ml_image_urls(ml_url: str, logger: RunLogger, page: Page | None = None) -> list[str]:
    urls = get_ml_image_urls_from_api(ml_url, logger)
    if urls:
        return urls
    urls = get_ml_image_urls_from_page(ml_url, logger)
    if urls:
        return urls
    if page is not None:
        return get_ml_image_urls_from_browser(page, ml_url, logger)
    return []

def normalize_kinderland_image_url(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith("http"):
        return None
    if not re.search(r"\.(jpg|jpeg|webp|png)(\?|$)", url, re.IGNORECASE):
        return None
    return url

def get_kinderland_product_json_by_handle(handle: str, logger: RunLogger) -> dict | None:
    url = f"{KINDERLAND_BASE_URL}/products/{handle}.js"
    try:
        response = requests.get(url, headers=REQUEST_HEADERS, timeout=30)
        if response.status_code != 200:
            logger.write(f"KINDERLAND: {handle}.js devolvió HTTP {response.status_code}")
            return None
        data = response.json()
        if not isinstance(data, dict) or not data.get("id"):
            return None
        return data
    except Exception as e:
        logger.write(f"KINDERLAND: error consultando {url}: {e}")
        return None

def find_kinderland_handles_by_search(sku: str, logger: RunLogger) -> list[str]:
    try:
        response = requests.get(
            f"{KINDERLAND_BASE_URL}/search",
            headers=REQUEST_HEADERS,
            params={"q": sku, "type": "product"},
            timeout=30,
        )
        if response.status_code != 200:
            logger.write(f"KINDERLAND: búsqueda HTTP {response.status_code} para SKU {sku}")
            return []
        handles = []
        seen = set()
        for match in re.finditer(r"/products/([^\"'?/#]+)", response.text):
            handle = match.group(1)
            if handle in seen:
                continue
            seen.add(handle)
            handles.append(handle)
        return handles
    except Exception as e:
        logger.write(f"KINDERLAND: error buscando SKU {sku}: {e}")
        return []

def get_kinderland_product(sku: str, logger: RunLogger) -> dict | None:
    logger.write(f"KINDERLAND: buscando producto para SKU {sku}...")
    products = []

    direct = get_kinderland_product_json_by_handle(sku, logger)
    if direct:
        products.append(direct)

    if not products:
        for handle in find_kinderland_handles_by_search(sku, logger):
            product = get_kinderland_product_json_by_handle(handle, logger)
            if product:
                products.append(product)

    selected = None
    for product in products:
        variants = product.get("variants") or []
        if any(str(variant.get("sku", "")).strip() == sku or str(variant.get("barcode", "")).strip() == sku for variant in variants):
            selected = product
            break
    if selected is None and products:
        selected = products[0]

    if not selected:
        logger.write(f"KINDERLAND: no se encontró producto para SKU {sku}.")
        return None

    logger.write(f"KINDERLAND: producto encontrado = {selected.get('title', '')}")
    return selected

def get_kinderland_image_urls_from_product(product: dict, logger: RunLogger) -> list[str]:
    raw_images = product.get("images") or []
    urls = []
    seen = set()
    for image in raw_images:
        if isinstance(image, dict):
            url = image.get("src")
        else:
            url = image
        normalized = normalize_kinderland_image_url(url)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        urls.append(normalized)

    logger.write(f"KINDERLAND: imágenes encontradas = {len(urls)}")
    return urls

def get_kinderland_image_urls(sku: str, logger: RunLogger) -> list[str]:
    logger.write(f"KINDERLAND: buscando imágenes para SKU {sku}...")
    product = get_kinderland_product(sku, logger)
    if not product:
        return []
    return get_kinderland_image_urls_from_product(product, logger)

def html_to_text(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", value)
    text = re.sub(r"(?i)</\s*p\s*>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()

def get_kinderland_description(product: dict | None) -> str:
    if not product:
        return ""
    return html_to_text(str(product.get("description") or product.get("body_html") or ""))

def get_notes_textarea(page: Page):
    return page.locator("textarea").first

def ensure_notes_if_empty(page: Page, description: str, logger: RunLogger) -> bool:
    logger.write("NOTAS: chequeando...")
    if not description:
        logger.write("NOTAS: Kinderland no tiene descripción para copiar.")
        return False

    notes = get_notes_textarea(page)
    try:
        if notes.count() == 0:
            logger.write("NOTAS: no se encontró campo de notas.")
            return False
        current = notes.input_value(timeout=5000).strip()
        if current:
            logger.write("NOTAS: ya tiene contenido, no se modifica.")
            return False

        logger.write("NOTAS: vacío, copiando descripción de Kinderland.")
        notes.scroll_into_view_if_needed(timeout=5000)
        page.wait_for_timeout(500)
        notes.fill(description)
        page.wait_for_timeout(500)
        return True
    except Exception as e:
        logger.write(f"NOTAS: no se pudo completar: {e}")
        return False

def get_picture_section(page: Page):
    return page.locator('section[class*="pictures__productPictures"]').first

def scroll_to_pictures(page: Page, logger: RunLogger) -> None:
    technical_section = get_picture_section(page)
    try:
        technical_section.scroll_into_view_if_needed(timeout=5000)
        page.wait_for_timeout(900)
        return
    except Exception:
        pass

    logger.write("FOTOS: no apareció el selector técnico; buscando sección por título...")
    for _ in range(8):
        try:
            title = page.locator("h2, h3, span, div").filter(has_text=re.compile(r"^Fotos$")).first
            if title.count() > 0:
                title.scroll_into_view_if_needed(timeout=5000)
                page.wait_for_timeout(900)
                return
        except Exception:
            pass
        page.mouse.wheel(0, 900)
        page.wait_for_timeout(500)

    try:
        page.get_by_text("Fotos", exact=True).scroll_into_view_if_needed(timeout=5000)
        page.wait_for_timeout(900)
        return
    except Exception:
        pass

    raise RuntimeError("No se encontró la sección Fotos en Producteca.")

def count_current_pictures(page: Page) -> int:
    return page.locator(PICTURE_DELETE_BUTTON_SELECTOR).count()

def remove_variants_if_any(page: Page, logger: RunLogger) -> bool:
    logger.write("PRODUCTO: chequeando variantes...")
    removed_any = False
    for _ in range(30):
        try:
            buttons = page.locator("a.react-tagsinput-remove")
            total = buttons.count()
            if total == 0:
                break
            removed_any = True
            buttons.nth(total - 1).click(force=True)
            page.wait_for_timeout(1000)
        except Exception:
            break
    logger.write("PRODUCTO: variantes eliminadas." if removed_any else "PRODUCTO: no hay variantes.")
    return removed_any

def dimensions_are_zero(page: Page, logger: RunLogger) -> bool:
    try:
        text = page.locator("body").inner_text(timeout=5000).lower()
        pattern = r"(\d+)\s*cm\s*x\s*(\d+)\s*cm\s*x\s*(\d+)\s*cm\s*-\s*(\d+)\s*gr"
        matches = re.findall(pattern, text)
        if not matches:
            logger.write("DIMENSIONES: no pude leer patrón exacto, asumo que no está en cero.")
            return False
        return any([int(a), int(b), int(c), int(w)] == [0, 0, 0, 0] for a, b, c, w in matches)
    except Exception:
        return False

def complete_dimensions_if_zero(page: Page, logger: RunLogger) -> bool:
    logger.write("DIMENSIONES: chequeando...")
    if not dimensions_are_zero(page, logger):
        logger.write("DIMENSIONES: ya tiene medidas.")
        return True

    for attempt in range(2):
        try:
            logger.write(f"DIMENSIONES: están en cero. Editando intento {attempt + 1}/2...")
            edit_button = page.locator('a:has-text("Editar")').filter(has_text="Dimensiones").first
            edit_button.scroll_into_view_if_needed()
            page.wait_for_timeout(800)
            edit_button.click(force=True)
            page.wait_for_timeout(1800)

            modal = page.locator('div[role="dialog"]').last
            inputs = modal.locator("input")
            total_inputs = inputs.count()
            logger.write(f"DIMENSIONES: inputs detectados = {total_inputs}")
            if total_inputs < 4:
                logger.write("DIMENSIONES: no encontré los 4 campos.")
                continue

            for i, value in enumerate(["21", "35", "28", "1000"]):
                field = inputs.nth(i)
                field.click(force=True)
                page.wait_for_timeout(200)
                field.press("Control+A")
                page.wait_for_timeout(100)
                field.press("Backspace")
                page.wait_for_timeout(100)
                field.fill(value)
                page.wait_for_timeout(250)
                field.press("Tab")
                page.wait_for_timeout(250)

            logger.write("DIMENSIONES: valores cargados.")
            page.wait_for_timeout(1000)
            save_modal = page.locator('div[role="dialog"] button:has-text("Guardar cambios")').last
            save_modal.scroll_into_view_if_needed()
            page.wait_for_timeout(300)
            logger.write("DIMENSIONES: guardando modal...")
            save_modal.click(force=True)
            page.wait_for_timeout(3500)

            logger.write("DIMENSIONES: guardando producto después del modal...")
            save_changes(page, logger, "dimensiones")
            page.wait_for_timeout(1500)

            if not dimensions_are_zero(page, logger):
                logger.write("DIMENSIONES: verificadas y guardadas correctamente.")
                return True
            logger.write("DIMENSIONES: siguen en cero después de guardar.")
        except Exception as e:
            logger.write(f"DIMENSIONES: error en intento {attempt + 1}: {e}")

    logger.write("ERROR: no se pudieron guardar dimensiones.")
    return False

def remove_current_pictures(page: Page, logger: RunLogger) -> None:
    logger.write("FOTOS: borrando fotos actuales...")
    scroll_to_pictures(page, logger)
    total_removed = 0
    for _ in range(20):
        try:
            buttons = page.locator(PICTURE_DELETE_BUTTON_SELECTOR)
            total = buttons.count()
            if total == 0:
                break
            clicked = False
            for _ in range(total):
                try:
                    current = page.locator(PICTURE_DELETE_BUTTON_SELECTOR)
                    if current.count() == 0:
                        break
                    current.nth(0).click(force=True)
                    page.wait_for_timeout(800)
                    total_removed += 1
                    clicked = True
                except Exception:
                    break
            if not clicked:
                break
        except Exception:
            break
    logger.write(f"FOTOS: borradas = {total_removed}")

def upload_pictures_one_by_one(page: Page, paths: list[Path], logger: RunLogger) -> None:
    logger.write("FOTOS: subiendo una por una...")
    scroll_to_pictures(page, logger)
    input_file = page.locator('input[type="file"]').first
    for i, path in enumerate(paths, start=1):
        before = count_current_pictures(page)
        logger.write(f"FOTOS: subiendo {i}/{len(paths)}")
        input_file.set_input_files(str(path))
        uploaded = False
        for _ in range(20):
            page.wait_for_timeout(700)
            after = count_current_pictures(page)
            if after > before:
                uploaded = True
                break
        if not uploaded:
            page.wait_for_timeout(1200)

def click_save_button(page: Page, logger: RunLogger) -> None:
    selectors = [
        'button:has-text("Guardar")',
        'button:has-text("Guardar cambios")',
        'button:has-text("Guardar y cerrar")',
        'button[type="submit"]',
    ]
    last_error = ""
    for selector in selectors:
        button = page.locator(selector).last
        try:
            if button.count() == 0:
                continue
            button.wait_for(state="visible", timeout=5000)
            button.scroll_into_view_if_needed(timeout=5000)
            page.wait_for_timeout(300)
            button.click(force=True, timeout=8000)
            return
        except Exception as e:
            last_error = str(e)

    clicked = page.evaluate(
        """
        () => {
          const words = ['guardar cambios', 'guardar y cerrar', 'guardar'];
          const buttons = Array.from(document.querySelectorAll('button, [role="button"], input[type="submit"]'));
          const button = buttons.reverse().find((el) => {
            const text = `${el.innerText || ''} ${el.value || ''}`.trim().toLowerCase();
            return words.some((word) => text.includes(word)) && !el.disabled;
          });
          if (!button) return false;
          button.scrollIntoView({ block: 'center', inline: 'center' });
          button.click();
          return true;
        }
        """
    )
    if not clicked:
        raise RuntimeError(f"No se encontró botón Guardar visible. Último error: {last_error}")

def save_changes(page: Page, logger: RunLogger, reason: str = "cambios") -> None:
    logger.write(f"PRODUCTO: guardando {reason}...")
    click_save_button(page, logger)
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        page.wait_for_timeout(3500)
    logger.write(f"PRODUCTO: guardado OK ({reason}).")

def get_filtered_product_links(page: Page) -> list[str]:
    page.wait_for_timeout(5000)
    links = page.locator('a[href^="/products/"]')
    total = links.count()
    hrefs = []
    seen = set()
    for i in range(total):
        try:
            href = links.nth(i).get_attribute("href")
            if href and href not in seen:
                seen.add(href)
                hrefs.append(href)
        except Exception:
            pass
    return hrefs

def product_has_mercadolibre_link(page: Page) -> bool:
    return get_mercadolibre_link(page, timeout=7000) is not None

def get_mercadolibre_link(page: Page, timeout: int = 7000) -> str | None:
    link = page.locator('a[href*="mercadolibre"]').first
    try:
        link.wait_for(timeout=timeout)
        return link.get_attribute("href", timeout=1000)
    except Exception:
        try:
            return page.evaluate(
                """
                () => {
                  const link = Array.from(document.querySelectorAll('a'))
                    .find(anchor => (anchor.href || '').includes('mercadolibre'));
                  return link ? link.href : null;
                }
                """
            )
        except Exception:
            return None

def get_mercadolibre_integration_text(page: Page) -> str:
    try:
        return page.evaluate(
            """
            () => {
              const title = Array.from(document.querySelectorAll('*'))
                .find(el => (el.innerText || '').trim() === 'Mercadolibre');
              if (!title) return '';

              let current = title;
              for (let depth = 0; current && depth < 6; depth += 1, current = current.parentElement) {
                const text = current.innerText || '';
                if (text.includes('Listado General') || text.includes('Catálogo') || text.includes('Configuración')) {
                  return text;
                }
              }
              return title.parentElement ? title.parentElement.innerText || '' : '';
            }
            """
        )
    except Exception:
        return ""

def mercadolibre_publication_is_active(page: Page) -> bool:
    integration_text = get_mercadolibre_integration_text(page)
    if not integration_text:
        return False
    active_match = re.search(r"\bActiva:\s*(\d+)", integration_text, re.IGNORECASE)
    return bool(active_match and int(active_match.group(1)) > 0)

def find_mercadolibre_product_href(page: Page, sku: str, hrefs: list[str], logger: RunLogger) -> str:
    logger.write(f'LISTA: validando producto correcto para SKU {sku} entre {len(hrefs)} candidato(s)...')
    for idx, href in enumerate(hrefs, start=1):
        logger.write(f"LISTA: revisando candidato {idx}/{len(hrefs)} para SKU {sku}: {href}")
        goto_product(page, href, logger)
        if not product_has_mercadolibre_link(page):
            logger.write(f"LISTA: candidato descartado para SKU {sku}, sin link de Mercado Libre: {href}")
            continue
        if mercadolibre_publication_is_active(page):
            logger.write(f"LISTA: SKU {sku} asociado a Mercado Libre activo en {href}")
            return href
        logger.write(f"LISTA: candidato descartado para SKU {sku}, Mercado Libre no figura activo: {href}")
    raise RuntimeError(f"No se encontró un producto con Mercado Libre activo para el SKU {sku}. Candidatos revisados: {len(hrefs)}")

def download_and_prepare(urls: list[str], logger: RunLogger) -> list[Path]:
    logger.write("FOTOS: descargando y convirtiendo imágenes...")
    output_paths = []
    for i, url in enumerate(urls, start=1):
        try:
            download_path = DOWNLOADS_DIR / f"img_{i:02d}.jpg"
            output_path = OUTPUT_DIR / f"img_{i:02d}.jpg"
            download_image(url, download_path)
            convert_to_1000(download_path, output_path)
            output_paths.append(output_path.resolve())
        except Exception as e:
            logger.write(f"FOTOS: error procesando imagen {i}: {e}")
    logger.write(f"FOTOS: listas para subir = {len(output_paths)}")
    return output_paths

def save_failure_screenshot(page: Page, sku: str, index: int) -> str:
    safe_sku = re.sub(r"[^A-Za-z0-9_\-]", "_", sku) or f"item_{index}"
    filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{index:03d}_{safe_sku}.png"
    path = SCREENSHOTS_DIR / filename
    try:
        page.screenshot(path=str(path), full_page=True)
        return str(path)
    except Exception:
        return ""

def get_product_name(page: Page) -> str:
    candidates = [page.locator("h1").first, page.locator('[class*="title"]').first]
    for candidate in candidates:
        try:
            if candidate.count() > 0:
                value = candidate.inner_text().strip()
                if value:
                    return value
        except Exception:
            pass
    return ""

def get_product_search_input(page: Page, timeout: int = 45000):
    selectors = [
        "input.search-input",
        'input[placeholder*="Buscar"]',
        'input[type="search"]',
    ]
    last_error = ""
    for selector in selectors:
        search = page.locator(selector).first
        try:
            search.wait_for(state="visible", timeout=timeout)
            return search
        except Exception as e:
            last_error = str(e)
    raise RuntimeError(
        "No se encontró el buscador de productos en Producteca. "
        f"URL actual: {page.url}. Último error: {last_error}"
    )

def reapply_filter(page: Page, filter_text: str, logger: RunLogger) -> None:
    logger.write("LISTA: aplicando filtro Mercado Libre activo...")
    try:
        page.goto(PRODUCTS_URL_WITH_ML_ACTIVE_FILTER, wait_until="commit", timeout=30000)
    except Exception as e:
        logger.write(f"LISTA: navegación a productos no confirmó carga completa, sigo esperando buscador: {e}")
    search = get_product_search_input(page)
    search.fill("")
    page.wait_for_timeout(500)
    search.fill(filter_text)
    search.press("Enter")
    page.wait_for_timeout(6000)

def goto_product(page: Page, href: str, logger: RunLogger) -> None:
    url = f"https://app.producteca.com{href}"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        logger.write(f"PRODUCTO: navegación no confirmó domcontentloaded, sigo si la página responde: {e}")
        if page.is_closed():
            raise
    page.wait_for_timeout(2500)
    try:
        page.locator('a[href*="mercadolibre"], input, button').first.wait_for(state="attached", timeout=10000)
    except Exception as e:
        logger.write(f"PRODUCTO: la vista tardó en exponer controles después de navegar: {e}")

def try_reapply_filter(page: Page, filter_text: str, logger: RunLogger) -> None:
    try:
        reapply_filter(page, filter_text, logger)
    except Exception as e:
        logger.write(f"LISTA: no se pudo reaplicar filtro durante recuperación: {e}")

def close_page_if_open(page: Page | None) -> None:
    if page is None:
        return
    try:
        if not page.is_closed():
            page.close()
    except Exception:
        pass

def ensure_open_page(context, page: Page | None, logger: RunLogger) -> Page:
    try:
        if page is not None and not page.is_closed():
            return page
    except Exception:
        pass
    logger.write("SISTEMA: la página de Playwright se cerró; abriendo una nueva...")
    page = context.new_page()
    page.set_default_timeout(15000)
    page.set_default_navigation_timeout(45000)
    return page

def new_clean_page(context, previous_page: Page | None, logger: RunLogger) -> Page:
    close_page_if_open(previous_page)
    logger.write("SISTEMA: abriendo pestaña limpia para el SKU...")
    page = context.new_page()
    page.set_default_timeout(15000)
    page.set_default_navigation_timeout(45000)
    return page

def process_photos(page: Page, item: ProductStatus, logger: RunLogger) -> tuple[bool, str]:
    logger.write("=" * 60)
    logger.write(f"FOTOS: producto {item.index} | SKU {item.sku}")
    try:
        item.product_name = get_product_name(page)
        item.step = "Obteniendo imágenes de Kinderland"
        item.last_event = "Obteniendo imágenes de Kinderland"
        product = get_kinderland_product(item.sku, logger)
        urls = get_kinderland_image_urls_from_product(product, logger) if product else []
        description = get_kinderland_description(product)

        if not urls:
            item.status = "Error"
            item.error = "No se encontraron imágenes en Kinderland."
            logger.write(f"ERROR: {item.error}")
            return False, ""

        item.images_detected = len(urls)
        item.step = "Preparando imágenes"
        clear_temp_files()
        output_paths = download_and_prepare(urls, logger)
        if not output_paths:
            item.status = "Error"
            item.error = "No quedó ninguna imagen lista para subir."
            logger.write(f"ERROR: {item.error}")
            return False, ""

        page.bring_to_front()
        page.wait_for_timeout(1500)
        had_variants = remove_variants_if_any(page, logger)
        if had_variants:
            save_changes(page, logger, "variantes")
            page.wait_for_timeout(2500)

        if not complete_dimensions_if_zero(page, logger):
            item.status = "Error"
            item.error = "No se pudieron completar dimensiones."
            return False, ""

        notes_changed = ensure_notes_if_empty(page, description, logger)
        remove_current_pictures(page, logger)
        upload_pictures_one_by_one(page, output_paths, logger)
        save_changes(page, logger, "fotos y notas" if notes_changed else "fotos")
        logger.write("FOTOS: OK")
        return True, ""

    except Exception as e:
        item.status = "Error"
        item.error = str(e)
        logger.write(f"ERROR: fallo en fotos del producto {item.index}: {e}")
        logger.write(traceback.format_exc())
        item.screenshot = save_failure_screenshot(page, item.sku, item.index)
        return False, ""

def run_job(skus: list[str], log_callback: LogCallback = None, progress_callback: ProgressCallback = None, headless: Optional[bool] = None) -> dict:
    logger = RunLogger(log_callback)
    statuses = [ProductStatus(index=i + 1, sku=sku) for i, sku in enumerate(skus)]
    summary = {"ok": 0, "error": 0, "total": len(skus), "log_file": str(logger.log_file), "results": []}

    if not skus:
        logger.write("No hay SKUs para procesar.")
        return summary

    ok_session, session_message = load_session_from_env_if_needed()
    logger.write(session_message)
    if not ok_session:
        return summary

    filter_text = "|".join(skus)
    logger.write(f"SISTEMA: SKUs cargados = {len(skus)}")
    _notify(progress_callback, statuses)

    if headless is None:
        headless = os.getenv("HEADLESS", "false").lower() == "true"

    with sync_playwright() as p:
        logger.write(f"SISTEMA: abriendo navegador... headless={headless}")
        browser = p.chromium.launch(headless=headless)
        context = None
        try:
            context = browser.new_context(storage_state=str(SESSION_FILE))
            page = None
            logger.write("LISTA: se resolverá producto Mercado Libre SKU por SKU.")

            for i, item in enumerate(statuses, start=1):
                page = new_clean_page(context, page, logger)
                item.status = "En proceso"
                item.step = "Buscando producto Mercado Libre"
                item.last_event = f"Resolviendo producto {i}/{len(statuses)}"
                _notify(progress_callback, statuses)
                try:
                    logger.write("-" * 60)
                    logger.write(f"LISTA: resolviendo SKU {item.sku} ({i}/{len(statuses)})")
                    reapply_filter(page, item.sku, logger)
                    hrefs = get_filtered_product_links(page)
                    logger.write(f"LISTA: hrefs encontrados para SKU {item.sku} = {len(hrefs)}")
                    if not hrefs:
                        item.status = "No encontrado"
                        item.step = "Listado"
                        item.last_event = "No apareció en el listado filtrado"
                        summary["error"] += 1
                        _notify(progress_callback, statuses)
                        close_page_if_open(page)
                        page = None
                        continue

                    href = find_mercadolibre_product_href(page, item.sku, hrefs, logger)
                    item.href = href
                    item.step = "Abriendo producto"
                    item.last_event = f"Abriendo producto Mercado Libre {i}/{len(statuses)}"
                    _notify(progress_callback, statuses)
                    goto_product(page, href, logger)

                    photos_ok, _ = process_photos(page, item, logger)
                    if not photos_ok:
                        summary["error"] += 1
                        _notify(progress_callback, statuses)
                        close_page_if_open(page)
                        page = None
                        continue

                    item.status = "OK"
                    item.step = "Finalizado"
                    item.last_event = "Proceso completo"
                    summary["ok"] += 1
                    _notify(progress_callback, statuses)
                    close_page_if_open(page)
                    page = None

                except Exception as e:
                    item.status = "Error"
                    item.error = str(e)
                    item.screenshot = save_failure_screenshot(page, item.sku, item.index)
                    summary["error"] += 1
                    logger.write(f"ERROR: fallo general del producto {i}: {e}")
                    logger.write(traceback.format_exc())
                    _notify(progress_callback, statuses)
                    close_page_if_open(page)
                    page = None

            logger.write("=" * 60)
            logger.write(f"SISTEMA: finalizado. OK = {summary['ok']} | ERROR = {summary['error']}")
            summary["results"] = [asdict(s) for s in statuses]
            return summary

        finally:
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass
            browser.close()
