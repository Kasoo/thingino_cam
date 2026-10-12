"""Switches backed by agent settings (PATCH /settings/<path>)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import ThinginoCoordinator
from .entity import ThinginoEntity, dig


@dataclass(frozen=True, kw_only=True)
class ThinginoSwitchDescription(SwitchEntityDescription):
    path: str           # agent setting path
    field: str          # JSON key in the setting resource
    capability: tuple[str, str]


DESCRIPTIONS = (
    ThinginoSwitchDescription(key="motion_detection", path="motion/enabled", field="enabled",
                              capability=("motion", "enabled")),
    ThinginoSwitchDescription(key="privacy", path="privacy/enabled", field="enabled",
                              capability=("privacy", "enabled"),
                              entity_registry_enabled_default=False),
    ThinginoSwitchDescription(key="microphone", path="audio/mic-enabled", field="mic_enabled",
                              capability=("audio", "mic_enabled")),
    ThinginoSwitchDescription(key="flip_horizontal", path="image/hflip", field="hflip",
                              capability=("image", "hflip"), entity_category=EntityCategory.CONFIG,
                              entity_registry_enabled_default=False),
    ThinginoSwitchDescription(key="flip_vertical", path="image/vflip", field="vflip",
                              capability=("image", "vflip"), entity_category=EntityCategory.CONFIG,
                              entity_registry_enabled_default=False),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coord: ThinginoCoordinator = entry.runtime_data
    async_add_entities(ThinginoSwitch(coord, d) for d in DESCRIPTIONS
                       if dig(coord.capabilities, *d.capability))


class ThinginoSwitch(ThinginoEntity, SwitchEntity):
    entity_description: ThinginoSwitchDescription

    def __init__(self, coordinator: ThinginoCoordinator, desc: ThinginoSwitchDescription) -> None:
        super().__init__(coordinator, desc.key)
        self.entity_description = desc

    @property
    def is_on(self) -> bool | None:
        desc = self.entity_description
        value = self.coordinator.setting(desc.path, desc.field)
        return None if value is None else bool(value)

    async def _set(self, value: bool) -> None:
        desc = self.entity_description
        await self.coordinator.async_set_setting(desc.path, desc.field, value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)
