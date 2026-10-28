from datetime import date

import pytest

import avia.dialog
from avia.criteria import ALLOWED_DAY_PARTS, SearchCriteria
from avia.dialog import (
    Dialog,
    Reply,
    ROUND_TRIP_SITES,
    SearchRequest,
    Session,
    Step,
)
from avia.models import Query

TODAY = date(2026, 10, 27)


def make_dialog() -> Dialog:
    return Dialog(today=lambda: TODAY)


def advance_to_baggage(dialog: Dialog, chat_id: int | str = 201) -> Session:
    dialog.on_callback(chat_id, "menu:go")
    session = dialog.sessions[chat_id]
    d_id = session.dialog_id
    dialog.on_text(chat_id, "Москва")
    dialog.on_text(chat_id, "Алматы")
    dialog.on_text(chat_id, "12.12.2026")
    dialog.on_callback(chat_id, f"trip:{d_id}:one")
    assert session.step == Step.BAGGAGE
    return session


def test_dialog_default_flow_criteria_is_default():
    """Путь по умолчанию (маршрут->даты->багаж) без захода в настройки даёт SearchRequest с criteria.is_default()."""
    dialog = make_dialog()
    chat_id = 201
    session = advance_to_baggage(dialog, chat_id)
    d_id = session.dialog_id

    # Проверяем кнопку «⚙ Расширенные настройки» без счетчика (N)
    rep = dialog._baggage_reply(session)
    assert rep.keyboard is not None
    adv_btns = [btn for row in rep.keyboard for btn in row if btn[1] == f"adv:{d_id}:menu"]
    assert len(adv_btns) == 1
    assert adv_btns[0][0] == "⚙ Расширенные настройки"

    # Выбор багажа даёт SearchRequest с критериями по умолчанию
    res = dialog.on_callback(chat_id, f"bag:{d_id}:with")
    assert len(res) == 1
    assert isinstance(res[0], SearchRequest)
    sr = res[0]
    assert sr.criteria.is_default() is True
    assert sr.criteria == SearchCriteria()
    assert sr.baggage_mode == "with"


