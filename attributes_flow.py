from __future__ import annotations

from typing import Dict, List

from playwright.sync_api import Page

from attribute_rules import TARGET_ATTRIBUTES, choose_attribute_match, normalize_text

def _click_text(page: Page, candidates: List[str]) -> bool:
    for text in candidates:
        loc = page.locator(f'text="{text}"')
        if loc.count() > 0:
            loc.first.click()
            return True
    return False

def open_mass_attribute_editor_for_current_filter(page: Page, logger) -> None:
    logger.write("ATRIBUTOS: abriendo Acciones...")
    for sel in ['button:has-text("Acciones")', '[role="button"]:has-text("Acciones")', 'text="Acciones"']:
        loc = page.locator(sel)
        if loc.count() > 0:
            loc.first.click()
            break
    else:
        raise RuntimeError('No se encontró el botón "Acciones".')

    logger.write("ATRIBUTOS: entrando a Edición masiva de atributos...")
    if not _click_text(page, ['Edición masiva de atributos', 'Edicion masiva de atributos']):
        raise RuntimeError('No se encontró la opción "Edición masiva de atributos".')
    page.wait_for_timeout(2500)

def select_required_attributes(page: Page, logger, attributes: List[str] | None = None) -> None:
    attributes = attributes or TARGET_ATTRIBUTES
    logger.write("ATRIBUTOS: seleccionando atributos requeridos...")
    dialog = page.locator('[role="dialog"]').last
    if dialog.count() == 0:
        dialog = page.locator("body")

    search_box = None
    for candidate in [dialog.locator('input[placeholder*="Buscar"]'), dialog.locator('input[type="search"]'), dialog.locator('input').first]:
        if candidate.count() > 0:
            search_box = candidate.first
            break
    if search_box is None:
        raise RuntimeError("No se encontró el buscador de atributos.")

    for attr in attributes:
        logger.write(f'ATRIBUTOS: buscando "{attr}"...')
        search_box.click()
        search_box.fill("")
        page.wait_for_timeout(200)
        search_box.fill(attr)
        page.wait_for_timeout(800)

        option_locators = dialog.locator('label, [role="checkbox"], .checkbox, .check')
        option_texts = []
        total = option_locators.count()
        for i in range(min(total, 80)):
            try:
                text = option_locators.nth(i).inner_text().strip()
                if text:
                    option_texts.append(text)
            except Exception:
                pass

        chosen = choose_attribute_match(attr, option_texts)
        if not chosen:
            logger.write(f'ATRIBUTOS: no hubo match confiable para "{attr}".')
            continue

        for candidate in [dialog.locator(f'label:has-text("{chosen}")'), dialog.locator(f'text="{chosen}"')]:
            if candidate.count() > 0:
                candidate.first.click()
                logger.write(f'ATRIBUTOS: marcado "{chosen}".')
                break
        else:
            logger.write(f'ATRIBUTOS: no se pudo clickear "{chosen}".')

    logger.write("ATRIBUTOS: continuando a la grilla...")
    for candidate in [dialog.locator('button:has-text("Continuar")'), dialog.locator('[role="button"]:has-text("Continuar")'), page.locator('button:has-text("Continuar")')]:
        if candidate.count() > 0:
            candidate.first.click()
            page.wait_for_timeout(3500)
            return
    raise RuntimeError('No se encontró el botón "Continuar".')

def _get_header_map(page: Page) -> Dict[str, int]:
    data = page.evaluate("""
    () => {
      const headers = [];
      const candidates = Array.from(document.querySelectorAll('[role="columnheader"], th, .ag-header-cell'));
      candidates.forEach((el, idx) => {
        const text = (el.innerText || el.textContent || '').trim();
        if (text) headers.push({text, idx});
      });
      return headers;
    }
    """)
    return {item["text"].strip(): item["idx"] for item in data}

def _get_row_index_for_sku(page: Page, sku: str) -> int | None:
    return page.evaluate("""
    (sku) => {
      const rows = Array.from(document.querySelectorAll('[role="row"], tr, .ag-row'));
      for (let i = 0; i < rows.length; i++) {
        const txt = (rows[i].innerText || rows[i].textContent || '').trim();
        if (txt.includes(sku)) return i;
      }
      return null;
    }
    """, sku)

def _focus_cell(page: Page, row_index: int, col_index: int):
    candidates = [
        page.locator('[role="row"]').nth(row_index).locator('[role="gridcell"]').nth(col_index),
        page.locator('tr').nth(row_index).locator('td').nth(col_index),
        page.locator('.ag-row').nth(row_index).locator('.ag-cell').nth(col_index),
    ]
    for cell in candidates:
        try:
            if cell.count() > 0:
                cell.scroll_into_view_if_needed()
                cell.dblclick()
                page.wait_for_timeout(250)
                return cell
        except Exception:
            pass
    return None

def _set_active_cell_text(page: Page, value: str) -> bool:
    for editor in [page.locator('input[type="text"]').last, page.locator('textarea').last, page.locator('[contenteditable="true"]').last]:
        try:
            if editor.count() > 0 and editor.is_visible():
                try:
                    tag = editor.evaluate("el => el.tagName.toLowerCase()")
                except Exception:
                    tag = "input"
                if tag in ["input", "textarea"]:
                    editor.fill("")
                    editor.type(value, delay=10)
                else:
                    editor.click()
                    page.keyboard.press("Meta+A")
                    page.keyboard.type(value, delay=10)
                page.keyboard.press("Tab")
                page.wait_for_timeout(150)
                return True
        except Exception:
            pass
    try:
        page.keyboard.press("Meta+A")
        page.keyboard.type(value, delay=10)
        page.keyboard.press("Tab")
        page.wait_for_timeout(150)
        return True
    except Exception:
        return False

def fill_attribute_grid(page: Page, sku: str, values: Dict[str, str], logger) -> None:
    logger.write("ATRIBUTOS: leyendo encabezados de la grilla...")
    page.wait_for_timeout(2500)
    header_map_raw = _get_header_map(page)
    normalized_map = {normalize_text(k): v for k, v in header_map_raw.items()}

    row_index = _get_row_index_for_sku(page, sku)
    if row_index is None:
        raise RuntimeError(f"No se encontró la fila del SKU {sku} en la grilla.")
    logger.write(f"ATRIBUTOS: fila detectada para SKU {sku} = {row_index}")

    for attr, value in values.items():
        if not str(value).strip():
            continue

        normalized_attr = normalize_text(attr)
        target_col = normalized_map.get(normalized_attr)
        if target_col is None:
            for raw_header, idx in header_map_raw.items():
                if normalized_attr == normalize_text(raw_header):
                    target_col = idx
                    break

        if target_col is None:
            logger.write(f'ATRIBUTOS: no se encontró la columna "{attr}".')
            continue

        cell = _focus_cell(page, row_index, target_col)
        if cell is None:
            logger.write(f'ATRIBUTOS: no se pudo enfocar la celda de "{attr}".')
            continue

        ok = _set_active_cell_text(page, str(value))
        if ok:
            logger.write(f'ATRIBUTOS: cargado "{attr}" = "{value}"')
        else:
            logger.write(f'ATRIBUTOS: no se pudo escribir "{attr}"')

    logger.write("ATRIBUTOS: guardando grilla...")
    for candidate in [page.locator('button:has-text("Guardar")'), page.locator('[role="button"]:has-text("Guardar")'), page.locator('text="Guardar"')]:
        if candidate.count() > 0:
            candidate.first.click()
            page.wait_for_timeout(4000)
            return
    raise RuntimeError('No se encontró el botón "Guardar" en la grilla.')
