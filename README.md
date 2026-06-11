# Producteca Fotos · V3

## Antes de correr en Mac
Necesitás:
1. `session.json` en esta carpeta

```bash
python3 -m pip install -r requirements.txt
python3 -m playwright install
python3 -m streamlit run app.py
```

## Autenticación
Opcionalmente podés proteger la app con variables de entorno:

```bash
APP_USERNAME=usuario
APP_PASSWORD=clave-segura
```

Si solo definís `APP_PASSWORD`, la app pide únicamente contraseña.

## Deploy
La app está preparada para Docker. En producción conviene:

1. usar `HEADLESS=true`
2. definir `APP_USERNAME` y `APP_PASSWORD`
3. no subir `session.json` al repositorio
4. cargar la sesión como variable/secreto `SESSION_JSON_CONTENT` con el contenido completo del JSON

También se acepta `session_json_content` en minúsculas, útil si el panel de deploy o Streamlit Secrets ya quedó configurado con ese nombre.

El valor tiene que ser el contenido completo de `session.json`, por ejemplo:

```json
{"cookies":[...],"origins":[...]}
```

El contenedor expone Streamlit en el puerto `8501`.

## Qué hace
Por cada SKU:
1. filtra el SKU en Producteca
2. revisa los productos encontrados
3. elige únicamente el producto con link de Mercado Libre
4. extrae el item_id del link
5. consulta `https://api.mercadolibre.com/items/{item_id}`
6. descarga las imágenes del campo `pictures`
7. elimina variantes si existen
8. borra las fotos actuales
9. sube las fotos nuevas una por una y guarda

## Recomendación
Usá navegador Visible.

## Nota honesta
Playwright se usa solo para Producteca. Mercado Libre se consulta por API pública para evitar abrir publicaciones con navegador.