def test_dialog_advanced_settings_full_cycle():
    """Вход в настройки, выбор всех критериев (включая переключение частей суток туда-сюда), сброс и Готово."""
    dialog = make_dialog()
    chat_id = 202
    session = advance_to_baggage(dialog, chat_id)
    d_id = session.dialog_id

    # 1. Вход в настройки
    rep = dialog.on_callback(chat_id, f"adv:{d_id}:menu")
    assert len(rep) == 1
    assert rep[0].edit is True
    assert "⚙ Расширенные настройки" in rep[0].text
    assert "Всё не важно" in rep[0].text

    # Проверка списка кнопок по одной в ряд
    expected_labels = [
        "Пересадки",
        "Время в пути",
        "Время вылета",
        "Время прилёта",
        "Баланс цены и времени",
        "Сбросить всё",
        "✅ Готово",
        "✖ Отмена",
    ]
    actual_labels = [row[0][0] for row in rep[0].keyboard]
    assert actual_labels == expected_labels

    # 2. Подэкран Пересадки
    rep_tr = dialog.on_callback(chat_id, f"adv:{d_id}:s_tr")
    assert rep_tr[0].edit is True
    assert "⚙ Пересадки" in rep_tr[0].text
    tr_labels = [btn[0] for row in rep_tr[0].keyboard for btn in row]
    assert tr_labels == ["Только прямые", "До 1", "До 2", "Не важно", "⬅ Назад"]

    # Выбор «Только прямые» (0) сразу возвращает на экран настроек
    rep_tr0 = dialog.on_callback(chat_id, f"adv:{d_id}:tr_0")
    assert rep_tr0[0].edit is True
    assert "⚙ Расширенные настройки" in rep_tr0[0].text
    assert "пересадки: только прямые" in rep_tr0[0].text
    assert session.criteria.max_transfers == 0

    # Проверка перевыбора: «До 1», «До 2», «Не важно»
    dialog.on_callback(chat_id, f"adv:{d_id}:s_tr")
    dialog.on_callback(chat_id, f"adv:{d_id}:tr_1")
    assert session.criteria.max_transfers == 1
    dialog.on_callback(chat_id, f"adv:{d_id}:s_tr")
    dialog.on_callback(chat_id, f"adv:{d_id}:tr_any")
    assert session.criteria.max_transfers is None

    # Возврат к «Только прямые»
    dialog.on_callback(chat_id, f"adv:{d_id}:s_tr")
    dialog.on_callback(chat_id, f"adv:{d_id}:tr_0")
    assert session.criteria.max_transfers == 0

    # 3. Подэкран Время в пути
    rep_dur = dialog.on_callback(chat_id, f"adv:{d_id}:s_dur")
    assert rep_dur[0].edit is True
    assert "⚙ Время в пути" in rep_dur[0].text
    dur_labels = [btn[0] for row in rep_dur[0].keyboard for btn in row]
    assert dur_labels == ["До 8 ч", "До 15 ч", "До 24 ч", "Не важно", "⬅ Назад"]

    # Выбор «До 15 ч» сразу возвращает на экран настроек
    rep_dur15 = dialog.on_callback(chat_id, f"adv:{d_id}:dur_15")
    assert rep_dur15[0].edit is True
    assert "в пути: до 15 ч" in rep_dur15[0].text
    assert session.criteria.max_duration_hours == 15

    # 4. Подэкран Время вылета (переключение туда-сюда)
    rep_dp = dialog.on_callback(chat_id, f"adv:{d_id}:s_dp")
    assert rep_dp[0].edit is True
    assert "⚙ Время вылета" in rep_dp[0].text

    # Выбор morning -> остаётся на подэкране, появляется галочка
    rep_m = dialog.on_callback(chat_id, f"adv:{d_id}:dp_morning")
    assert rep_m[0].edit is True
    assert "⚙ Время вылета" in rep_m[0].text
    dp_labels_m = [btn[0] for row in rep_m[0].keyboard for btn in row]
    assert any("✓" in lbl and "утро" in lbl for lbl in dp_labels_m)
    assert session.criteria.depart_parts == frozenset({"morning"})

    # Добавляем evening
    rep_e = dialog.on_callback(chat_id, f"adv:{d_id}:dp_evening")
    dp_labels_e = [btn[0] for row in rep_e[0].keyboard for btn in row]
    assert any("✓" in lbl and "утро" in lbl for lbl in dp_labels_e)
    assert any("✓" in lbl and "вечер" in lbl for lbl in dp_labels_e)
    assert session.criteria.depart_parts == frozenset({"morning", "evening"})

    # Повторное нажатие morning снимает выбор
    rep_m_off = dialog.on_callback(chat_id, f"adv:{d_id}:dp_morning")
    dp_labels_off = [btn[0] for row in rep_m_off[0].keyboard for btn in row]
    assert not any("✓" in lbl and "утро" in lbl for lbl in dp_labels_off)
    assert any("✓" in lbl and "вечер" in lbl for lbl in dp_labels_off)
    assert session.criteria.depart_parts == frozenset({"evening"})

    # «Не важно» очищает выбор
    rep_any = dialog.on_callback(chat_id, f"adv:{d_id}:dp_any")
    dp_labels_any = [btn[0] for row in rep_any[0].keyboard for btn in row]
    assert not any("✓" in lbl for lbl in dp_labels_any)
    assert session.criteria.depart_parts == frozenset()

    # Снова ставим morning и возвращаемся кнопкой «⬅ Назад»
    dialog.on_callback(chat_id, f"adv:{d_id}:dp_morning")
    rep_back = dialog.on_callback(chat_id, f"adv:{d_id}:menu")
    assert rep_back[0].edit is True
    assert "⚙ Расширенные настройки" in rep_back[0].text
    assert "вылет: утром" in rep_back[0].text

    # 5. Подэкран Время прилёта
    dialog.on_callback(chat_id, f"adv:{d_id}:s_ap")
    dialog.on_callback(chat_id, f"adv:{d_id}:ap_day")
    dialog.on_callback(chat_id, f"adv:{d_id}:menu")
    assert "прилёт: днём" in dialog._adv_menu_reply(session).text
    assert session.criteria.arrive_parts == frozenset({"day"})

    # 6. Подэкран Баланс цены и времени
    rep_hc = dialog.on_callback(chat_id, f"adv:{d_id}:s_hc")
    assert rep_hc[0].edit is True
    assert "⚙ Баланс цены и времени" in rep_hc[0].text
    assert "стоимость часа в пути" in rep_hc[0].text
    assert "Лучший баланс" in rep_hc[0].text
    hc_labels = [btn[0] for row in rep_hc[0].keyboard for btn in row]
    assert hc_labels == ["500 ₽/ч", "1000 ₽/ч", "2000 ₽/ч", "Выключить", "⬅ Назад"]

    # Выбор 500 ₽/ч сразу возвращает на экран настроек
    rep_hc500 = dialog.on_callback(chat_id, f"adv:{d_id}:hc_500")
    assert rep_hc500[0].edit is True
    assert "баланс: 500 ₽/ч" in rep_hc500[0].text
    assert session.criteria.hour_cost == 500

    # Все 5 критериев установлены
    assert len(session.criteria.describe()) == 5

    # 7. «Сбросить всё»
    rep_reset = dialog.on_callback(chat_id, f"adv:{d_id}:reset")
    assert rep_reset[0].edit is True
    assert "Всё не важно" in rep_reset[0].text
    assert session.criteria.is_default() is True

    # 8. Зададим критерии заново и нажмём «✅ Готово»
    dialog.on_callback(chat_id, f"adv:{d_id}:s_tr")
    dialog.on_callback(chat_id, f"adv:{d_id}:tr_1")  # max_transfers=1
    dialog.on_callback(chat_id, f"adv:{d_id}:s_hc")
    dialog.on_callback(chat_id, f"adv:{d_id}:hc_1000")  # hour_cost=1000

    assert len(session.criteria.describe()) == 2

    # Нажатие «✅ Готово» возвращает на экран багажа с edit=True
    rep_done = dialog.on_callback(chat_id, f"adv:{d_id}:done")
    assert len(rep_done) == 1
    assert rep_done[0].edit is True
    assert "Багаж?" in rep_done[0].text
    adv_btns = [btn for row in rep_done[0].keyboard for btn in row if btn[1] == f"adv:{d_id}:menu"]
    assert len(adv_btns) == 1
    assert adv_btns[0][0] == "⚙ Расширенные настройки (2)"

    # Выбор багажа отдаёт SearchRequest с установленными критериями
    res = dialog.on_callback(chat_id, f"bag:{d_id}:without")
    assert len(res) == 1
    assert isinstance(res[0], SearchRequest)
    sr = res[0]
    assert sr.baggage_mode == "without"
    assert sr.criteria.max_transfers == 1
    assert sr.criteria.hour_cost == 1000
    assert sr.criteria.max_duration_hours is None
    assert sr.criteria.depart_parts == frozenset()
    assert sr.criteria.arrive_parts == frozenset()
    assert sr.criteria.is_default() is False


