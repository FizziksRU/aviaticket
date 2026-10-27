"""Тесты для форматирования списков, нарезки отчёта и карточки деталей."""
from datetime import date
from pathlib import Path
import re
import sys

_src = str(Path(__file__).resolve().parent.parent / "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

import avia
_avia_path = str(Path(_src) / "avia")
if _avia_path not in avia.__path__:
    avia.__path__ = [_avia_path] + list(avia.__path__)

for _mod in ["avia.models", "avia.formatting", "avia.offers", "avia.store"]:
    sys.modules.pop(_mod, None)

from avia.formatting import build_report, format_details, format_report
from avia.models import Leg, Offer, Query, SiteResult


def test_through_numbering_across_multiple_sites():
    """Тест сквозной нумерации 1., 2., 3., 4. по нескольким сайтам."""
    q = Query("MOW", "ALA", date(2026, 12, 12))
    offers_s1 = [
        Offer(site="site1", price=10000, airline="Nordwind", depart_time="10:00", arrive_time="14:00"),
        Offer(site="site1", price=11000, airline="Nordwind", depart_time="12:00", arrive_time="16:00"),
    ]
    offers_s2 = [
        Offer(site="site2", price=12000, airline="Scat", depart_time="15:00", arrive_time="19:00"),
        Offer(site="site2", price=13000, airline="Scat", depart_time="18:00", arrive_time="22:00"),
    ]
    results = [
        SiteResult(site="site1", ok=True, offers=offers_s1),
        SiteResult(site="site2", ok=True, offers=offers_s2),
    ]

    parts, flat = build_report(q, results, top_n=5)
    assert len(parts) == 1
    text = parts[0]

    assert "1. " in text
    assert "2. " in text
    assert "3. " in text
    assert "4. " in text

    # Проверка порядка в flat списке
    assert len(flat) == 4
    assert flat[0] == offers_s1[0]
    assert flat[1] == offers_s1[1]
    assert flat[2] == offers_s2[0]
    assert flat[3] == offers_s2[1]

    # Внизу есть подсказка
    assert "Напишите номер билета, чтобы увидеть подробности" in text


def test_chunking_long_report_200_offers():
    """Тест нарезки длинного отчёта из 200 вариантов.

    Проверяется:
      - все части <= 4000 (и <= 4096)
      - теги сбалансированы
      - нумерация непрерывна от 1 до 200
      - заголовок (продолжение) присутствует
    """
    q = Query("MOW", "ALA", date(2026, 12, 12))
    offers = [
        Offer(
            site="supersite",
            price=10000 + i * 10,
            airline=f"Airline {i}",
            depart_time=f"{i % 24:02d}:00",
            arrive_time=f"{(i + 4) % 24:02d}:00",
            duration="4 ч 00 мин",
            transfers=i % 3,
            baggage="с багажом" if i % 2 == 0 else "без багажа",
        )
        for i in range(200)
    ]
    results = [SiteResult(site="supersite", ok=True, offers=offers)]

    parts, flat = build_report(q, results, top_n=200)

    # Должно быть разбито на несколько частей
    assert len(parts) > 1
    assert len(flat) == 200

    # Проверка сквозной нумерации: каждый номер от 1 до 200 встречается ровно один раз
    combined_text = "\n".join(parts)
    for n in range(1, 201):
        pattern = rf"(?:^|\n){n}\.\s+"
        assert re.search(pattern, combined_text), f"Номер {n}. не найден в отчёте"

    # Проверка каждой части: длина <= 4000 (и тем более <= 4096) и баланс тегов
    for idx, part in enumerate(parts):
        assert len(part) <= 4000, f"Часть {idx} превышает 4000 символов: {len(part)}"
        # Проверка баланса тегов <b>, <i>, <a>
        assert part.count("<b>") == part.count("</b>"), f"Несбалансированные <b> в части {idx}"
        assert part.count("<i>") == part.count("</i>"), f"Несбалансированные <i> в части {idx}"
        assert part.count("<a ") == part.count("</a>"), f"Несбалансированные <a> в части {idx}"

        if idx > 0:
            # На продолжении должен повторяться заголовок сайта
            assert "<b>supersite</b> (продолжение)" in part


def test_header_baggage_and_return_date():
    """Тест шапки с обратной датой и фильтрацией багажа."""
    q_two_way = Query("MOW", "ALA", date(2026, 12, 12), return_date=date(2026, 12, 25))
    o_unknown = Offer(site="s", price=15000, baggage=None)
    results = [SiteResult(site="s", ok=True, offers=[o_unknown])]

    text = format_report(q_two_way, results, baggage_mode="with", dropped=5)

    assert "туда 12.12.2026, обратно 25.12.2026" in text
    assert "Багаж: только с багажом, скрыто вариантов: 5" in text
    assert "вариантов без данных о багаже показаны с пометкой" in text
    # В строке варианта с baggage_state None указано «багаж не указан»
    assert "багаж не указан" in text


def test_format_details_one_leg_and_escaping():
    """Тест карточки деталей с одним плечом и экранированием XSS."""
    q = Query("MOW", "ALA", date(2026, 12, 12))
    o = Offer(
        site="<script>bad</script>",
        price=18500,
        currency="RUB",
        airline="<b>EvilAirline</b>",
        depart_time="22:50",
        arrive_time="05:10",
        duration="4ч 20м",
        transfers=0,
        flight_numbers=["<XSS> 101"],
        aircraft="Boeing <737>",
        layovers=["Астана <1>", "Алматы <2>"],
        baggage="с багажом <23кг>",
        carry_on="ручная кладь <10кг>",
        url="https://site.com/buy?id=1&name=<evil>",
        fetched_at=1793094000.0,
    )

    card = format_details(o, q, fetched_at=1793094000.0)

    # Проверка, что сырые HTML-теги экранированы
    assert "<script>" not in card
    assert "&lt;script&gt;bad&lt;/script&gt;" in card
    assert "<b>EvilAirline</b>" not in card
    assert "&lt;b&gt;EvilAirline&lt;/b&gt;" in card
    assert "&lt;XSS&gt; 101" in card
    assert "Boeing &lt;737&gt;" in card
    assert "&lt;23кг&gt;" in card
    assert "&lt;10кг&gt;" in card
    assert "<evil>" not in card

    # Проверка полей
    assert "MOW → ALA" in card
    assert "22:50" in card
    assert "05:10" in card
    assert "В пути: 4ч 20м" in card
    assert "Пересадки: Астана &lt;1&gt;, Алматы &lt;2&gt;" in card
    assert "18 500 ₽" in card
    assert 'href="https://site.com/buy?id=1&amp;name=&lt;evil&gt;"' in card
    assert "Цена снята:" in card


def test_format_details_two_legs():
    """Тест карточки деталей с обратным плечом (o.back)."""
    q = Query("MOW", "ALA", date(2026, 12, 12), return_date=date(2026, 12, 20))
    back_leg = Leg(
        airline="Air Astana",
        flight_numbers=["KC 872"],
        aircraft="A321",
        depart_time="08:00",
        arrive_time="11:30",
        duration="4ч 30м",
        transfers=0,
    )
    o = Offer(
        site="kupibilet",
        price=35000,
        airline="Air Astana",
        flight_numbers=["KC 871"],
        aircraft="A321",
        depart_time="20:00",
        arrive_time="03:00",
        duration="5ч 00м",
        transfers=0,
        back=back_leg,
    )

    card = format_details(o, q)

    assert "✈️ <b>MOW → ALA</b>" in card
    assert "KC 871" in card
    assert "Обратно (ALA → MOW)" in card
    assert "KC 872" in card
    assert "Air Astana" in card
    assert "35 000 ₽" in card
