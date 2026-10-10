"""Day/night mode: auto (photosensing) or forced day/night."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import ThinginoCoordinator
from .entity import ThinginoEntity, dig


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coord: ThinginoCoordinator = entry.runtime_data
    modes = dig(coord.capabilities, "daynight", "modes")
    if dig(coord.capabilities, "daynight", "force_mode") and modes:
        async_add_entities([DayNightSelect(coord, list(modes))])


class DayNightSelect(ThinginoEntity, SelectEntity):
    def __init__(self, coordinator: ThinginoCoordinator, modes: list[str]) -> None:
        super().__init__(coordinator, "daynight_mode")
        self._attr_options = modes

    @property
    def current_option(self) -> str | None:
        # config.daynight.force_mode is null in auto; runtime target_mode says the same thing
        forced = dig(self.coordinator.data.config, "daynight", "force_mode")
        mode = forced or dig(self.coordinator.data.state, "daynight", "target_mode") or "auto"
        return mode if mode in self.options else None

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_daynight(option)
