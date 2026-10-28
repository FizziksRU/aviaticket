from __future__ import annotations

from dataclasses import dataclass


DAY_PARTS: tuple[str, ...] = ("morning", "day", "evening", "night")

DAY_PART_NAMES: dict[str, str] = {
    "morning": "утро",
    "day": "день",
    "evening": "вечер",
    "night": "ночь",
}

DAY_PART_DESCRIPTIONS: dict[str, str] = {
    "morning": "утром",
    "day": "днём",
    "evening": "вечером",
    "night": "ночью",
}

# (start_inclusive, end_inclusive)
DAY_PART_RANGES: dict[str, tuple[str, str]] = {
    "night": ("00:00", "05:59"),
    "morning": ("06:00", "11:59"),
    "day": ("12:00", "17:59"),
    "evening": ("18:00", "23:59"),
}

ALLOWED_MAX_TRANSFERS: tuple[int, ...] = (0, 1, 2)
ALLOWED_DAY_PARTS: frozenset[str] = frozenset(DAY_PARTS)


@dataclass(frozen=True)
class SearchCriteria:
    max_transfers: int | None = None
    max_duration_hours: int | None = None
    depart_parts: frozenset[str] = frozenset()
    arrive_parts: frozenset[str] = frozenset()
    hour_cost: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.depart_parts, frozenset):
            object.__setattr__(self, "depart_parts", frozenset(self.depart_parts))
        if not isinstance(self.arrive_parts, frozenset):
            object.__setattr__(self, "arrive_parts", frozenset(self.arrive_parts))

    def is_default(self) -> bool:
        return (
            self.max_transfers is None
            and self.max_duration_hours is None
            and not self.depart_parts
            and not self.arrive_parts
            and self.hour_cost is None
        )

    def describe(self) -> list[str]:
        items: list[str] = []

        if self.max_transfers is not None:
            if self.max_transfers == 0:
                items.append("пересадки: только прямые")
            else:
                items.append(f"пересадки: не больше {self.max_transfers}")

        if self.max_duration_hours is not None:
            items.append(f"в пути: до {self.max_duration_hours} ч")

        if self.depart_parts:
            ordered = [p for p in DAY_PARTS if p in self.depart_parts]
            ordered.extend(sorted(p for p in self.depart_parts if p not in DAY_PARTS))
            words = [DAY_PART_DESCRIPTIONS.get(p, p) for p in ordered]
            if len(words) == 1:
                parts_str = words[0]
            else:
                parts_str = ", ".join(words[:-1]) + f" или {words[-1]}"
            items.append(f"вылет: {parts_str}")

        if self.arrive_parts:
            ordered = [p for p in DAY_PARTS if p in self.arrive_parts]
            ordered.extend(sorted(p for p in self.arrive_parts if p not in DAY_PARTS))
            words = [DAY_PART_DESCRIPTIONS.get(p, p) for p in ordered]
            if len(words) == 1:
                parts_str = words[0]
            else:
                parts_str = ", ".join(words[:-1]) + f" или {words[-1]}"
            items.append(f"прилёт: {parts_str}")

        if self.hour_cost is not None:
            items.append(f"баланс: {self.hour_cost} ₽/ч")

        return items
