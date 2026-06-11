from __future__ import annotations

import json
import os
import re
from typing import Dict

import requests

from attribute_rules import JSON_ATTRIBUTES, FIXED_ATTRIBUTE_VALUES

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

def _build_prompt(name: str, sku: str, link: str) -> str:
    body = """Actuá como un asistente de carga de datos para sistemas.

Buscá información confiable del siguiente producto usando Mercado Libre, fabricante, Amazon u otras tiendas reconocidas.

Producto:
- Nombre: {name}
- SKU: {sku}
- Link: {link}

Necesito que devuelvas EXCLUSIVAMENTE un JSON válido, sin texto adicional, sin explicación, sin markdown.

Los atributos a completar son:

- Edad recomendada
- Material de las fichas
- Material
- Cantidad de piezas
- Funciona a pila
- Cantidad de pilas
- Tipo de pila
- Incluye pilas
- Con sonido
- Con luces
- Tiene articulacion
- Personaje
- Tipo de producto
- Incluye
- Dimensiones (largo x alto x ancho)
- Alto
- Ancho
- Peso
- Origen
- Garantia

Reglas:
- Si un dato no se encuentra claramente, devolver "" (string vacío)
- No inventar información
- Para Alto y Ancho usar formato: "12 cm"
- Para Peso usar formato: "350 gr"
- Para Dimensiones usar formato: "largo x alto x ancho cm"
- Para booleanos usar: "Sí" o "No"
- No agregar campos extra

Formato de salida:

{{
  "Edad recomendada": "",
  "Material de las fichas": "",
  "Material": "",
  "Cantidad de piezas": "",
  "Funciona a pila": "",
  "Cantidad de pilas": "",
  "Tipo de pila": "",
  "Incluye pilas": "",
  "Con sonido": "",
  "Con luces": "",
  "Tiene articulacion": "",
  "Personaje": "",
  "Tipo de producto": "",
  "Incluye": "",
  "Dimensiones (largo x alto x ancho)": "",
  "Alto": "",
  "Ancho": "",
  "Peso": "",
  "Origen": "",
  "Garantia": ""
}}"""
    return body.format(name=name or "", sku=sku or "", link=link or "")

def _extract_text_from_response(payload: dict) -> str:
    try:
        parts = payload["candidates"][0]["content"]["parts"]
        return "".join(part.get("text", "") for part in parts)
    except Exception as e:
        raise RuntimeError(f"No se pudo leer la respuesta de Gemini: {e}") from e

def _extract_json(text: str) -> dict:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    first = text.find("{")
    last = text.rfind("}")
    if first == -1 or last == -1 or last <= first:
        raise RuntimeError("Gemini no devolvió un JSON válido.")
    text = text[first:last+1]
    return json.loads(text)

def research_attributes(name: str, sku: str, link: str, logger=None) -> Dict[str, str]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Falta GEMINI_API_KEY en variables de entorno.")

    prompt = _build_prompt(name=name, sku=sku, link=link)
    if logger:
        logger.write(f"ATRIBUTOS: consultando Gemini para SKU {sku}...")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
    }

    response = requests.post(url, headers=headers, json=payload, timeout=90)
    response.raise_for_status()
    data = response.json()
    raw_text = _extract_text_from_response(data)
    parsed = _extract_json(raw_text)

    result = {}
    for field in JSON_ATTRIBUTES:
        value = parsed.get(field, "")
        result[field] = "" if value is None else str(value).strip()

    for key, value in FIXED_ATTRIBUTE_VALUES.items():
        result[key] = value

    if logger:
        found = sum(1 for v in result.values() if str(v).strip())
        logger.write(f"ATRIBUTOS: Gemini devolvió {found} valores no vacíos para SKU {sku}.")
    return result
