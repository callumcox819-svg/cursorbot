# email_matcher.py
import re

def normalize_email(email: str) -> str:
    if not email:
        return ''
    email = email.lower().strip()
    if '@gmail.com' in email or '@googlemail.com' in email:
        local, _ = email.split('@', 1)
        local = local.split('+', 1)[0].replace('.', '')
        return f"{local}@gmail.com"
    return email

def normalize_name(name: str) -> str:
    if not name:
        return ''
    name = name.lower()
    rep = {'ä':'a','ö':'o','ü':'u','ß':'ss'}
    for k,v in rep.items(): name = name.replace(k,v)
    name = re.sub(r'[^a-z0-9]', '', name)
    return name

async def match_by_seller_name(session, from_name):
    return None

async def match_by_item_title(session, subject):
    return None
