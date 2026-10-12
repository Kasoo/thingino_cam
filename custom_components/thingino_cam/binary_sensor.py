"""Binary sensors: motion (pushed over SSE) and the event stream connection."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import CameraData, ThinginoCoordinator
from .entity import ThinginoEntity


@dataclass(frozen=True, kw_only=True)
class ThinginoBinaryDescription(BinarySensorEntityDescription):
    value: Callable[[CameraData, ThinginoCoordinator], bool | None]


DESCRIPTIONS = (
    ThinginoBinaryDescription(
        key="motion", device_class=BinarySensorDeviceClass.MOTION,
        value=lambda d, c: d.motion),
    ThinginoBinaryDescription(
        key="event_stream", device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC, entity_registry_enabled_default=False,
        value=lambda d, c: c.events_connected),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities(ThinginoBinarySensor(entry.runtime_data, desc) for desc in DESCRIPTIONS)


class ThinginoBinarySensor(ThinginoEntity, BinarySensorEntity):
    entity_description: ThinginoBinaryDescription

    def __init__(self, coordinator: ThinginoCoordinator, desc: ThinginoBinaryDescription) -> None:
        super().__init__(coordinator, desc.key)
        self.entity_description = desc

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value(self.coordinator.data, self.coordinator)
