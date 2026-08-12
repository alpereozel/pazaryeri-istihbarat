# Pazaryeri İstihbarat V7

Trendyol / Hepsiburada ürün URL'sinden herkese açık ürün sinyallerini normalize eden ve kanıt seviyesine göre tahmini satış/ciro üreten MVP.

## V7 değişiklikleri
- Kategori ve kategori URL'si için JSON-LD + breadcrumb + HTML fallback'leri.
- Kategori/satış sırası sinyalini ayrı alan olarak gösterme.
- Açık 24 saatlik satış sinyali varsa en güçlü sinyal olarak kullanma.
- Toplam stok miktarını satış sinyali olarak KULLANMAMA.
- Satış tahmininde kanıt seviyesi: strong / medium / medium-low / weak / none.
- Günlük merkez tahmini + dar tahmin bandı.
- Günlük, 3 günlük, 7 günlük ve 30 günlük satış + ciro.
- Tahmin nedenlerinin kullanıcıya açık gösterimi.
- Eski SQLite veritabanı varsa `category_url` alanını otomatik ekleme.

## Önemli
Rakip mağazanın gerçek sipariş adedi resmi satıcı API'sinden alınamaz. Bu nedenle satış rakamları tahmindir. Açık bir satın alma/satış sinyali yoksa model bunu açıkça belirtir.

## Çalıştırma

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Render Start Command:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```
