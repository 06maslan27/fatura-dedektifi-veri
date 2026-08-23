#!/usr/bin/env python3
"""
EPİAŞ ŞEFFAFLIK PLATFORMU AYNASI

EPİAŞ'tan PTF (piyasa takas fiyatı) ve YEKDEM birim maliyet verisini çeker, uygulamanın
okuduğu ``PriceSeries`` biçiminde tek bir JSON dosyası üretir.

NEDEN AYNA: EPİAŞ Şeffaflık 2.0 web servisleri kayıtlı hesapla TGT (ticket) almayı
zorunlu tutuyor; anonim erişim yok. Bu kimlik bilgisi APK'ya gömülemez — herkes tarafından
çıkarılabilir ve hesap kapanır. Bu yüzden veriyi GitHub Actions çeker (kimlik bilgisi
GitHub secret'ında kalır), sonucu küçük bir JSON olarak yayımlar; uygulama yalnızca o
dosyayı indirir. Uygulamadan EPİAŞ'a hiçbir istek gitmez, EPİAŞ'a hiçbir kullanıcı verisi
gönderilmez.

Kullanım:
    export EPIAS_USERNAME="..." EPIAS_PASSWORD="..."
    python epias_mirror.py --days 45 --out ../../data/piyasa/ptf-yekdem.json

İlk kez çalıştırırken alan adlarını gözle doğrulamak için:
    python epias_mirror.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import getpass
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

CAS_URL = "https://giris.epias.com.tr/cas/v1/tickets"
BASE_URL = "https://seffaflik.epias.com.tr"

# Uçlar 401 (var, kimlik gerekiyor) / 404 (yok) farkıyla doğrulandı.
MCP_PATH = "/electricity-service/v1/markets/dam/data/mcp"            # PTF, saatlik
YEKDEM_UNIT_COST_PATH = "/electricity-service/v1/renewables/data/unit-cost"  # YEKDEM birim maliyet

# GÖP EŞLEŞME MİKTARI — günlük PTF'nin AĞIRLIĞI budur.
#
# Mevzuat "EPİAŞ tarafından Türkiye için günlük açıklanan AĞIRLIKLI ORTALAMA piyasa takas
# fiyatları" der (1 Nolu Açıklama md. 12). Saatlik PTF'lerin düz ortalaması ağırlıklı
# ortalama DEĞİLDİR: gecenin ucuz ve az işlem gören saati ile akşam puantının pahalı ve
# yoğun saati aynı ağırlığa sahip olamaz. Doğru ağırlık, o saatte gün öncesi piyasasında
# eşleşen miktardır:
#
#     PTF_gün = Σ(PTF_saat × Eşleşme_saat) / Σ(Eşleşme_saat)
#
# Uç adı sürümle değişebildiği için birkaç aday sırayla denenir; hiçbiri tutmazsa düz
# ortalamaya düşülür ve bu durum üretilen dosyanın not alanına YAZILIR — sessizce yanlış
# yöntem kullanılmasın.
MATCHING_QUANTITY_PATHS = (
    "/electricity-service/v1/markets/dam/data/matching-quantity",
    "/electricity-service/v1/markets/dam/data/dam-volume",
    "/electricity-service/v1/markets/dam/data/day-ahead-market-trade-volume",
    "/electricity-service/v1/markets/dam/data/trade-volume",
)

TIMEZONE_SUFFIX = "+03:00"

# EPİAŞ yanıtlarındaki alan adları sürümle değişebiliyor. Tek yerden, sırayla deneniyor:
# ilk eşleşen kullanılır. --dry-run çıktısına bakıp buraya ekleme yapmak yeterli.
LIST_FIELD_CANDIDATES = ("items", "body", "content", "data")
DATE_FIELD_CANDIDATES = ("date", "effectiveDate", "period", "time", "dateTime")
PRICE_FIELD_CANDIDATES = ("price", "mcp", "priceTl", "unitCost", "cost", "amount", "value")
QUANTITY_FIELD_CANDIDATES = (
    "matchingQuantity", "quantity", "volume", "tradeVolume", "amount", "value",
)


class EpiasError(RuntimeError):
    pass


def login(username: str, password: str) -> str:
    """CAS'tan TGT alır. Ticket yanıt gövdesinde ya da Location başlığında gelir."""
    body = urllib.parse.urlencode({"username": username, "password": password}).encode()
    request = urllib.request.Request(
        CAS_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/plain",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            location = response.headers.get("Location", "")
            payload = response.read().decode("utf-8", errors="replace").strip()
    except urllib.error.HTTPError as exc:
        raise EpiasError(
            f"EPİAŞ girişi başarısız (HTTP {exc.code}). Kullanıcı adı/parola doğru mu?"
        ) from exc

    for candidate in (payload, location.rsplit("/", 1)[-1] if location else ""):
        if candidate.startswith("TGT-"):
            return candidate
    raise EpiasError(f"TGT bulunamadı. Yanıt: {payload[:200]!r} Location: {location!r}")


def fetch(path: str, tgt: str, start: date, end: date, raw: bool = False):
    payload = json.dumps(
        {
            "startDate": f"{start.isoformat()}T00:00:00{TIMEZONE_SUFFIX}",
            "endDate": f"{end.isoformat()}T00:00:00{TIMEZONE_SUFFIX}",
        }
    ).encode()
    request = urllib.request.Request(
        BASE_URL + path,
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json", "TGT": tgt},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            document = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise EpiasError(f"{path} çağrısı başarısız (HTTP {exc.code}): {detail}") from exc

    if raw:
        return document
    return extract_rows(document, path)


def extract_rows(document, path: str) -> list[dict]:
    """Yanıttaki satır listesini bulur; yapı sürümden sürüme değişebildiği için esnek."""
    if isinstance(document, list):
        return document
    for field in LIST_FIELD_CANDIDATES:
        value = document.get(field) if isinstance(document, dict) else None
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for inner in value.values():
                if isinstance(inner, list):
                    return inner
    raise EpiasError(
        f"{path} yanıtında satır listesi bulunamadı. Alanlar: {list(document)[:10]}"
    )


def pick(row: dict, candidates: tuple[str, ...]):
    for field in candidates:
        if field in row and row[field] is not None:
            return row[field]
    return None


def to_day(value) -> str | None:
    """'2026-08-01T00:00:00+03:00' → '2026-08-01'."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    return value[:10]


def to_hour_key(value) -> str | None:
    """'2026-08-01T14:00:00+03:00' → '2026-08-01T14' (saatlik eşleştirme anahtarı)."""
    if not isinstance(value, str) or len(value) < 13:
        return None
    return value[:13]


def hourly_quantities(rows: list[dict]) -> dict[str, Decimal]:
    """Saat anahtarı → o saatte eşleşen miktar."""
    sonuc: dict[str, Decimal] = {}
    for row in rows:
        saat = to_hour_key(pick(row, DATE_FIELD_CANDIDATES))
        miktar = pick(row, QUANTITY_FIELD_CANDIDATES)
        if saat is None or miktar is None:
            continue
        try:
            sonuc[saat] = Decimal(str(miktar))
        except Exception:  # noqa: BLE001 - bozuk satırı atla
            continue
    return sonuc


def daily_weighted_average(
    rows: list[dict], quantities: dict[str, Decimal]
) -> tuple[dict[str, Decimal], bool]:
    """
    Saatlik PTF'yi güne indirger.

    Eşleşme miktarı verisi varsa AĞIRLIKLI ortalama (mevzuatın istediği), yoksa düz
    ortalama alınır. İkinci dönüş değeri hangisinin kullanıldığını söyler; çağıran bunu
    üretilen dosyaya not olarak yazar.
    """
    agirlikli = bool(quantities)
    paylar: dict[str, Decimal] = {}
    paydalar: dict[str, Decimal] = {}

    for row in rows:
        ham_tarih = pick(row, DATE_FIELD_CANDIDATES)
        gun = to_day(ham_tarih)
        fiyat = pick(row, PRICE_FIELD_CANDIDATES)
        if gun is None or fiyat is None:
            continue
        try:
            fiyat = Decimal(str(fiyat))
        except Exception:  # noqa: BLE001
            continue

        agirlik = Decimal(1)
        if agirlikli:
            saat = to_hour_key(ham_tarih)
            agirlik = quantities.get(saat, Decimal(0)) if saat else Decimal(0)
            # O saatin miktarı yoksa saati düşürmek yerine ağırlığı 1 saymak, günün
            # tamamını kaybetmekten iyidir; ama bu artık saf ağırlıklı ortalama değil.
            if agirlik <= 0:
                agirlik = Decimal(1)

        paylar[gun] = paylar.get(gun, Decimal(0)) + fiyat * agirlik
        paydalar[gun] = paydalar.get(gun, Decimal(0)) + agirlik

    return (
        {
            gun: (paylar[gun] / paydalar[gun]) if paydalar[gun] > 0 else Decimal(0)
            for gun in paylar
        },
        agirlikli,
    )


def to_points(values: dict[str, Decimal], divisor: Decimal, scale: str) -> list[dict]:
    """MWh fiyatını kWh'e çevirip (bölen 1000) noktalara dönüştürür."""
    points = []
    for day in sorted(values):
        price = (values[day] / divisor).quantize(Decimal(scale), rounding=ROUND_HALF_UP)
        points.append({"date": day, "price": f"{price}"})
    return points


def monthly_yekdem(values: dict[str, Decimal]) -> list[dict]:
    """
    Günlük/aylık gelen YEKDEM birim maliyetini TAKVİM AYI kayıtlarına indirger.

    Mevzuat YEKDEM'i ay bazında tanımlar (Tebliğ md. 6/6): fatura dönemi bir aya kaç gün
    düşüyorsa o ayın bedeli o ağırlıkla girer. Bu yüzden ayna da ay bazında yazar; günlük
    bir seri olarak yazmak, motorun ağırlıklandırmasını bozardı.

    EPİAŞ'ın açıkladığı bu değerler KESİNLEŞMİŞ GERÇEKLEŞEN bedeldir; 1 Nolu Açıklama
    md. 13 uyarınca Kurul'un öngördüğü tahmini bedeli ezer. Bu yüzden actual=true.
    """
    buckets: dict[tuple[int, int], list[Decimal]] = {}
    for day, value in values.items():
        yil, ay = int(day[:4]), int(day[5:7])
        buckets.setdefault((yil, ay), []).append(value)
    kayitlar = []
    for (yil, ay) in sorted(buckets):
        degerler = buckets[(yil, ay)]
        ortalama = (sum(degerler) / Decimal(len(degerler))).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        kayitlar.append(
            {
                "year": yil,
                "month": ay,
                "unitCostPerMwh": f"{ortalama}",
                "actual": True,
                "source": "EPİAŞ Şeffaflık Platformu · YEKDEM birim maliyeti (gerçekleşen)",
            }
        )
    return kayitlar


def build_catalog(
    mcp_points: list[dict],
    yekdem_months: list[dict],
    generated_at: str,
    weighted: bool = False,
) -> dict:
    series = []
    if mcp_points:
        yontem = (
            "Saatlik PTF, o saatte gün öncesi piyasasında eşleşen miktarla "
            "ağırlıklandırılarak güne indirgendi."
            if weighted
            else "UYARI: eşleşme miktarı verisi alınamadı, saatlik PTF'lerin DÜZ ortalaması "
            "alındı. Mevzuat ağırlıklı ortalama ister; bu değer yaklaşıktır."
        )
        series.append(
            {
                "id": "epias-ptf-gunluk",
                "name": "PTF (günlük ağırlıklı ortalama)",
                "unit": "TL/kWh",
                "points": mcp_points,
                "source": "EPİAŞ Şeffaflık Platformu · gün öncesi piyasası takas fiyatı (MCP)",
                "placeholder": not weighted,
                "note": yontem + " MWh fiyatı kWh'e çevrildi. Son kaynak hesabında bu "
                "günlük değerlerin ARİTMETİK ortalaması alınır (1 Nolu Açıklama md. 12).",
            }
        )
    return {
        "schemaVersion": 1,
        "title": "EPİAŞ piyasa verisi",
        "generatedAt": generated_at,
        "disclaimer": "Bu dosya EPİAŞ Şeffaflık Platformu'ndan otomatik çekilen kamuya açık "
        "piyasa verisidir. Uygulama yalnızca bu dosyayı indirir; EPİAŞ'a kullanıcı verisi "
        "gönderilmez.",
        "priceSeries": series,
        "yekdemUnitCosts": yekdem_months,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="EPİAŞ PTF/YEKDEM aynası")
    parser.add_argument("--days", type=int, default=60, help="Kaç günlük geçmiş çekilsin")
    parser.add_argument("--out", default="data/piyasa/ptf-yekdem.json")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Ham yanıtı yazdırır; alan adlarını doğrulamak için ilk çalıştırmada kullan.",
    )
    args = parser.parse_args()

    # Kimlik önce ortam değişkeninden (GitHub Actions böyle çalışır), yoksa ekrandan
    # sorulur. Parolayı batch dosyasında okumak kırılgandı (for /f PowerShell'in çıktısını
    # yakalayınca soru ekrana hiç basılmıyordu); buraya alındı. getpass parolayı ekranda
    # GÖSTERMEZ ve hiçbir yere kaydetmez.
    username = os.environ.get("EPIAS_USERNAME")
    password = os.environ.get("EPIAS_PASSWORD")

    if not username or not password:
        if not sys.stdin.isatty():
            print(
                "EPIAS_USERNAME ve EPIAS_PASSWORD ortam degiskenleri gerekli.",
                file=sys.stderr,
            )
            return 2
        print("EPIAS Seffaflik Platformu girisi")
        print("(parola yazarken ekranda gorunmez, hicbir yere kaydedilmez)")
        print()
        if not username:
            username = input("  Kullanici adi (e-posta): ").strip()
        if not password:
            password = getpass.getpass("  Parola: ")
        print()
        if not username or not password:
            print("Kullanici adi ve parola bos olamaz.", file=sys.stderr)
            return 2

    end = date.today() + timedelta(days=1)
    start = end - timedelta(days=args.days)

    try:
        tgt = login(username, password)
    except EpiasError as hata:
        print()
        print("GIRIS BASARISIZ: " + str(hata), file=sys.stderr)
        print(
            "Not: EPIAS Seffaflik Platformu hesabi ile giris yapilir. Ayni parolayla "
            "tarayicidan seffaflik.epias.com.tr adresine girebiliyor musun? Giremiyorsan "
            "hesap henuz onaylanmamis olabilir.",
            file=sys.stderr,
        )
        return 3

    if args.dry_run:
        # Amaç ham JSON'u kusmak değil, tek bakışta "çalışıyor mu, hangi alanlar geldi,
        # tahminlerimiz tuttu mu" sorusunu cevaplamak.
        print("Giris basarili - TGT alindi.")
        print()
        tamam = True
        for baslik, path in (
            ("PTF (gun oncesi piyasasi takas fiyati)", MCP_PATH),
            ("YEKDEM birim maliyeti", YEKDEM_UNIT_COST_PATH),
        ):
            print("=== " + baslik + " ===")
            print("    uc: " + path)
            try:
                satirlar = extract_rows(fetch(path, tgt, start, end, raw=True), path)
            except Exception as hata:  # noqa: BLE001 - teshis ciktisi
                print("    HATA: " + str(hata))
                print()
                tamam = False
                continue

            print("    gelen kayit: " + str(len(satirlar)))
            if not satirlar:
                print("    UYARI: kayit yok - tarih araligi ya da uc adi degismis olabilir.")
                print()
                tamam = False
                continue

            ornek = satirlar[0]
            print("    alanlar: " + ", ".join(ornek.keys()))
            tarih_alani = next((a for a in DATE_FIELD_CANDIDATES if a in ornek), None)
            deger_alani = next((a for a in PRICE_FIELD_CANDIDATES if a in ornek), None)
            print("    tarih alani : " + (tarih_alani or "BULUNAMADI"))
            print("    deger alani : " + (deger_alani or "BULUNAMADI"))
            if tarih_alani and deger_alani:
                print("    ornek       : {} -> {}".format(ornek[tarih_alani], ornek[deger_alani]))
            else:
                print("    UYARI: alan adlari degismis. Betikteki *_FIELD_CANDIDATES "
                      "listelerine yukaridaki alan adini ekle.")
                tamam = False
            print()

        print("=== GOP eslesme miktari (gunluk PTF'nin agirligi) ===")
        bulundu = None
        for path in MATCHING_QUANTITY_PATHS:
            try:
                satirlar = extract_rows(fetch(path, tgt, start, end, raw=True), path)
            except Exception as hata:  # noqa: BLE001
                print("    " + path + " -> " + str(hata)[:70])
                continue
            if satirlar:
                bulundu = path
                ornek = satirlar[0]
                print("    BULUNDU: " + path)
                print("    alanlar: " + ", ".join(ornek.keys()))
                miktar_alani = next(
                    (a for a in QUANTITY_FIELD_CANDIDATES if a in ornek), None
                )
                print("    miktar alani: " + (miktar_alani or "BULUNAMADI"))
                if not miktar_alani:
                    tamam = False
                break
        if bulundu is None:
            print("    HICBIRI TUTMADI - duz ortalamaya dusulecek (mevzuat agirlikli ister).")
            tamam = False
        print()

        print("SONUC: " + ("her sey yerinde, ayna calismaya hazir."
                           if tamam else "eksik var - yukaridaki uyarilara bak."))
        return 0 if tamam else 1

    mcp_rows = fetch(MCP_PATH, tgt, start, end)
    yekdem_rows = fetch(YEKDEM_UNIT_COST_PATH, tgt, start, end)

    # Ağırlık için eşleşme miktarı; uç adı sürümle değişebildiği için sırayla denenir.
    miktarlar: dict[str, Decimal] = {}
    for path in MATCHING_QUANTITY_PATHS:
        try:
            satirlar = fetch(path, tgt, start, end)
        except Exception:  # noqa: BLE001 - uç yoksa sıradakine geç
            continue
        miktarlar = hourly_quantities(satirlar)
        if miktarlar:
            print("Eşleşme miktarı ucu: " + path)
            break

    gunluk, agirlikli = daily_weighted_average(mcp_rows, miktarlar)
    if not agirlikli:
        print(
            "UYARI: eşleşme miktarı alınamadı, düz ortalama kullanıldı. "
            "--dry-run çıktısındaki uç adlarına bakıp MATCHING_QUANTITY_PATHS listesini güncelle.",
            file=sys.stderr,
        )

    # PTF MWh başına TL olarak yayımlanır; uygulama kWh ile çalışıyor.
    mcp_points = to_points(gunluk, Decimal(1000), "0.000001")

    yekdem_values: dict[str, Decimal] = {}
    for row in yekdem_rows:
        day = to_day(pick(row, DATE_FIELD_CANDIDATES))
        price = pick(row, PRICE_FIELD_CANDIDATES)
        if day and price is not None:
            yekdem_values[day] = Decimal(str(price))
    # YEKDEM ay bazında saklanır (TL/MWh olarak, mevzuatın yayımladığı birim).
    yekdem_months = monthly_yekdem(yekdem_values)

    document = build_catalog(
        mcp_points,
        yekdem_months,
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        weighted=agirlikli,
    )

    out_path = args.out
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    print(
        f"Yazıldı: {out_path} · PTF {len(mcp_points)} gün "
        f"({'ağırlıklı' if agirlikli else 'DÜZ'} ortalama) · YEKDEM {len(yekdem_months)} ay"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except EpiasError as error:
        print(f"HATA: {error}", file=sys.stderr)
        sys.exit(1)
