"""测试: 在 create_account 之前访问不同 client_id 的 OAuth authorize URL,
观察 create_account 返回的 continue_url 变化."""

from curl_cffi import requests
import urllib.parse
import json

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.7103.112 Safari/537.36'

def test_client_id(client_id, redirect_uri, scope='openid email profile offline_access', simplified=False):
    print(f"\n{'='*60}")
    print(f"Testing client_id={client_id[:30]}...")
    print(f"redirect_uri={redirect_uri}")
    
    session = requests.Session(impersonate='chrome136')
    
    # Step 1: Visit OAuth authorize URL to set context cookies
    state = 'teststate123'
    params = {
        'client_id': client_id,
        'response_type': 'code',
        'redirect_uri': redirect_uri,
        'scope': scope,
        'state': state,
        'prompt': 'login',
        'id_token_add_organizations': 'true',
    }
    if simplified:
        params['codex_cli_simplified_flow'] = 'true'
    
    url = f'https://auth.openai.com/oauth/authorize?{urllib.parse.urlencode(params)}'
    try:
        resp = session.get(url, headers={'User-Agent': UA, 'Accept': 'text/html,*/*'}, timeout=30, allow_redirects=False)
        print(f"  Authorize URL status: {resp.status_code}")
        
        # Follow redirects
        for i in range(5):
            loc = resp.headers.get('Location', '')
            if resp.status_code not in (301, 302, 303, 307, 308) or not loc:
                break
            url = urllib.parse.urljoin(str(resp.url), loc)
            resp = session.get(url, headers={'User-Agent': UA, 'Accept': 'text/html,*/*'}, timeout=30, allow_redirects=False)
        
        print(f"  Final after redirects: {resp.status_code} {str(resp.url)[:80]}")
    except Exception as e:
        print(f"  Error visiting authorize URL: {e}")
        return
    
    # Check cookies
    print("  Cookies set:")
    for c in session.cookies.jar:
        print(f"    {c.name} domain={c.domain}")
        if c.name in ('rg_context', 'iss_context'):
            try:
                import base64
                decoded = base64.b64decode(c.value)
                print(f"      (decoded: {decoded[:100]})")
            except:
                print(f"      (value: {c.value[:100]})")
    
    # Step 2: Try create_account with a dummy payload
    # We just want to see what continue_url would be returned
    # We need a valid sentinel token for this...
    print("  (Skipping create_account test - needs valid sentinel)")

# Test different client_ids
print("Testing OAuth context cookies for different client_ids")

test_client_id(
    'app_2SKx67EdpoN0G6j64rFvigXD',
    'https://platform.openai.com/auth/callback',
)

test_client_id(
    'app_EMoamEEZ73f0CkXaXp7hrann',
    'http://localhost:1455/auth/callback',
    simplified=True,
)

test_client_id(
    'app_X8zY6vW2pQ9tR3dE7nK1jL5gH',
    'https://chatgpt.com/api/auth/callback/openai',
)

# Maybe there's another client_id for API/web?
print("\n" + "="*60)
print("Looking for other client_ids in codebase...")
