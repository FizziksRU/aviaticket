from __future__ import annotations

import dataclasses
import pytest

from avia.criteria import (
    ALLOWED_DAY_PARTS,
    ALLOWED_MAX_TRANSFERS,
    DAY_PART_DESCRIPTIONS,
    DAY_PART_NAMES,
    DAY_PART_RANGES,
    DAY_PARTS,
    SearchCriteria,
)


def test_default_criteria():
    c = SearchCriteria()
    assert c.is_default() is True
    assert c.max_transfers is None
    assert c.max_duration_hours is None
    assert c.depart_parts == frozenset()
    assert c.arrive_parts == frozenset()
    assert c.hour_cost is None
    assert c.describe() == []


def test_is_default_with_parameters():
    assert SearchCriteria(max_transfers=0).is_default() is False
    assert SearchCriteria(max_duration_hours=12).is_default() is False
    assert SearchCriteria(depart_parts={"morning"}).is_default() is False
    assert SearchCriteria(arrive_parts={"night"}).is_default() is False
    assert SearchCriteria(hour_cost=500).is_default() is False


def test_frozen_immutability():
    c = SearchCriteria()
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.max_transfers = 1  # type: ignore[misc]


def test_describe_transfers():
    assert SearchCriteria(max_transfers=0).describe() == ["пересадки: только прямые"]
    assert SearchCriteria(max_transfers=1).describe() == ["пересадки: не больше 1"]
    assert SearchCriteria(max_transfers=2).describe() == ["пересадки: не больше 2"]


def test_describe_duration():
    assert SearchCriteria(max_duration_hours=15).describe() == ["в пути: до 15 ч"]
    assert SearchCriteria(max_duration_hours=8).describe() == ["в пути: до 8 ч"]


def test_describe_day_parts():
    assert SearchCriteria(depart_parts={"morning"}).describe() == ["вылет: утром"]
    assert SearchCriteria(depart_parts={"morning", "evening"}).describe() == ["вылет: утром или вечером"]
    assert SearchCriteria(depart_parts={"morning", "day", "evening"}).describe() == [
        "вылет: утром, днём или вечером"
    ]
    assert SearchCriteria(arrive_parts={"day"}).describe() == ["прилёт: днём"]
    assert SearchCriteria(arrive_parts={"night"}).describe() == ["прилёт: ночью"]


def test_describe_balance():
    assert SearchCriteria(hour_cost=500).describe() == ["баланс: 500 ₽/ч"]


def test_describe_combined():
    c = SearchCriteria(
        max_transfers=1,
        max_duration_hours=15,
        depart_parts=frozenset({"morning", "evening"}),
        arrive_parts=frozenset({"day"}),
        hour_cost=500,
    )
    desc = c.describe()
    assert desc == [
        "пересадки: не больше 1",
        "в пути: до 15 ч",
        "вылет: утром или вечером",
        "прилёт: днём",
        "баланс: 500 ₽/ч",
    ]


def test_constants():
    assert "morning" in DAY_PARTS
    assert "day" in DAY_PARTS
    assert "evening" in DAY_PARTS
    assert "night" in DAY_PARTS
    assert ALLOWED_DAY_PARTS == frozenset({"morning", "day", "evening", "night"})
    assert 0 in ALLOWED_MAX_TRANSFERS
    assert 1 in ALLOWED_MAX_TRANSFERS
    assert 2 in ALLOWED_MAX_TRANSFERS
    assert DAY_PART_NAMES["morning"] == "утро"
    assert DAY_PART_DESCRIPTIONS["morning"] == "утром"
    assert DAY_PART_RANGES["morning"] == ("06:00", "11:59")
    assert DAY_PART_RANGES["night"] == ("00:00", "05:59")
