"""FHIR R4 Observation serializer for SleepDay.

Converts a canonical SleepDay into a FHIR R4 Observation resource
with LOINC-coded components. This is an OUTPUT format — the internal
canonical model remains the source of truth.

Design decisions:
- category: activity (wearable-generated wellness data)
- effective[x]: effectivePeriod when onset/offset exist, else effectiveDateTime
- component[]: one per LOINC-coded metric
- sleep efficiency uses text-only code (no standard LOINC exists)
"""

from typing import Any

from sleep.domain.models import SleepDay

LOINC_SYSTEM = "http://loinc.org"
UCUM_SYSTEM = "http://unitsofmeasure.org"

_SLEEP_LOINC: dict[str, tuple[str, str]] = {
    "total_sleep_minutes": ("93832-4", "Sleep duration"),
    "deep_sleep_minutes": ("93831-6", "Deep sleep duration"),
    "light_sleep_minutes": ("93830-8", "Light sleep duration"),
    "rem_sleep_minutes": ("93829-0", "REM sleep duration"),
}


def sleep_day_to_fhir_observation(sleep_day: SleepDay) -> dict[str, Any]:
    """Convert a canonical SleepDay to a FHIR R4 Observation resource."""
    observation: dict[str, Any] = {
        "resourceType": "Observation",
        "id": str(sleep_day.id),
        "status": "final",
        "category": [
            {
                "coding": [
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                        "code": "activity",
                        "display": "Activity",
                    }
                ]
            }
        ],
        "code": {
            "coding": [
                {
                    "system": LOINC_SYSTEM,
                    "code": "93832-4",
                    "display": "Sleep duration",
                }
            ],
            "text": "Sleep observation from wearable device",
        },
        "subject": {"reference": f"Patient/{sleep_day.patient_id}"},
        "component": [],
    }

    # effective[x]: exactly one form (FHIR choice type, 0..1)
    if sleep_day.sleep_onset and sleep_day.sleep_offset:
        observation["effectivePeriod"] = {
            "start": sleep_day.sleep_onset.isoformat(),
            "end": sleep_day.sleep_offset.isoformat(),
        }
    else:
        observation["effectiveDateTime"] = sleep_day.effective_date.isoformat()

    # LOINC-coded components
    for field_name, (loinc_code, display) in _SLEEP_LOINC.items():
        value = getattr(sleep_day, field_name, None)
        if value is not None:
            observation["component"].append(
                {
                    "code": {
                        "coding": [
                            {
                                "system": LOINC_SYSTEM,
                                "code": loinc_code,
                                "display": display,
                            }
                        ]
                    },
                    "valueQuantity": {
                        "value": value,
                        "unit": "min",
                        "system": UCUM_SYSTEM,
                        "code": "min",
                    },
                }
            )

    # Sleep efficiency: no standard LOINC — text-only code
    if sleep_day.sleep_efficiency is not None:
        observation["component"].append(
            {
                "code": {"text": "Sleep efficiency"},
                "valueQuantity": {
                    "value": round(sleep_day.sleep_efficiency, 4),
                    "unit": "ratio",
                    "system": UCUM_SYSTEM,
                    "code": "{ratio}",
                },
            }
        )

    # Awake minutes: no LOINC — text-only code
    if sleep_day.awake_minutes is not None:
        observation["component"].append(
            {
                "code": {"text": "Awake duration during sleep"},
                "valueQuantity": {
                    "value": sleep_day.awake_minutes,
                    "unit": "min",
                    "system": UCUM_SYSTEM,
                    "code": "min",
                },
            }
        )

    observation["device"] = {"display": f"{sleep_day.source.value} wearable"}

    return observation