def test_dialog_expired_dialog_id():
    """Устаревший dialog_id при adv-действиях возвращает expired reply."""
    dialog = make_dialog()
    chat_id = 203
    session = advance_to_baggage(dialog, chat_id)

    rep = dialog.on_callback(chat_id, "adv:wrong_id:menu")
    assert len(rep) == 1
    assert rep[0].answer == "Диалог устарел, начните заново"
    assert "Диалог устарел, начните заново" in rep[0].text

    rep_sub = dialog.on_callback(chat_id, "adv:wrong_id:tr_0")
    assert len(rep_sub) == 1
    assert rep_sub[0].answer == "Диалог устарел, начните заново"

    # Несуществующая сессия
    rep_no_sess = dialog.on_callback(999999, "adv:some_id:menu")
    assert len(rep_no_sess) == 1
    assert rep_no_sess[0].answer == "Диалог устарел, начните заново"


def test_dialog_all_adv_callback_data_len_le_64():
    """Все callback_data на всех экранах настроек <= 64 байт."""
    dialog = make_dialog()
    chat_id = 204
    session = advance_to_baggage(dialog, chat_id)
    d_id = session.dialog_id

    def check_kb(kb):
        assert kb is not None
        for row in kb:
            for label, cb in row:
                raw = cb.encode("utf-8")
                assert len(raw) <= 64, f"Callback {cb!r} exceeds 64 bytes ({len(raw)})"

    # Экран багажа
    rep_bag = dialog._baggage_reply(session)
    check_kb(rep_bag.keyboard)

    # Экран меню настроек
    rep_menu = dialog.on_callback(chat_id, f"adv:{d_id}:menu")
    check_kb(rep_menu[0].keyboard)

    # Подэкран пересадок
    rep_tr = dialog.on_callback(chat_id, f"adv:{d_id}:s_tr")
    check_kb(rep_tr[0].keyboard)

    # Подэкран времени в пути
    rep_dur = dialog.on_callback(chat_id, f"adv:{d_id}:s_dur")
    check_kb(rep_dur[0].keyboard)

    # Подэкран вылета
    rep_dp = dialog.on_callback(chat_id, f"adv:{d_id}:s_dp")
    check_kb(rep_dp[0].keyboard)

    # Подэкран вылета с выбранными частями
    dialog.on_callback(chat_id, f"adv:{d_id}:dp_morning")
    dialog.on_callback(chat_id, f"adv:{d_id}:dp_night")
    rep_dp_sel = dialog._adv_depart_reply(session)
    check_kb(rep_dp_sel.keyboard)

    # Подэкран прилёта
    rep_ap = dialog.on_callback(chat_id, f"adv:{d_id}:s_ap")
    check_kb(rep_ap[0].keyboard)

    # Подэкран баланса
    rep_hc = dialog.on_callback(chat_id, f"adv:{d_id}:s_hc")
    check_kb(rep_hc[0].keyboard)


