# Pazaryeri İstihbarat MVP

Bu proje, Trendyol/Hepsiburada ürün URL'sinden ürün analizi başlatmak için hazırlanmış bir MVP iskeletidir.

## MVP'nin yaptığı
- Ürün URL'sini kabul eder.
- Pazaryerini URL'den algılar.
- Ürün/satıcı/fiyat/kategori/puan/yorum gibi verileri normalize eder.
- Zaman içinde alınan snapshot'ları SQLite'ta saklar.
- Stok ve yorum değişimlerinden tahmini satış hesaplar.
- Günlük/3 günlük/7 günlük/30 günlük tahmini satış ve ciro üretir.
- Kategori komisyon oranı girildiğinde tahmini komisyon ve katkı marjı hesaplar.
- Basit bir web dashboard'u sunar.

## Önemli
Rakip mağazanın gerçek sipariş verisi resmi satıcı API'sinden alınamaz. Bu MVP'deki satış rakamları "tahmin" mantığıyla çalışır.

Canlı Trendyol entegrasyonu için güncel Product V2 API'leri kullanılmalıdır. Trendyol'un eski Product V1 servisleri 10 Ağustos 2026 itibarıyla kullanım dışı bırakılmıştır.

## Çalıştırma

Python 3.11+:

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Sonra:
http://127.0.0.1:8000

## API
POST /api/analyze
```json
{"url":"https://www.trendyol.com/ornek-urun-p-123"}
```

POST /api/snapshots
```json
{
  "product_id":"trendyol:123",
  "price":299.90,
  "stock":87,
  "review_count":1241
}
```

GET /api/products
GET /api/products/{product_id}


## V2 ekran sıralaması
Analiz ekranında satış ve ciro tahminleri şu sırayla gösterilir:
1. Günlük satış + günlük ciro
2. 3 günlük satış + 3 günlük ciro
3. 7 günlük satış + 7 günlük ciro
4. Aylık (30 günlük) satış + aylık ciro

Bu değerler tahmindir; veri biriktikçe güven skoru yükselir.
