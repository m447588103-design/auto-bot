import asyncio
from playwright.async_api import async_playwright

async def run_browser_task(query: str) -> str:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        try:
            # Example automated action: Search or open a site
            if "github" in query.lower():
                await page.goto("https://github.com/trending")
                title = await page.title()
                await browser.close()
                return f"Successfully accessed GitHub Trending. Page Title: {title}"
            else:
                await page.goto("https://www.google.com")
                await browser.close()
                return "Successfully accessed Google and performed browser check."
        except Exception as e:
            await browser.close()
            return f"Browser automation failed: {str(e)}"