def test_dialog_unknown_adv_value_does_not_crash():
    """Неизвестные значения callback_data безопасно возвращают экран настроек без исключений."""
    dialog = make_dialog()
    chat_id = 205
    session = advance_to_baggage(dialog, chat_id)
    d_id = session.dialog_id

    unknown_values = [
        "unknown_action",
        "tr_999",
        "tr_abc",
        "dur_999",
        "dur_abc",
        "dp_midnight",
        "dp_unknown",
        "ap_foo",
        "hc_999",
        "hc_abc",
        "",
        "something:else",
    ]

    for val in unknown_values:
        rep = dialog.on_callback(chat_id, f"adv:{d_id}:{val}")
        assert len(rep) == 1
        assert rep[0].edit is True
        assert "⚙ Расширенные настройки" in rep[0].text


def test_dialog_adv_ignored_on_other_steps():
    """На других шагах adv-кнопки возвращают пустой список (как pax)."""
    dialog = make_dialog()
    chat_id = 206

    # ORIGIN
    dialog.on_callback(chat_id, "menu:go")
    session = dialog.sessions[chat_id]
    d_id = session.dialog_id
    assert session.step == Step.ORIGIN
    assert dialog.on_callback(chat_id, f"adv:{d_id}:menu") == []

    # DEST
    dialog.on_text(chat_id, "MOW")
    assert session.step == Step.DEST
    assert dialog.on_callback(chat_id, f"adv:{d_id}:menu") == []

    # DATE
    dialog.on_text(chat_id, "ALA")
    assert session.step == Step.DATE
    assert dialog.on_callback(chat_id, f"adv:{d_id}:menu") == []

    # TRIP
    dialog.on_text(chat_id, "12.12.2026")
    assert session.step == Step.TRIP
    assert dialog.on_callback(chat_id, f"adv:{d_id}:menu") == []

    # RETURN_DATE
    dialog.on_callback(chat_id, f"trip:{d_id}:rt")
    assert session.step == Step.RETURN_DATE
    assert dialog.on_callback(chat_id, f"adv:{d_id}:menu") == []

    # SEARCHING
    dialog.on_text(chat_id, "20.12.2026")
    assert session.step == Step.BAGGAGE
    dialog.on_callback(chat_id, f"bag:{d_id}:any")
    assert session.step == Step.SEARCHING
    assert dialog.on_callback(chat_id, f"adv:{d_id}:menu") == []


