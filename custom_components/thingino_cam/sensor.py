"""Sensors: running day/night mode, scene brightness (pushed samples), system diagnostics."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfInformation
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import CameraData, ThinginoCoordinator
from .entity import ThinginoEntity, dig


@dataclass(frozen=True, kw_only=True)
class ThinginoSensorDescription(SensorEntityDescription):
    value: Callable[[CameraData], Any]


def _boot_time(d: CameraData) -> datetime | None:
    up = d.system.get("uptime_seconds")
    if up is None:
        return None
    # round to the minute so the state does not change on every poll
    boot = dt_util.utcnow() - timedelta(seconds=int(up))
    return boot.replace(second=0, microsecond=0)


DESCRIPTIONS = (
    ThinginoSensorDescription(
        key="daynight_running", device_class=SensorDeviceClass.ENUM, options=["day", "night"],
        value=lambda d: d.sample.get("daynight_mode") or dig(d.state, "daynight", "running_mode")),
    # the ISP's average luma, 0-255; a proxy for scene brightness (also drives auto day/night)
    ThinginoSensorDescription(
        key="scene_brightness", state_class=SensorStateClass.MEASUREMENT,
        value=lambda d: d.sample.get("ae_luma")),
    ThinginoSensorDescription(
        key="boot_time", device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC, value=_boot_time),
    ThinginoSensorDescription(
        key="load_average", state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC, value=lambda d: d.system.get("load_average_1m")),
    ThinginoSensorDescription(
        key="memory_available", device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.KIBIBYTES,
        suggested_unit_of_measurement=UnitOfInformation.MEBIBYTES, suggested_display_precision=1,
        state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda d: d.system.get("memory_available_kib")),
    ThinginoSensorDescription(
        key="ip_address", entity_category=EntityCategory.DIAGNOSTIC, entity_registry_enabled_default=False,
        value=lambda d: d.network.get("ip")),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities(ThinginoSensor(entry.runtime_data, d) for d in DESCRIPTIONS)


class ThinginoSensor(ThinginoEntity, SensorEntity):
    entity_description: ThinginoSensorDescription

    def __init__(self, coordinator: ThinginoCoordinator, desc: ThinginoSensorDescription) -> None:
        super().__init__(coordinator, desc.key)
        self.entity_description = desc

    @property
    def native_value(self) -> Any:
        return self.entity_description.value(self.coordinator.data)
