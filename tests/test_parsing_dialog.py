from datetime import date

import pytest

from avia.parsing import (
    CITIES,
    ParseError,
    UnknownCityError,
    city_name,
    parse_date,
    parse_passengers,
    parse_query,
    parse_return_date,
    suggest_cities,
    to_iata,
)

TODAY = date(2026, 10, 27)


def test_suggest_cities():
    assert suggest_cities("алмати") == ["Алматы"]
    assert suggest_cities("Алмати") == ["Алматы"]
    assert suggest_cities("  алмати  ") == ["Алматы"]
    assert suggest_cities("алма-аты") == ["Алматы"]
    assert suggest_cities("питер") == ["Санкт-Петербург"]
    assert suggest_cities("спб") == ["Санкт-Петербург"]
    assert suggest_cities("санкт петербург") == ["Санкт-Петербург"]
    assert suggest_cities("неведомыйгород123") == []
    assert suggest_cities("") == []


def test_suggest_cities_limit_and_no_code_duplicates():
    # Проверяем, что нет дублей по коду, и лимит соблюдается
    res = suggest_cities("самар", limit=2)
    assert len(res) == 2
    assert "Самара" in res
    assert "Самарканд" in res

    one = suggest_cities("самар", limit=1)
    assert len(one) == 1

    zero = suggest_cities("самар", limit=0)
    assert zero == []


def test_yo_normalization():
    assert suggest_cities("ёкатеринбург") == ["Екатеринбург"]
    assert to_iata("ёкатеринбург") == "SVX"
    assert to_iata("Ёкатеринбург") == "SVX"


def test_unknown_city_error_with_suggestions():
    with pytest.raises(UnknownCityError) as exc_info:
        to_iata("алмати")
    err = exc_info.value
    assert isinstance(err, ParseError)
    assert err.suggestions == ["Алматы"]
    assert "не знаю город «алмати». Укажите код IATA, например ALA" in str(err)
    assert "Возможно, вы имели в виду: Алматы?" in str(err)


def test_unknown_city_error_without_suggestions():
    with pytest.raises(UnknownCityError) as exc_info:
        to_iata("неведомыйгород123")
    err = exc_info.value
    assert isinstance(err, ParseError)
    assert err.suggestions == []
    assert str(err) == "не знаю город «неведомыйгород123». Укажите код IATA, например ALA"
    assert "Возможно, вы имели в виду" not in str(err)


def test_city_name():
    assert city_name("ALA") == "Алматы"
    assert city_name("ala") == "Алматы"
    assert city_name("MOW") == "Москва"
    assert city_name("LED") == "Санкт-Петербург"
    assert city_name("NQZ") == "Астана"
    assert city_name("ROV") == "Ростов"
    assert city_name("GOJ") == "Нижний Новгород"
    # Для неизвестного кода возвращается сам код
    assert city_name("XYZ") == "XYZ"
    assert city_name("unknown_code") == "unknown_code"


def test_to_iata_normalization_and_new_cities():
    # strip, регистр, лишние пробелы, «Санкт Петербург» без дефиса
    assert to_iata("  Москва  ") == "MOW"
    assert to_iata("мОсКвА") == "MOW"
    assert to_iata("Санкт Петербург") == "LED"
    assert to_iata("санкт   петербург") == "LED"
    assert to_iata("санкт-петербург") == "LED"

    # Новые города
    assert to_iata("ростов") == "ROV"
    assert to_iata("краснодар") == "KRR"
    assert to_iata("нижний новгород") == "GOJ"
    assert to_iata("владивосток") == "VVO"
    assert to_iata("калининград") == "KGD"
    assert to_iata("нур-султан") == "NQZ"
    assert to_iata("алма-ата") == "ALA"
    assert to_iata("шымкент") == "CIT"
    assert to_iata("ош") == "OSS"
    assert to_iata("нукус") == "NCU"

    # IATA 3 латинские буквы
    assert to_iata("MOW") == "MOW"
    assert to_iata("ala") == "ALA"
    assert to_iata("tas") == "TAS"


def test_parse_passengers():
    assert parse_passengers("1") == 1
    assert parse_passengers("9") == 9
    assert parse_passengers(" 5 ") == 5

    for bad in ["0", "10", "-1", "abc", "", " "]:
        with pytest.raises(ParseError, match="число пассажиров: от 1 до 9"):
            parse_passengers(bad)


def test_parse_return_date():
    depart = date(2026, 12, 20)

    # Раньше вылета -> ParseError
    with pytest.raises(ParseError, match="обратная дата раньше даты вылета"):
        parse_return_date("19.12.2026", depart=depart, today=TODAY)
    with pytest.raises(ParseError, match="обратная дата раньше даты вылета"):
        parse_return_date("2026-12-19", depart=depart, today=TODAY)
    with pytest.raises(ParseError, match="обратная дата раньше даты вылета"):
        parse_return_date("19.12.26", depart=depart, today=TODAY)

    # Равная вылету допустима
    assert parse_return_date("20.12.2026", depart=depart, today=TODAY) == depart
    assert parse_return_date("20.12", depart=depart, today=TODAY) == depart
    assert parse_return_date("20/12", depart=depart, today=TODAY) == depart

    # Без года: дата не раньше depart (в том же году)
    assert parse_return_date("25.12", depart=depart, today=TODAY) == date(2026, 12, 25)

    # Без года: 05.01 при вылете 20.12 — следующий год
    assert parse_return_date("05.01", depart=depart, today=TODAY) == date(2027, 1, 5)

    # Поддержка date-объекта
    assert parse_return_date(date(2026, 12, 25), depart=depart, today=TODAY) == date(2026, 12, 25)
    with pytest.raises(ParseError, match="обратная дата раньше даты вылета"):
        parse_return_date(date(2026, 12, 19), depart=depart, today=TODAY)


def test_parse_date_past_dates():
    for past_str in ["20.10.2026", "2026-10-20", "20.10.26", "20/10/26", "20/10/2026"]:
        with pytest.raises(ParseError, match="эта дата уже в прошлом, введите будущую дату"):
            parse_date(past_str, today=TODAY)


def test_parse_date_regression_formats():
    assert parse_date("12.12", today=TODAY) == date(2026, 12, 12)
    assert parse_date("12.12.2026", today=TODAY) == date(2026, 12, 12)
    assert parse_date("2026-12-12", today=TODAY) == date(2026, 12, 12)
    assert parse_date("12/12", today=TODAY) == date(2026, 12, 12)
    assert parse_date("12.12.26", today=TODAY) == date(2026, 12, 12)
    assert parse_date("12/12/26", today=TODAY) == date(2026, 12, 12)
    assert parse_date("12/12/2026", today=TODAY) == date(2026, 12, 12)
    assert parse_date("05.01", today=TODAY) == date(2027, 1, 5)

    # Несуществующая дата
    with pytest.raises(ParseError, match="такой даты нет: 31.02"):
        parse_date("31.02", today=TODAY)

    # Непонятный формат
    with pytest.raises(ParseError, match="не понял дату «непонятно»"):
        parse_date("непонятно", today=TODAY)


def test_parse_query_command_still_works():
    q = parse_query("Москва Алматы 10.12 2", TODAY)
    assert (q.origin, q.destination, q.depart, q.adults) == ("MOW", "ALA", date(2026, 12, 10), 2)
