"""Utilitas normalisasi satuan luasan tanah ke dalam Meter Persegi (m²)."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Union

NumericInput = Union[int, float, Decimal, str, None]

# Faktor konversi ke Meter Persegi (m²) sesuai standar pertanahan Indonesia (Jabodetabek/Jawa Barat)
AREA_UNIT_FACTORS_TO_M2: dict[str, Decimal] = {
    "m2": Decimal("1"),
    "m²": Decimal("1"),
    "meter_persegi": Decimal("1"),
    "meter persegi": Decimal("1"),
    "sqm": Decimal("1"),
    "ha": Decimal("10000"),
    "hektar": Decimal("10000"),
    "hectare": Decimal("10000"),
    "hectares": Decimal("10000"),
    "are": Decimal("100"),
    "a": Decimal("100"),
    "bata": Decimal("14"),
    "tumbak": Decimal("14"),
    "ru": Decimal("14"),
}


def normalize_to_m2(
    value: NumericInput,
    unit: str | None = "m2",
) -> Decimal | None:
    """Mengonversi nilai luasan dari satuan asal (Ha, Are, Bata/Tumbak, m²) menjadi m².

    Mengembalikan ``None`` jika ``value`` bernilai ``None``.
    """
    if value is None:
        return None

    try:
        numeric_value = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Nilai luasan tidak valid: {value!r}") from exc

    if numeric_value < 0:
        raise ValueError(f"Nilai luasan tidak boleh negatif: {numeric_value}")

    normalized_unit = (unit or "m2").strip().lower()
    factor = AREA_UNIT_FACTORS_TO_M2.get(normalized_unit)
    if factor is None:
        supported = ", ".join(sorted(AREA_UNIT_FACTORS_TO_M2.keys()))
        raise ValueError(
            f"Satuan luasan tidak dikenali: {unit!r}. Satuan didukung: {supported}"
        )

    return (numeric_value * factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
