"""使用 Playwright + stealth 自动化登录 chatgpt.com 并提取 session token."""

import asyncio
import json
import glob
from playwright.async_api import async_playwright
from playwright_stealth import stealth

async def test_login():
    # Load existing token for credentials
    files = sorted(glob.glob('web_token/*.json'))
    if not files:
        print("No token files found")
        return
    
    with open(files[-1]) as f:
        token = json.load(f)
    
    email = token['email']
    password = token['password']
    print(f"Using account: {email}")
    
    async with async_playwright() as p:
        # Launch with stealth args
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--disable-web-security',
                '--disable-features=IsolateOrigins,site-per-process',
            ]
        )
        
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.7103.112 Safari/537.36',
            viewport={'width': 1920, 'height': 1080},
            locale='en-US',
            timezone_id='America/New_York',
        )
        
        page = await context.new_page()
        await stealth(page)
        
        # Test 1: Visit auth.openai.com/log-in directly
        print("\n=== Test 1: Visit auth.openai.com/log-in ===")
        try:
            response = await page.goto('https://auth.openai.com/log-in', wait_until='domcontentloaded', timeout=30000)
            print(f"Status: {response.status if response else 'None'}")
            print(f"URL: {page.url}")
            
            # Wait for page to load
            await asyncio.sleep(3)
            
            # Check if we're on login page
            title = await page.title()
            print(f"Title: {title}")
            
            # Try to find email input
            email_input = await page.query_selector('input[type="email"], input[name="username"], input#email')
            print(f"Email input found: {email_input is not None}")
            
            if email_input:
                print("Page appears to be login form - trying to fill credentials...")
                # Fill email
                await email_input.fill(email)
                
                # Find continue/submit button
                continue_btn = await page.query_selector('button[type="submit"], button:has-text("Continue"), button:has-text("Sign in")')
                if continue_btn:
                    await continue_btn.click()
                    await asyncio.sleep(2)
                    
                    # Check for password field
                    password_input = await page.query_selector('input[type="password"]')
                    if password_input:
                        print("Password field appeared")
                        await password_input.fill(password)
                        
                        # Find login button
                        login_btn = await page.query_selector('button[type="submit"], button:has-text("Log in"), button:has-text("Sign in")')
                        if login_btn:
                            await login_btn.click()
                            await asyncio.sleep(3)
                            
                            print(f"After login click: {page.url}")
                            
                            # Check cookies
                            cookies = await context.cookies()
                            for c in cookies:
                                if 'session' in c['name'] or 'auth' in c['name']:
                                    print(f"Cookie: {c['name']} domain={c['domain']}")
                    else:
                        print("No password field appeared - might be OTP or error page")
                        content = await page.content()
                        print(f"Page content snippet: {content[:500]}")
                else:
                    print("No continue button found")
            else:
                # Check what page we're on
                content = await page.content()
                print(f"Page content (first 1000 chars): {content[:1000]}")
                
        except Exception as e:
            print(f"Error during login test: {e}")
        
        await browser.close()

asyncio.run(test_login())
