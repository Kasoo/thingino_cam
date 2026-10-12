"""Sensors: running day/night mode (pushed samples), last boot time, load average, IP address."""
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
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import CameraData, ThinginoCoordinator
from .entity import ThinginoEntity, dig


@dataclass(frozen=True, kw_only=True)
class ThinginoSensorDescription(SensorEntityDescription):
    value: Callable[[CameraData], Any]
    uses_health: bool = False


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
    ThinginoSensorDescription(
        key="boot_time", device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC, entity_registry_enabled_default=False,
        uses_health=True, value=_boot_time),
    # 1-minute load average; it counts tasks in uninterruptible sleep, so it reads high on these
    # cameras even while the CPU is mostly idle
    ThinginoSensorDescription(
        key="load_average", state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC, entity_registry_enabled_default=False,
        uses_health=True, value=lambda d: d.system.get("load_average_1m")),
    ThinginoSensorDescription(
        key="ip_address", entity_category=EntityCategory.DIAGNOSTIC, entity_registry_enabled_default=False,
        uses_health=True, value=lambda d: d.network.get("ip")),
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
    def uses_health(self) -> bool:
        return self.entity_description.uses_health

    @property
    def native_value(self) -> Any:
        return self.entity_description.value(self.coordinator.data)
