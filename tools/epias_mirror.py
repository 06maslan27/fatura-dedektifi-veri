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
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

CAS_URL = "https://giris.epias.com.tr/cas/v1/tickets"
BASE_URL = "https://seffaflik.epias.com.tr"

# Uçlar 401 (var, kimlik gerekiyor) / 404 (yok) farkıyla doğrulandı.
MCP_PATH = "/electricity-service/v1/markets/dam/data/mcp"            # PTF, saatlik
YEKDEM_UNIT_COST_PATH = "/electricity-service/v1/renewables/data/unit-cost"  # YEKDEM birim maliyet

TIMEZONE_SUFFIX = "+03:00"

# EPİAŞ yanıtlarındaki alan adları sürümle değişebiliyor. Tek yerden, sırayla deneniyor:
# ilk eşleşen kullanılır. --dry-run çıktısına bakıp buraya ekleme yapmak yeterli.
LIST_FIELD_CANDIDATES = ("items", "body", "content", "data")
DATE_FIELD_CANDIDATES = ("date", "effectiveDate", "period", "time", "dateTime")
PRICE_FIELD_CANDIDATES = ("price", "mcp", "priceTl", "unitCost", "cost", "amount", "value")


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


def daily_average(rows: list[dict]) -> dict[str, Decimal]:
    """
    Saatlik PTF'yi güne indirger (basit ortalama).

    NOT: En doğrusu tüketim ağırlıklı ortalamadır; ama evde saatlik tüketim verisi yok.
    Basit ortalama, günlük endeksli sözleşmeler için kabul edilebilir bir yaklaşımdır ve
    uygulamada "günlük ortalama PTF" olarak açıkça etiketlenir.
    """
    buckets: dict[str, list[Decimal]] = {}
    for row in rows:
        day = to_day(pick(row, DATE_FIELD_CANDIDATES))
        price = pick(row, PRICE_FIELD_CANDIDATES)
        if day is None or price is None:
            continue
        buckets.setdefault(day, []).append(Decimal(str(price)))
    return {
        day: (sum(values) / Decimal(len(values))) if values else Decimal(0)
        for day, values in buckets.items()
    }


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
    mcp_points: list[dict], yekdem_months: list[dict], generated_at: str
) -> dict:
    series = []
    if mcp_points:
        series.append(
            {
                "id": "epias-ptf-gunluk",
                "name": "PTF (günlük ağırlıklı ortalama)",
                "unit": "TL/kWh",
                "points": mcp_points,
                "source": "EPİAŞ Şeffaflık Platformu · gün öncesi piyasası takas fiyatı (MCP)",
                "note": "Günlük PTF; MWh fiyatı kWh'e çevrildi. Son kaynak hesabında bu "
                "günlerin ARİTMETİK ortalaması alınır (1 Nolu Açıklama md. 12).",
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

    username = os.environ.get("EPIAS_USERNAME")
    password = os.environ.get("EPIAS_PASSWORD")
    if not username or not password:
        print(
            "EPIAS_USERNAME ve EPIAS_PASSWORD ortam değişkenleri gerekli.\n"
            "EPİAŞ Şeffaflık Platformu'na kayıt olup kendi hesabını kullan.",
            file=sys.stderr,
        )
        return 2

    end = date.today() + timedelta(days=1)
    start = end - timedelta(days=args.days)

    tgt = login(username, password)

    if args.dry_run:
        for path in (MCP_PATH, YEKDEM_UNIT_COST_PATH):
            print(f"\n=== {path} ===")
            document = fetch(path, tgt, start, end, raw=True)
            print(json.dumps(document, ensure_ascii=False, indent=2)[:2000])
        return 0

    mcp_rows = fetch(MCP_PATH, tgt, start, end)
    yekdem_rows = fetch(YEKDEM_UNIT_COST_PATH, tgt, start, end)

    # PTF MWh başına TL olarak yayımlanır; uygulama kWh ile çalışıyor.
    mcp_points = to_points(daily_average(mcp_rows), Decimal(1000), "0.000001")

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
    )

    out_path = args.out
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    print(
        f"Yazıldı: {out_path} · PTF {len(mcp_points)} gün · YEKDEM {len(yekdem_months)} ay"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except EpiasError as error:
        print(f"HATA: {error}", file=sys.stderr)
        sys.exit(1)