def test_dialog_cancel_on_adv_screen():
    """Отмена на экране настроек сбрасывает сессию и возвращает главное меню."""
    dialog = make_dialog()
    chat_id = 207
    session = advance_to_baggage(dialog, chat_id)
    d_id = session.dialog_id

    dialog.on_callback(chat_id, f"adv:{d_id}:menu")

    # Нажатие Cancel
    rep = dialog.on_callback(chat_id, f"cancel:{d_id}")
    assert dialog.sessions.get(chat_id) is None or dialog.sessions[chat_id].step == Step.MENU
    assert "Главное меню" in rep[0].text

    # Также через adv:cancel
    session2 = advance_to_baggage(dialog, chat_id)
    d_id2 = session2.dialog_id
    dialog.on_callback(chat_id, f"adv:{d_id2}:menu")
    rep2 = dialog.on_callback(chat_id, f"adv:{d_id2}:cancel")
    assert dialog.sessions.get(chat_id) is None or dialog.sessions[chat_id].step == Step.MENU
    assert "Главное меню" in rep2[0].text


def test_dialog_roundtrip_go_preserves_criteria(monkeypatch):
    """При поиске туда-обратно и переходе на «Искать только туда» (go) criteria сохраняются."""
    dialog = make_dialog()
    chat_id = 208

    dialog.on_callback(chat_id, "menu:go")
    session = dialog.sessions[chat_id]
    d_id = session.dialog_id
    dialog.on_text(chat_id, "MOW")
    dialog.on_text(chat_id, "ALA")
    dialog.on_text(chat_id, "12.12.2026")
    dialog.on_callback(chat_id, f"trip:{d_id}:rt")
    dialog.on_text(chat_id, "20.12.2026")
    assert session.step == Step.BAGGAGE

    # Настраиваем пересадки
    dialog.on_callback(chat_id, f"adv:{d_id}:menu")
    dialog.on_callback(chat_id, f"adv:{d_id}:s_tr")
    dialog.on_callback(chat_id, f"adv:{d_id}:tr_0")
    dialog.on_callback(chat_id, f"adv:{d_id}:done")

    # Имитируем отсутствие сайтов с round-trip
    monkeypatch.setattr(avia.dialog, "ROUND_TRIP_SITES", frozenset())
    dialog.on_callback(chat_id, f"bag:{d_id}:with")

    # Нажатие «Искать только туда»
    res = dialog.on_callback(chat_id, f"go:{d_id}")
    assert len(res) == 1
    assert isinstance(res[0], SearchRequest)
    assert res[0].criteria.max_transfers == 0
    assert res[0].criteria.is_default() is False


def test_dialog_price_ignores_criteria():
    """Команда /price не использует расширенные критерии."""
    dialog = make_dialog()
    chat_id = 209

    res = dialog.on_text(chat_id, "/price Москва Алматы 10.12")
    assert len(res) == 1
    assert isinstance(res[0], SearchRequest)
    assert res[0].criteria.is_default() is True
