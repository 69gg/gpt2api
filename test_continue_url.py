"""测试不同 OAuth 预设置下 create_account 返回的 continue_url."""

from curl_cffi import requests
import urllib.parse
import json
import base64
import secrets
import re

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.7103.112 Safari/537.36'

def _random_state():
    return secrets.token_urlsafe(32)

def _pkce_verifier():
    return secrets.token_urlsafe(32)

def _sha256_b64url_no_pad(s: str) -> str:
    import hashlib
    return base64.urlsafe_b64encode(hashlib.sha256(s.encode()).digest()).decode().rstrip("=")

def setup_oauth_context(session, client_id, redirect_uri, scope='openid email profile offline_access', simplified=False):
    """Setup OAuth context by visiting authorize URL, like reg_new.py does."""
    state = _random_state()
    code_verifier = _pkce_verifier()
    code_challenge = _sha256_b64url_no_pad(code_verifier)
    
    params = {
        'client_id': client_id,
        'response_type': 'code',
        'redirect_uri': redirect_uri,
        'scope': scope,
        'state': state,
        'code_challenge': code_challenge,
        'code_challenge_method': 'S256',
        'prompt': 'login',
        'id_token_add_organizations': 'true',
    }
    if simplified:
        params['codex_cli_simplified_flow'] = 'true'
    
    url = f'https://auth.openai.com/oauth/authorize?{urllib.parse.urlencode(params)}'
    
    try:
        resp = session.get(url, headers={'User-Agent': UA, 'Accept': 'text/html,*/*'}, timeout=30, allow_redirects=False)
        print(f"  OAuth visit: {resp.status_code}")
        
        # Follow to /log-in
        for i in range(10):
            loc = resp.headers.get('Location', '')
            if resp.status_code not in (301, 302, 303, 307, 308) or not loc:
                break
            url = urllib.parse.urljoin(str(resp.url), loc)
            resp = session.get(url, headers={'User-Agent': UA, 'Accept': 'text/html,*/*'}, timeout=30, allow_redirects=False)
        
        print(f"  Final URL: {str(resp.url)[:80]}")
    except Exception as e:
        print(f"  Error: {e}")
        return None
    
    # Print cookies
    print("  Cookies after OAuth visit:")
    for c in session.cookies.jar:
        print(f"    {c.name} domain={c.domain}")
        if c.name == 'rg_context':
            # Try to decode
            val = c.value
            print(f"      raw: {val[:50]}")
            try:
                decoded = base64.b64decode(val)
                print(f"      base64 decoded: {decoded[:50]}")
            except:
                try:
                    decoded = base64.urlsafe_b64decode(val + '=' * ((4 - len(val) % 4) % 4))
                    print(f"      base64url decoded: {decoded[:50]}")
                except:
                    pass
    
    return state, code_verifier


def test_client_id(client_id, redirect_uri, simplified=False, label=""):
    print(f"\n{'='*60}")
    print(f"Testing: {label}")
    print(f"client_id={client_id}")
    print(f"redirect_uri={redirect_uri}")
    
    session = requests.Session(impersonate='chrome136')
    
    # Step 1: Visit OAuth authorize URL
    oauth_result = setup_oauth_context(session, client_id, redirect_uri, simplified=simplified)
    if not oauth_result:
        return
    
    state, code_verifier = oauth_result
    
    # Step 2: Get did and check cookies
    did = str(session.cookies.get('oai-did') or '').strip()
    if not did:
        print("  No oai-did found!")
        return
    print(f"  did: {did[:20]}...")
    
    print(f"  OAuth context established successfully")

# Test different client_ids
test_client_id(
    'app_2SKx67EdpoN0G6j64rFvigXD',
    'https://platform.openai.com/auth/callback',
    label='Platform'
)

test_client_id(
    'app_EMoamEEZ73f0CkXaXp7hrann',
    'http://localhost:1455/auth/callback',
    simplified=True,
    label='Codex'
)

test_client_id(
    'app_X8zY6vW2pQ9tR3dE7nK1jL5gH',
    'https://chatgpt.com/api/auth/callback/openai',
    label='ChatGPT NextAuth'
)

print("\nDone!")
