# Fatura Dedektifi — piyasa verisi

Bu depo tek bir iş yapar: **EPİAŞ Şeffaflık Platformu'ndan** günlük PTF (piyasa takas
fiyatı) ve aylık YEKDEM birim maliyetini çekip `data/piyasa/ptf-yekdem.json` dosyasına
yazar. Uygulama bu dosyayı indirir.

## Neden ayrı ve public bir depo?

Uygulama kodu **private** bir depoda. Ama bu dosyanın dağıtıldığı CDN (jsDelivr) yalnızca
public depolara hizmet veriyor. İçerik zaten kamuya açık piyasa verisi — gizli bir tarafı
yok. Bu yüzden kod private, veri public.

## Adres

```
https://cdn.jsdelivr.net/gh/06maslan27/fatura-dedektifi-veri@main/data/piyasa/ptf-yekdem.json
```

Uygulamaya gömülü olan adres budur; kullanıcı hiçbir kurulum yapmaz.

CDN arada olduğu için istekler bu depoya değil kenar sunuculara düşer — `raw.githubusercontent.com`
dosya dağıtımı için tasarlanmadığından kullanıcı sayısı büyüdüğünde kısıtlanır, jsDelivr ise
tam olarak bu iş için var. Uygulama günde bir kez ve ETag ile şartlı indirir; dosya
değişmediyse 304 döner ve hiç veri inmez.

## Yeni YEKDEM öngörüsü ekleme

Kurul yılda bir (bazen yıl ortasında revize ederek) aylık YEKDEM öngörülerini yayımlıyor.
Fatura hesabına giren değer budur — EPİAŞ'ın gerçekleşen bedeli değil (Tebliğ md. 6/6).

**Uygulamayı güncellemeye gerek yok.** Şunu yap:

1. [`data/yekdem-ongoru.csv`](data/yekdem-ongoru.csv) dosyasını aç
2. Sağ üstteki **kalem simgesine** bas
3. En alta satırları yaz:

```
2027,1,415.20,EPDK Kurul Kararı 15xxx / 12.2026
2027,2,398.60,
2027,3,372.40,
```

4. **Commit changes** de

Kaynağı bir kez yazman yeterli; boş bırakılan satırlar üstteki kaynağı devralır. Kurul
yıl ortasında revize ederse eski satırı silme, yenisini alta yaz — sonraki geçerli olur.

Telefondan da yapılabilir. Birkaç dakika içinde bütün kullanıcılara iner; uygulama zaten
her gün bu dosyayı indiriyor.

**Bir şeyi yanlış yazarsan ne olur:** iş kırmızı yanar, hangi satırın nesi bozuk yazar ve
**yayındaki dosya değişmez**. Yarım veri kimseye gitmez.

## Kurulum (tek seferlik)

1. `Settings → Secrets and variables → Actions` altına ekle:
   - `EPIAS_USERNAME` — EPİAŞ Şeffaflık Platformu kullanıcı adın (e-posta)
   - `EPIAS_PASSWORD` — parolan
2. `Actions → Piyasa verisi → Run workflow` ile elle bir kez çalıştır.
3. Sonrası günlük otomatik.

Parola yalnızca GitHub secret olarak durur; ne uygulamaya ne de bu depodaki dosyalara girer.

## Elle çalıştırma / alan adı doğrulama

EPİAŞ yanıt şeması sürümle değişebiliyor. Değişip değişmediğini görmek için:

```bash
EPIAS_USERNAME=... EPIAS_PASSWORD=... python tools/epias_mirror.py --dry-run
```

`--dry-run` hiçbir dosya yazmaz; yalnızca hangi alanların geldiğini basar.
