from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
import time
import traceback
import unicodedata
from html import unescape

import requests
from PIL import Image
from playwright.sync_api import sync_playwright, Page

BASE_DIR = Path(__file__).resolve().parent
SESSION_FILE = BASE_DIR / "session.json"
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
    name_filter: str = ""
    href: str = ""
    status: str = "Pendiente"
    step: str = ""
    images_detected: int = 0
    last_event: str = ""
    error: str = ""
    screenshot: str = ""
    product_name: str = ""
    elapsed_seconds: float = 0.0

LogCallback = Optional[Callable[[str], None]]
ProgressCallback = Optional[Callable[[list[dict]], None]]

class RunLogger:
    def __init__(self, callback: LogCallback = None) -> None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = LOGS_DIR / f"run_{timestamp}.log"
        self.single_image_skus_file = LOGS_DIR / f"skus_una_imagen_{timestamp}.txt"
        self.callback = callback

    def write(self, message: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        try:
            print(line)
        except BrokenPipeError:
            pass
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if self.callback:
            self.callback(line)

    def save_single_image_sku(self, sku: str) -> None:
        with self.single_image_skus_file.open("a", encoding="utf-8") as f:
            f.write(sku + "\n")

def _notify(progress_callback: ProgressCallback, statuses: list[ProductStatus]) -> None:
    if progress_callback:
        progress_callback([asdict(s) for s in statuses])

def parse_skus(raw_text: str) -> list[str]:
    items = [line.strip() for line in raw_text.splitlines() if line.strip()]
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result

def parse_sku_input_line(raw_value: str) -> tuple[str, str]:
    value = (raw_value or "").strip()
    if "|" not in value:
        return value, ""
    sku, name_filter = value.split("|", 1)
    return sku.strip(), name_filter.strip()

def load_session_from_env_if_needed() -> tuple[bool, str]:
    if SESSION_FILE.exists():
        return True, f"session.json detectado en {SESSION_FILE}"

    env_value = os.getenv("SESSION_JSON_CONTENT", "").strip()
    if not env_value:
        return False, "No existe session.json y no se encontró SESSION_JSON_CONTENT"

    try:
        data = json.loads(env_value)
        SESSION_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return True, "session.json creado desde SESSION_JSON_CONTENT"
    except Exception as e:
        return False, f"No se pudo crear session.json desde variable de entorno: {e}"

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

def get_image_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as img:
        return img.size

def assert_image_is_1000(output_path: Path) -> None:
    width, height = get_image_size(output_path)
    if (width, height) != (1000, 1000):
        raise RuntimeError(f"{output_path.name} quedó en {width}x{height}, esperado 1000x1000")

def get_producteca_access_token() -> str:
    try:
        state = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(f"No se pudo leer session.json para obtener accessToken: {e}") from e
    for cookie in state.get("cookies", []):
        if cookie.get("name") == "accessToken" and cookie.get("value"):
            return cookie["value"]
    raise RuntimeError("No se encontró accessToken en session.json.")

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

def normalize_match_tokens(value: str) -> set[str]:
    ascii_value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
    normalized = re.sub(r"[^a-z0-9]+", " ", ascii_value.lower())
    stopwords = {"de", "del", "la", "el", "y", "con", "para", "the", "and"}
    return {token for token in normalized.split() if len(token) >= 3 and token not in stopwords}

def product_titles_are_compatible(producteca_name: str, kinderland_title: str) -> bool:
    producteca_tokens = normalize_match_tokens(producteca_name)
    kinderland_tokens = normalize_match_tokens(kinderland_title)
    if not producteca_tokens or not kinderland_tokens:
        return False
    overlap = producteca_tokens & kinderland_tokens
    return len(overlap) >= 2 or bool(overlap and any(token.isdigit() for token in overlap))

def ensure_product_identity_matches(item: ProductStatus, kinderland_product: dict | None, logger: RunLogger) -> bool:
    kinderland_title = (kinderland_product or {}).get("title", "")
    if product_titles_are_compatible(item.product_name, kinderland_title):
        return True
    item.status = "Error"
    item.error = (
        "El producto encontrado en Kinderland no coincide con Producteca. "
        f"Producteca='{item.product_name}' | Kinderland='{kinderland_title}'"
    )
    logger.write(f"ERROR: {item.error}")
    return False

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

def mark_picture_upload_context(page: Page, logger: RunLogger) -> None:
    marked = page.evaluate(
        f"""
        () => {{
          const deleteSelector = {json.dumps(PICTURE_DELETE_BUTTON_SELECTOR)};
          const clearMarks = () => {{
            document.querySelectorAll('[data-codex-picture-upload], [data-codex-picture-section]').forEach((el) => {{
              el.removeAttribute('data-codex-picture-upload');
              el.removeAttribute('data-codex-picture-section');
            }});
          }};
          const isUsable = (el) => {{
            const style = window.getComputedStyle(el);
            return style.display !== 'none' && style.visibility !== 'hidden' && !el.disabled;
          }};
          const textHasFotos = (el) => /\\bFotos\\b/i.test(el.innerText || '');
          const inputs = Array.from(document.querySelectorAll('input[type="file"]'));
          let best = null;

          for (const input of inputs) {{
            let current = input;
            for (let depth = 0; current && depth < 9; depth += 1, current = current.parentElement) {{
              let score = 0;
              if (textHasFotos(current)) score += 100;
              if (current.querySelector(deleteSelector)) score += 80;
              if ((current.className || '').toString().toLowerCase().includes('picture')) score += 40;
              if (isUsable(input)) score += 10;
              const rect = current.getBoundingClientRect();
              if (rect.width > 0 && rect.height > 0) score += 5;
              if (!best || score > best.score) best = {{ input, section: current, score }};
            }}
          }}

          if (!best || best.score < 80) return {{ ok: false, inputs: inputs.length, score: best ? best.score : 0 }};
          clearMarks();
          best.input.setAttribute('data-codex-picture-upload', 'true');
          best.section.setAttribute('data-codex-picture-section', 'true');
          return {{ ok: true, inputs: inputs.length, score: best.score }};
        }}
        """
    )
    if not marked.get("ok"):
        raise RuntimeError(
            f"No se pudo identificar el uploader de Fotos. Inputs detectados: {marked.get('inputs')}, score: {marked.get('score')}"
        )
    logger.write(f"FOTOS: uploader de sección Fotos identificado (inputs={marked.get('inputs')}, score={marked.get('score')}).")

def get_picture_upload_input(page: Page):
    return page.locator('input[type="file"][data-codex-picture-upload="true"]').first

def get_marked_picture_section(page: Page):
    return page.locator('[data-codex-picture-section="true"]').first

def count_current_pictures(page: Page) -> int:
    section = get_marked_picture_section(page)
    try:
        if section.count() > 0:
            return section.locator(PICTURE_DELETE_BUTTON_SELECTOR).count()
    except Exception:
        pass
    return page.locator(PICTURE_DELETE_BUTTON_SELECTOR).count()

def wait_for_picture_count(page: Page, expected: int, logger: RunLogger, timeout_ms: int = 60000) -> None:
    deadline = datetime.now().timestamp() + (timeout_ms / 1000)
    last_count = -1
    while datetime.now().timestamp() < deadline:
        count = count_current_pictures(page)
        if count != last_count:
            logger.write(f"FOTOS: miniaturas visibles = {count}/{expected}")
            last_count = count
        if count >= expected:
            return
        page.wait_for_timeout(1000)
    raise RuntimeError(f"Las fotos no llegaron a {expected}. Miniaturas visibles: {count_current_pictures(page)}")

def wait_for_picture_uploads_to_settle(page: Page, expected: int, logger: RunLogger, timeout_ms: int = 90000) -> None:
    logger.write("FOTOS: esperando que Producteca termine de procesar las subidas...")
    wait_for_picture_count(page, expected, logger, timeout_ms=timeout_ms)
    stable_hits = 0
    deadline = datetime.now().timestamp() + (timeout_ms / 1000)
    while datetime.now().timestamp() < deadline:
        count = count_current_pictures(page)
        busy = page.evaluate(
            """
            () => {
              const text = document.body.innerText || '';
              if (/subiendo|cargando|procesando|uploading|loading/i.test(text)) return true;
              const busySelectors = [
                '[aria-busy="true"]',
                '[role="progressbar"]',
                '[class*="spinner"]',
                '[class*="loading"]',
                '[class*="progress"]'
              ];
              return busySelectors.some((selector) => document.querySelector(selector));
            }
            """
        )
        if count >= expected and not busy:
            stable_hits += 1
            if stable_hits >= 3:
                logger.write("FOTOS: subidas estables antes de guardar.")
                return
        else:
            stable_hits = 0
        page.wait_for_timeout(1200)
    raise RuntimeError("Producteca no estabilizó las subidas de fotos antes de guardar.")

def verify_picture_count_after_save(page: Page, expected: int, logger: RunLogger) -> None:
    logger.write("FOTOS: verificando persistencia después de guardar...")
    page.reload(wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(3500)
    scroll_to_pictures(page, logger)
    mark_picture_upload_context(page, logger)
    wait_for_picture_count(page, expected, logger, timeout_ms=45000)
    persisted = get_picture_sources(page)
    mlstatic_count = sum(1 for src in persisted if "mlstatic.com" in src)
    logger.write(
        f"FOTOS: persistencia verificada, miniaturas visibles = {count_current_pictures(page)}, "
        f"mlstatic = {mlstatic_count}/{len(persisted)}."
    )
    if mlstatic_count:
        raise RuntimeError(
            "Producteca volvió a mostrar fotos de Mercado Libre después de guardar; "
            "las fotos subidas no quedaron persistidas como archivos nuevos."
        )

def get_picture_sources(page: Page) -> list[str]:
    return page.evaluate(
        """
        () => {
          const section = document.querySelector('[data-codex-picture-section="true"]') || document.body;
          return Array.from(section.querySelectorAll('img'))
            .map((img) => img.currentSrc || img.src || '')
            .filter(Boolean);
        }
        """
    )

def get_product_id_from_href(href: str) -> str:
    match = re.search(r"/products/(\d+)", href or "")
    if not match:
        raise RuntimeError(f"No se pudo obtener product_id desde href: {href}")
    return match.group(1)

def get_producteca_product(page: Page, href: str) -> dict:
    product_id = get_product_id_from_href(href)
    token = get_producteca_access_token()
    response = page.context.request.get(
        f"https://apps.producteca.com/api/products/{product_id}?",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60000,
    )
    if not response.ok:
        raise RuntimeError(f"No se pudo leer producto {product_id}: HTTP {response.status} {response.text()[:500]}")
    return response.json()

def product_name_matches_filter(product_name: str, name_filter: str) -> bool:
    if not name_filter:
        return True
    product_text = " ".join(normalize_match_tokens(product_name))
    filter_tokens = normalize_match_tokens(name_filter)
    return bool(filter_tokens and filter_tokens.issubset(set(product_text.split())))

def product_name_match_score(product_name: str, name_filter: str) -> int:
    product_tokens = normalize_match_tokens(product_name)
    filter_tokens = normalize_match_tokens(name_filter)
    if not filter_tokens:
        return 0
    return len(product_tokens & filter_tokens)

def find_product_href_by_name_filter(page: Page, sku: str, name_filter: str, hrefs: list[str], logger: RunLogger) -> str:
    if not name_filter:
        return find_mercadolibre_product_href(page, sku, hrefs, logger)

    logger.write(
        f'LISTA: buscando candidato para SKU {sku} cuyo nombre coincida con "{name_filter}" '
        f"entre {len(hrefs)} candidato(s)..."
    )
    matches = []
    best_score = 0
    for href in hrefs:
        product = get_producteca_product(page, href)
        product_name = str(product.get("name") or "")
        score = product_name_match_score(product_name, name_filter)
        logger.write(f'LISTA: candidato {href} | score={score} | nombre="{product_name}"')
        if score > best_score:
            best_score = score
            matches = [(href, product_name)]
        elif score == best_score and score > 0:
            matches.append((href, product_name))

    expected_tokens = normalize_match_tokens(name_filter)
    if best_score < len(expected_tokens):
        raise RuntimeError(f'No se encontró un producto cuyo nombre contenga "{name_filter}" para el SKU {sku}.')
    if len(matches) > 1:
        names = " | ".join(name for _, name in matches[:5])
        raise RuntimeError(f'Coincidencia ambigua para SKU {sku} y nombre "{name_filter}": {names}')

    href, product_name = matches[0]
    logger.write(f'LISTA: candidato elegido por nombre para SKU {sku}: {href} | "{product_name}"')
    return href

def upload_picture_via_api(page: Page, path: Path, logger: RunLogger) -> str:
    assert_image_is_1000(path)
    token = get_producteca_access_token()
    response = page.context.request.post(
        "https://app.producteca.com/api/picture",
        headers={"Authorization": f"Bearer {token}", "Origin": "https://app.producteca.com"},
        multipart={"file": {"name": path.name, "mimeType": "image/jpeg", "buffer": path.read_bytes()}},
        timeout=60000,
    )
    if not response.ok:
        raise RuntimeError(f"No se pudo subir {path.name} por API: HTTP {response.status} {response.text()[:500]}")
    data = response.json()
    secure_url = data.get("secure_url")
    if not secure_url:
        raise RuntimeError(f"La API de fotos no devolvió secure_url para {path.name}: {data}")
    logger.write(f"FOTOS API: {path.name} subida a {secure_url}")
    return secure_url

def replace_product_pictures_via_api(page: Page, href: str, paths: list[Path], logger: RunLogger) -> None:
    product_id = get_product_id_from_href(href)
    token = get_producteca_access_token()
    logger.write(f"FOTOS API: subiendo {len(paths)} imagen(es) 1000x1000 y reemplazando fotos del producto {product_id}...")
    urls = [upload_picture_via_api(page, path, logger) for path in paths]

    auth_headers = {"Authorization": f"Bearer {token}"}
    product_url = f"https://apps.producteca.com/api/products/{product_id}?"
    get_response = page.context.request.get(product_url, headers=auth_headers, timeout=60000)
    if not get_response.ok:
        raise RuntimeError(f"No se pudo leer producto {product_id}: HTTP {get_response.status} {get_response.text()[:500]}")
    product = get_response.json()

    variations = product.get("variations") or []
    if not variations:
        raise RuntimeError(f"Producto {product_id} no tiene variaciones donde guardar fotos.")
    pictures_payload = [
        {
            "variation": variation["id"],
            "pictures": [{"url": url} for url in urls],
        }
        for variation in variations
        if variation.get("id")
    ]
    put_response = page.context.request.put(
        f"https://apps.producteca.com/api/products/{product_id}/pictures",
        headers={**auth_headers, "Content-Type": "application/json"},
        data=json.dumps(pictures_payload),
        timeout=60000,
    )
    if not put_response.ok:
        raise RuntimeError(f"No se pudieron guardar fotos del producto {product_id}: HTTP {put_response.status} {put_response.text()[:500]}")
    try:
        saved = put_response.json()
    except Exception:
        verify_response = page.context.request.get(product_url, headers=auth_headers, timeout=60000)
        if not verify_response.ok:
            raise RuntimeError(
                f"Fotos guardadas pero no se pudo verificar producto {product_id}: "
                f"HTTP {verify_response.status} {verify_response.text()[:500]}"
            )
        saved = verify_response.json()
    if isinstance(saved, list):
        saved_pictures = (saved or [{}])[0].get("pictures") or []
    else:
        saved_pictures = (saved.get("variations") or [{}])[0].get("pictures") or []
    mlstatic_count = sum(1 for picture in saved_pictures if "mlstatic.com" in str(picture.get("url", "")))
    if mlstatic_count:
        raise RuntimeError(f"Producteca respondió con {mlstatic_count} foto(s) mlstatic después del guardado API.")
    logger.write(f"FOTOS API: guardadas {len(saved_pictures)} foto(s) nuevas en Producteca.")

def get_producteca_picture_urls(page: Page, href: str, logger: RunLogger) -> list[str]:
    product_id = get_product_id_from_href(href)
    token = get_producteca_access_token()
    response = page.context.request.get(
        f"https://apps.producteca.com/api/products/{product_id}?",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60000,
    )
    if not response.ok:
        raise RuntimeError(f"No se pudo leer producto {product_id}: HTTP {response.status} {response.text()[:500]}")
    product = response.json()
    urls = []
    seen = set()
    for variation in product.get("variations") or []:
        for picture in variation.get("pictures") or []:
            url = picture.get("url")
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
    logger.write(f"PRODUCTECA: fotos actuales encontradas = {len(urls)}")
    return urls

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

DEFAULT_DIMENSIONS = {"weight": 1000, "width": 21, "length": 28, "height": 35, "pieces": 1}

def complete_dimensions_if_zero(page: Page, href: str, logger: RunLogger) -> bool:
    """Completa y verifica dimensiones directamente en Producteca.

    La pantalla puede mostrar valores nuevos antes de que el producto haya sido
    persistido. Leer nuevamente la API evita dar por exitoso un guardado fallido.
    """
    product_id = get_product_id_from_href(href)
    product_url = f"https://apps.producteca.com/api/products/{product_id}?"
    headers = {"Authorization": f"Bearer {get_producteca_access_token()}"}
    logger.write("DIMENSIONES API: chequeando valores persistidos...")

    try:
        response = page.context.request.get(product_url, headers=headers, timeout=60000)
        if not response.ok:
            raise RuntimeError(f"HTTP {response.status} {response.text()[:500]}")
        product = response.json()
        current = product.get("dimensions") or {}
        measured = [current.get(key) or 0 for key in ("width", "height", "length", "weight")]
        if any(float(value) > 0 for value in measured):
            logger.write(f"DIMENSIONES API: ya tiene medidas: {current}")
            return True

        logger.write(f"DIMENSIONES API: guardando valores predeterminados {DEFAULT_DIMENSIONS}...")
        product["dimensions"] = DEFAULT_DIMENSIONS.copy()
        # El endpoint de edición espera el documento sin el identificador de ruta.
        product.pop("id", None)
        saved_response = page.context.request.put(
            product_url,
            headers={**headers, "Content-Type": "application/json"},
            data=json.dumps(product),
            timeout=60000,
        )
        if not saved_response.ok:
            raise RuntimeError(f"HTTP {saved_response.status} {saved_response.text()[:500]}")

        verify_response = page.context.request.get(product_url, headers=headers, timeout=60000)
        if not verify_response.ok:
            raise RuntimeError(f"falló verificación HTTP {verify_response.status} {verify_response.text()[:500]}")
        persisted = verify_response.json().get("dimensions") or {}
        if any(persisted.get(key) != value for key, value in DEFAULT_DIMENSIONS.items()):
            raise RuntimeError(f"Producteca devolvió dimensiones distintas: {persisted}")

        logger.write(f"DIMENSIONES API: verificadas y guardadas correctamente: {persisted}")
        return True
    except Exception as e:
        logger.write(f"ERROR: no se pudieron guardar dimensiones por API: {e}")
        return False

def remove_current_pictures(page: Page, logger: RunLogger) -> None:
    logger.write("FOTOS: borrando fotos actuales...")
    scroll_to_pictures(page, logger)
    mark_picture_upload_context(page, logger)
    total_removed = 0
    for _ in range(20):
        try:
            section = get_marked_picture_section(page)
            buttons = section.locator(PICTURE_DELETE_BUTTON_SELECTOR) if section.count() > 0 else page.locator(PICTURE_DELETE_BUTTON_SELECTOR)
            total = buttons.count()
            if total == 0:
                break
            clicked = False
            for _ in range(total):
                try:
                    section = get_marked_picture_section(page)
                    current = section.locator(PICTURE_DELETE_BUTTON_SELECTOR) if section.count() > 0 else page.locator(PICTURE_DELETE_BUTTON_SELECTOR)
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
    mark_picture_upload_context(page, logger)
    for i, path in enumerate(paths, start=1):
        mark_picture_upload_context(page, logger)
        input_file = get_picture_upload_input(page)
        before = count_current_pictures(page)
        assert_image_is_1000(path)
        width, height = get_image_size(path)
        size_kb = path.stat().st_size / 1024
        logger.write(f"FOTOS: subiendo {i}/{len(paths)} desde {path.name} ({width}x{height}, {size_kb:.0f} KB)")
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
    wait_for_picture_uploads_to_settle(page, len(paths), logger)

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

def click_product_save_button_outside_modal(page: Page, logger: RunLogger, timeout_ms: int = 15000) -> None:
    last_error = ""
    deadline = datetime.now().timestamp() + (timeout_ms / 1000)
    while datetime.now().timestamp() < deadline:
        try:
            clicked = page.evaluate(
                """
                () => {
                  const words = ['guardar cambios', 'guardar y cerrar', 'guardar'];
                  const isVisible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== 'hidden'
                      && style.display !== 'none'
                      && rect.width > 0
                      && rect.height > 0;
                  };
                  const isDisabled = (el) => {
                    return el.disabled
                      || el.getAttribute('aria-disabled') === 'true'
                      || el.classList.contains('disabled');
                  };
                  const buttons = Array.from(document.querySelectorAll('button, [role="button"], input[type="submit"]'));
                  const button = buttons.reverse().find((el) => {
                    if (el.closest('[role="dialog"]')) return false;
                    if (!isVisible(el) || isDisabled(el)) return false;
                    const text = `${el.innerText || ''} ${el.value || ''}`.trim().toLowerCase();
                    return words.some((word) => text.includes(word));
                  });
                  if (!button) return false;
                  button.scrollIntoView({ block: 'center', inline: 'center' });
                  button.click();
                  return true;
                }
                """
            )
            if clicked:
                return
        except Exception as e:
            last_error = str(e)
        page.wait_for_timeout(500)

    raise RuntimeError(f"No se encontró el botón Guardar del producto fuera del modal. Último error: {last_error}")

def save_changes(page: Page, logger: RunLogger, reason: str = "cambios") -> None:
    logger.write(f"PRODUCTO: guardando {reason}...")
    click_save_button(page, logger)
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        page.wait_for_timeout(3500)
    logger.write(f"PRODUCTO: guardado OK ({reason}).")

def save_product_changes_outside_modal(page: Page, logger: RunLogger, reason: str = "cambios") -> None:
    logger.write(f"PRODUCTO: guardando {reason} fuera del modal...")
    click_product_save_button_outside_modal(page, logger)
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        page.wait_for_timeout(3500)
    logger.write(f"PRODUCTO: guardado OK ({reason}).")

def get_filtered_product_links(page: Page, timeout_ms: int = 8000) -> list[str]:
    """Wait until the filtered result set settles instead of always sleeping 5s."""
    links = page.locator('a[href^="/products/"]')
    deadline = time.monotonic() + timeout_ms / 1000
    last_count = -1
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        count = links.count()
        if count != last_count:
            last_count = count
            stable_since = time.monotonic()
        elif count > 0 and time.monotonic() - stable_since >= 0.6:
            break
        page.wait_for_timeout(150)
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

def wait_for_active_mercadolibre_product(page: Page, sku: str, href: str, logger: RunLogger, timeout_ms: int = 15000) -> bool:
    deadline = time.monotonic() + (timeout_ms / 1000)
    logged_wait = False
    last_state = ""

    while time.monotonic() < deadline:
        link = get_mercadolibre_link(page, timeout=800)
        integration_text = get_mercadolibre_integration_text(page)
        has_active_text = bool(re.search(r"\bActiva\b", integration_text, re.IGNORECASE))
        has_active_count = bool(re.search(r"\bActiva:\s*[1-9]\d*", integration_text, re.IGNORECASE))

        if link and (has_active_text or has_active_count):
            return True

        state = "sin link"
        if "Mercadolibre" in integration_text or "MercadoLibre" in integration_text:
            state = "integración Mercado Libre visible, esperando datos"
        if state != last_state:
            logger.write(f"LISTA: {state} para SKU {sku} en {href}")
            last_state = state
        elif not logged_wait:
            logger.write(f"LISTA: esperando que cargue la integración Mercado Libre para SKU {sku}...")
            logged_wait = True

        page.wait_for_timeout(500)

    return False

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
        if wait_for_active_mercadolibre_product(page, sku, href, logger):
            logger.write(f"LISTA: SKU {sku} asociado a Mercado Libre activo en {href}")
            return href
        logger.write(f"LISTA: candidato descartado para SKU {sku}, Mercado Libre no cargó activo: {href}")
    raise RuntimeError(f"No se encontró un producto con Mercado Libre activo para el SKU {sku}. Candidatos revisados: {len(hrefs)}")

def find_active_mercadolibre_product_hrefs(page: Page, sku: str, hrefs: list[str], logger: RunLogger) -> list[str]:
    logger.write(f"LISTA: expandiendo SKU {sku}; se validarán {len(hrefs)} candidato(s) activos...")
    active_hrefs = []
    for idx, href in enumerate(hrefs, start=1):
        logger.write(f"LISTA: validando candidato expandido {idx}/{len(hrefs)} para SKU {sku}: {href}")
        goto_product(page, href, logger)
        if wait_for_active_mercadolibre_product(page, sku, href, logger):
            active_hrefs.append(href)
            logger.write(f"LISTA: candidato activo agregado para SKU {sku}: {href}")
        else:
            logger.write(f"LISTA: candidato descartado al expandir SKU {sku}: {href}")
    if not active_hrefs:
        raise RuntimeError(f"No se encontró ningún producto activo de Mercado Libre para el SKU {sku}.")
    logger.write(f"LISTA: SKU {sku} expandido a {len(active_hrefs)} producto(s) activo(s).")
    return active_hrefs

def download_and_prepare(urls: list[str], logger: RunLogger) -> list[Path]:
    logger.write("FOTOS: descargando y convirtiendo imágenes...")
    def prepare_one(i: int, url: str) -> tuple[int, Path]:
        download_path = DOWNLOADS_DIR / f"img_{i:02d}.jpg"
        output_path = OUTPUT_DIR / f"img_{i:02d}.jpg"
        download_image(url, download_path)
        convert_to_1000(download_path, output_path)
        assert_image_is_1000(output_path)
        return i, output_path.resolve()

    prepared: dict[int, Path] = {}
    workers = min(4, len(urls))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(prepare_one, i, url): i for i, url in enumerate(urls, start=1)}
        for future in as_completed(futures):
            i = futures[future]
            try:
                image_index, output_path = future.result()
                prepared[image_index] = output_path
                logger.write(f"FOTOS: imagen {image_index} convertida a 1000x1000")
            except Exception as e:
                logger.write(f"FOTOS: error procesando imagen {i}: {e}")
    output_paths = [prepared[i] for i in sorted(prepared)]
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
    search.fill(filter_text)
    search.press("Enter")
    # Result readiness is detected by get_filtered_product_links.

def goto_product(page: Page, href: str, logger: RunLogger) -> None:
    url = f"https://app.producteca.com{href}"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        logger.write(f"PRODUCTO: navegación no confirmó domcontentloaded, sigo si la página responde: {e}")
        if page.is_closed():
            raise
    try:
        page.locator('a[href*="mercadolibre"], input, button').first.wait_for(state="attached", timeout=7000)
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
        item.step = "Obteniendo imágenes de Producteca"
        item.last_event = "Obteniendo imágenes de Producteca"
        urls = get_producteca_picture_urls(page, item.href, logger)

        if not urls:
            item.status = "Error"
            item.error = "No se encontraron imágenes en Producteca."
            logger.write(f"ERROR: {item.error}")
            return False, ""

        item.images_detected = len(urls)
        if len(urls) == 1:
            item.status = "Omitido"
            item.step = "Una sola imagen"
            item.last_event = "SKU guardado; producto omitido"
            logger.save_single_image_sku(item.sku)
            logger.write(
                f"FOTOS: SKU {item.sku} omitido porque Producteca tiene una sola imagen. "
                f"Guardado en {logger.single_image_skus_file}"
            )
            return False, "single_image"

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

        if not complete_dimensions_if_zero(page, item.href, logger):
            item.status = "Error"
            item.error = "No se pudieron completar dimensiones."
            return False, ""

        replace_product_pictures_via_api(page, item.href, output_paths, logger)
        verify_picture_count_after_save(page, len(output_paths), logger)
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
    statuses = [
        ProductStatus(index=i + 1, sku=sku, name_filter=name_filter)
        for i, raw_value in enumerate(skus)
        for sku, name_filter in [parse_sku_input_line(raw_value)]
    ]
    summary = {
        "ok": 0,
        "error": 0,
        "skipped": 0,
        "total": len(skus),
        "log_file": str(logger.log_file),
        "single_image_skus_file": str(logger.single_image_skus_file),
        "results": [],
    }

    if not skus:
        logger.write("No hay SKUs para procesar.")
        return summary

    ok_session, session_message = load_session_from_env_if_needed()
    logger.write(session_message)
    if not ok_session:
        return summary

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

            current_pos = 0
            while current_pos < len(statuses):
                item = statuses[current_pos]
                item_started_at = time.monotonic()
                i = current_pos + 1
                page = new_clean_page(context, page, logger)
                item.status = "En proceso"
                item.step = "Buscando producto Mercado Libre"
                item.last_event = f"Resolviendo producto {i}/{len(statuses)}"
                _notify(progress_callback, statuses)
                try:
                    logger.write("-" * 60)
                    logger.write(f"LISTA: resolviendo SKU {item.sku} ({i}/{len(statuses)})")
                    if item.href:
                        href = item.href
                        logger.write(f"LISTA: producto ya resuelto por expansión para SKU {item.sku}: {href}")
                    else:
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
                            current_pos += 1
                            item.elapsed_seconds = round(time.monotonic() - item_started_at, 1)
                            continue

                        if item.name_filter:
                            href = find_product_href_by_name_filter(page, item.sku, item.name_filter, hrefs, logger)
                        else:
                            active_hrefs = find_active_mercadolibre_product_hrefs(page, item.sku, hrefs, logger)
                            href = active_hrefs[0]
                            if len(active_hrefs) > 1:
                                additions = [
                                    ProductStatus(index=0, sku=item.sku, href=extra_href)
                                    for extra_href in active_hrefs[1:]
                                ]
                                statuses[current_pos + 1:current_pos + 1] = additions
                                for new_index, status in enumerate(statuses, start=1):
                                    status.index = new_index
                                summary["total"] = len(statuses)
                                item.last_event = f"SKU expandido a {len(active_hrefs)} productos activos"
                                logger.write(f"LISTA: se agregaron {len(additions)} trabajo(s) extra para SKU {item.sku}.")
                                _notify(progress_callback, statuses)

                    item.href = href
                    item.step = "Abriendo producto"
                    item.last_event = f"Abriendo producto Mercado Libre {i}/{len(statuses)}"
                    _notify(progress_callback, statuses)
                    goto_product(page, href, logger)

                    photos_ok, photos_result = process_photos(page, item, logger)
                    if not photos_ok:
                        if photos_result == "single_image":
                            summary["skipped"] += 1
                        else:
                            summary["error"] += 1
                        _notify(progress_callback, statuses)
                        close_page_if_open(page)
                        page = None
                        current_pos += 1
                        item.elapsed_seconds = round(time.monotonic() - item_started_at, 1)
                        continue

                    item.status = "OK"
                    item.step = "Finalizado"
                    item.last_event = "Proceso completo"
                    summary["ok"] += 1
                    item.elapsed_seconds = round(time.monotonic() - item_started_at, 1)
                    logger.write(f"TIEMPO: SKU {item.sku} completado en {item.elapsed_seconds:.1f}s")
                    _notify(progress_callback, statuses)
                    close_page_if_open(page)
                    page = None
                    current_pos += 1

                except Exception as e:
                    item.status = "Error"
                    item.error = str(e)
                    item.screenshot = save_failure_screenshot(page, item.sku, item.index)
                    summary["error"] += 1
                    item.elapsed_seconds = round(time.monotonic() - item_started_at, 1)
                    logger.write(f"ERROR: fallo general del producto {i}: {e}")
                    logger.write(traceback.format_exc())
                    _notify(progress_callback, statuses)
                    close_page_if_open(page)
                    page = None
                    current_pos += 1

            logger.write("=" * 60)
            logger.write(
                f"SISTEMA: finalizado. OK = {summary['ok']} | "
                f"OMITIDOS = {summary['skipped']} | ERROR = {summary['error']}"
            )
            summary["results"] = [asdict(s) for s in statuses]
            return summary

        finally:
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass
            browser.close()
