from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()

    page.goto("https://app.producteca.com/products")

    input("Logueate en Producteca y cuando ya estés adentro, apretá ENTER acá...")

    context.storage_state(path="session.json")
    browser.close()

print("session.json guardado OK")