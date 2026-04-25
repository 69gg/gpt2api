import asyncio
from playwright.async_api import async_playwright

async def test():
    print("Starting browser...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        print("Browser launched")
        page = await browser.new_page()
        print("Page created")
        response = await page.goto('https://auth.openai.com/log-in', timeout=30000)
        print(f"Response status: {response.status if response else 'None'}")
        print(f"URL: {page.url}")
        title = await page.title()
        print(f"Title: {title}")
        content = await page.content()
        print(f"Content length: {len(content)}")
        print(f"Content snippet: {content[:1000]}")
        await browser.close()
        print("Browser closed")

asyncio.run(test())
