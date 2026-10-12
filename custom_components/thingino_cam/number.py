"""Numbers: image tuning (ISP, 0-255) and motion detection parameters."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import ThinginoCoordinator
from .entity import ThinginoEntity, dig


@dataclass(frozen=True, kw_only=True)
class ThinginoNumberDescription(NumberEntityDescription):
    path: str
    field: str
    capability: tuple[str, str]
    # what rmd/the ISP uses while raptor.conf leaves the key commented out (the agent reports null)
    default: float | None = None


def _image(key: str) -> ThinginoNumberDescription:
    return ThinginoNumberDescription(
        key=key, path=f"image/{key}", field=key, capability=("image", key), default=128,
        native_min_value=0, native_max_value=255, native_step=1, mode=NumberMode.SLIDER,
        entity_category=EntityCategory.CONFIG, entity_registry_enabled_default=False)


DESCRIPTIONS = (
    _image("brightness"), _image("contrast"), _image("saturation"), _image("sharpness"),
    # raptor.conf: "sensitivity = 3  # move: 0-4"
    ThinginoNumberDescription(
        key="motion_sensitivity", path="motion/sensitivity", field="sensitivity",
        capability=("motion", "sensitivity"), default=3, native_min_value=0, native_max_value=4,
        native_step=1, mode=NumberMode.BOX, entity_category=EntityCategory.CONFIG),
    ThinginoNumberDescription(
        key="motion_cooldown", path="motion/cooldown-time", field="cooldown_time",
        capability=("motion", "cooldown_time"), default=10, native_min_value=0, native_max_value=600,
        native_step=1, native_unit_of_measurement=UnitOfTime.SECONDS, mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coord: ThinginoCoordinator = entry.runtime_data
    async_add_entities(ThinginoNumber(coord, d) for d in DESCRIPTIONS
                       if dig(coord.capabilities, *d.capability))


class ThinginoNumber(ThinginoEntity, NumberEntity):
    entity_description: ThinginoNumberDescription

    def __init__(self, coordinator: ThinginoCoordinator, desc: ThinginoNumberDescription) -> None:
        super().__init__(coordinator, desc.key)
        self.entity_description = desc

    @property
    def native_value(self) -> float | None:
        desc = self.entity_description
        value = self.coordinator.setting(desc.path, desc.field)
        return desc.default if value is None else value

    async def async_set_native_value(self, value: float) -> None:
        desc = self.entity_description
        await self.coordinator.async_set_setting(desc.path, desc.field, int(value))
