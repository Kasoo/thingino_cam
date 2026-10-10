"""Buttons: reboot the camera, restart the streamer."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import ThinginoCoordinator
from .entity import ThinginoEntity


@dataclass(frozen=True, kw_only=True)
class ThinginoButtonDescription(ButtonEntityDescription):
    action: str


DESCRIPTIONS = (
    ThinginoButtonDescription(key="reboot", action="reboot", device_class=ButtonDeviceClass.RESTART,
                              entity_category=EntityCategory.CONFIG),
    ThinginoButtonDescription(key="restart_streamer", action="streamer/restart",
                              entity_category=EntityCategory.CONFIG),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities(ThinginoButton(entry.runtime_data, d) for d in DESCRIPTIONS)


class ThinginoButton(ThinginoEntity, ButtonEntity):
    entity_description: ThinginoButtonDescription

    def __init__(self, coordinator: ThinginoCoordinator, desc: ThinginoButtonDescription) -> None:
        super().__init__(coordinator, desc.key)
        self.entity_description = desc

    async def async_press(self) -> None:
        await self.coordinator.async_action(self.entity_description.action)
