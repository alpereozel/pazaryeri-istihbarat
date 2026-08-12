# Pazaryeri İstihbarat V10

Trendyol / Hepsiburada ürün linkinden açık ürün verilerini toplar, satış tahmini ve satış potansiyeli gösterir.

## V10 yenilikleri

- Kategoriye göre komisyon referansı ve mevcut fiyat üzerinden komisyon tutarı.
- `2 Saatlik Takibe Al` düğmesi.
- Takip listesi GitHub'da `data/tracked_urls.json` içinde tutulur.
- GitHub Actions, ürünleri **2 saatte bir** taramak üzere planlanmıştır.
- Takip geçmişi `tracker-data` branch'inde `data/tracking_history.json` olarak saklanır; böylece Render'ın geçici diskine bağlı kalmaz.
- Analiz ekranı geçmiş takip verisini de kullanabilir.
- Rakip araştırması bu sürümde özellikle eklenmedi.

## Ücretsiz çalışma mimarisi

- Render Free: web uygulaması.
- GitHub Actions: 2 saatlik takip. Public repository'lerde standart GitHub-hosted runner kullanımı ücretsizdir. GitHub schedule yoğunluk nedeniyle birkaç dakika gecikebilir.

## Bir kez yapılacak Render ayarları

`Takibe Al` düğmesinin GitHub'daki takip listesini güncelleyebilmesi için Render → Environment Variables bölümüne:

- `GITHUB_TOKEN` = GitHub fine-grained Personal Access Token
- `GITHUB_REPO` = `alperoezel/pazaryeri-istihbarat`
- `GITHUB_BRANCH` = `main`
- `TRACKING_BRANCH` = `tracker-data`

eklenmelidir.

Token için yalnızca bu repository'ye **Contents: Read and write** izni vermek yeterlidir. Token'ı kod içine yazmayın.

## Komisyon notu

Komisyon oranları otomatik eşleştiğinde ekranda referans oran gösterilir. Bu oranlar satıcıya özel sözleşme, alt kategori veya kampanyaya göre değişebilir; gerçek kesinti için satıcı panelindeki oran esas alınmalıdır.

## Çalıştırma

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```
