from __future__ import annotations

import os
from pathlib import Path

from automation import BASE_DIR, parse_skus, run_job

SKUS_FILE = BASE_DIR / "skus.txt"


def main() -> None:
    os.environ.setdefault("PRODUCTECA_FAST", "1")

    if not SKUS_FILE.exists():
        SKUS_FILE.write_text("31660008\n", encoding="utf-8")
        print(f"Se creó {SKUS_FILE}. Editalo con un SKU por línea y volvé a ejecutar.")
        return

    raw_skus = SKUS_FILE.read_text(encoding="utf-8")
    skus = parse_skus(raw_skus)
    if not skus:
        print(f"No hay SKUs en {SKUS_FILE}. Agregá un SKU por línea.")
        return

    print(f"SKUs cargados: {len(skus)}")
    result = run_job(skus, headless=False)

    print("\nResumen")
    print(f"Total: {result['total']}")
    print(f"OK: {result['ok']}")
    print(f"Error: {result['error']}")
    print(f"Log: {result['log_file']}")

    if result["results"]:
        print("\nResultados")
        for item in result["results"]:
            status = item.get("status", "")
            sku = item.get("sku", "")
            error = item.get("error", "")
            suffix = f" - {error}" if error else ""
            print(f"{sku}: {status}{suffix}")


if __name__ == "__main__":
    main()
