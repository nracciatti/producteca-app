from __future__ import annotations

TARGET_ATTRIBUTES = [
    "Edad recomendada",
    "Material de las fichas",
    "Material",
    "Cantidad de piezas",
    "Funciona a pila",
    "Cantidad de pilas",
    "Tipo de pila",
    "Incluye pilas",
    "Con sonido",
    "Con luces",
    "Tiene articulacion",
    "Personaje",
    "Tipo de producto",
    "Incluye",
    "Dimensiones (largo x alto x ancho)",
    "Alto",
    "Ancho",
    "Peso",
    "Origen",
    "Garantia",
    "Impuesto interno 0",
    "IVA 21",
]

FIXED_ATTRIBUTE_VALUES = {
    "Impuesto interno 0": "Sí",
    "IVA 21": "Sí",
}

JSON_ATTRIBUTES = [
    "Edad recomendada",
    "Material de las fichas",
    "Material",
    "Cantidad de piezas",
    "Funciona a pila",
    "Cantidad de pilas",
    "Tipo de pila",
    "Incluye pilas",
    "Con sonido",
    "Con luces",
    "Tiene articulacion",
    "Personaje",
    "Tipo de producto",
    "Incluye",
    "Dimensiones (largo x alto x ancho)",
    "Alto",
    "Ancho",
    "Peso",
    "Origen",
    "Garantia",
]

ATTRIBUTE_RULES = {
    "Edad recomendada": {"allowed": ["edad recomendada"], "blocked": []},
    "Material de las fichas": {"allowed": ["material de las fichas"], "blocked": ["material"]},
    "Material": {"allowed": ["material"], "blocked": ["material de las fichas", "material del empaque"]},
    "Cantidad de piezas": {"allowed": ["cantidad de piezas"], "blocked": []},
    "Funciona a pila": {"allowed": ["funciona a pila", "funciona a pilas"], "blocked": []},
    "Cantidad de pilas": {"allowed": ["cantidad de pilas"], "blocked": []},
    "Tipo de pila": {"allowed": ["tipo de pila"], "blocked": []},
    "Incluye pilas": {"allowed": ["incluye pilas"], "blocked": []},
    "Con sonido": {"allowed": ["con sonido"], "blocked": []},
    "Con luces": {"allowed": ["con luces"], "blocked": []},
    "Tiene articulacion": {"allowed": ["tiene articulacion", "tiene articulación"], "blocked": []},
    "Personaje": {"allowed": ["personaje"], "blocked": []},
    "Tipo de producto": {"allowed": ["tipo de producto"], "blocked": []},
    "Incluye": {"allowed": ["incluye"], "blocked": ["incluye pilas"]},
    "Dimensiones (largo x alto x ancho)": {"allowed": ["dimensiones (largo x alto x ancho)", "dimensiones"], "blocked": []},
    "Alto": {"allowed": ["alto"], "blocked": ["alto del empaque"]},
    "Ancho": {"allowed": ["ancho"], "blocked": ["ancho del empaque"]},
    "Peso": {"allowed": ["peso"], "blocked": ["peso del empaque", "peso del paquete"]},
    "Origen": {"allowed": ["origen", "pais de origen", "país de origen"], "blocked": []},
    "Garantia": {"allowed": ["garantia", "garantía"], "blocked": []},
    "Impuesto interno 0": {"allowed": ["impuesto interno 0", "impuesto interno"], "blocked": []},
    "IVA 21": {"allowed": ["iva 21", "iva"], "blocked": []},
}

def normalize_text(value: str) -> str:
    replacements = {
        "á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u",
        "Á": "a", "É": "e", "Í": "i", "Ó": "o", "Ú": "u",
    }
    for old, new in replacements.items():
        value = value.replace(old, new)
    return " ".join(value.strip().lower().split())

def choose_attribute_match(target: str, options: list[str]) -> str | None:
    rules = ATTRIBUTE_RULES.get(target, {"allowed": [target], "blocked": []})
    normalized_options = [(opt, normalize_text(opt)) for opt in options if opt.strip()]
    allowed = {normalize_text(x) for x in rules["allowed"]}
    blocked = {normalize_text(x) for x in rules["blocked"]}

    exact = [raw for raw, norm in normalized_options if norm in allowed and norm not in blocked]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None

    partial = []
    for raw, norm in normalized_options:
        if norm in blocked:
            continue
        for candidate in allowed:
            if candidate in norm:
                partial.append(raw)
                break

    partial = list(dict.fromkeys(partial))
    if len(partial) == 1:
        return partial[0]
    return None
