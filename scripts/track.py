import json, os, sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.main import parse_public_product, marketplace, product_id_from_url

ROOT = Path(__file__).resolve().parents[1]
TRACKED = ROOT / 'data' / 'tracked_urls.json'
HISTORY = ROOT / 'data' / 'tracking_history.json'

try:
    tracked = json.loads(TRACKED.read_text(encoding='utf-8')) if TRACKED.exists() else []
except Exception:
    tracked = []
try:
    history = json.loads(HISTORY.read_text(encoding='utf-8')) if HISTORY.exists() else {}
except Exception:
    history = {}

if not isinstance(tracked, list): tracked = []
if not isinstance(history, dict): history = {}

now = datetime.now(timezone.utc).isoformat()
for url in tracked:
    try:
        mp = marketplace(url)
        if mp == 'unknown':
            continue
        pid = f'{mp}:{product_id_from_url(url)}'
        data = parse_public_product(url)
        snap = {
            'captured_at': now,
            'url': url,
            'marketplace': mp,
            'title': data.get('title'),
            'price': data.get('price'),
            'list_price': data.get('list_price'),
            'stock': data.get('stock'),
            'review_count': data.get('review_count'),
            'rating': data.get('rating'),
            'seller': data.get('seller'),
            'brand': data.get('brand'),
            'category': data.get('category'),
            'category_rank': data.get('category_rank'),
            'category_rank_source': data.get('category_rank_source'),
            'sales_badge': data.get('sales_badge'),
            'sales_badge_rank': data.get('sales_badge_rank'),
            'purchase_signal_24h': data.get('purchase_signal_24h'),
            'sold_total_signal': data.get('sold_total_signal'),
        }
        history.setdefault(pid, []).append(snap)
        # Keep the repo lightweight while preserving long-term trends.
        history[pid] = history[pid][-1000:]
        print(f'OK {pid}: {data.get("title")}')
    except Exception as e:
        print(f'ERROR {url}: {e}')

HISTORY.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding='utf-8')
