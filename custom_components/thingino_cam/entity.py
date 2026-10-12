"""Base entity: every entity hangs off the one camera device."""
from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ThinginoCoordinator
from .rtsp import url_host


class ThinginoEntity(CoordinatorEntity[ThinginoCoordinator]):
    _attr_has_entity_name = True
    # True for entities read from /health data (system, network); the coordinator polls /health only
    # while at least one such entity exists, and disabled entities are never created.
    uses_health = False

    def __init__(self, coordinator: ThinginoCoordinator, key: str) -> None:
        super().__init__(coordinator)
        dev = coordinator.device
        self._attr_unique_id = f"{coordinator.device_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.device_id)},
            manufacturer=dev.get("manufacturer") or "Thingino",
            model=dev.get("model"),
            sw_version=(dev.get("software") or {}).get("firmware_version"),
            name=coordinator.config_entry.title,
            configuration_url=f"https://{url_host(coordinator.config_entry.data['host'])}/",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.uses_health:
            self.coordinator.health_users.add(self.entity_id)
            self.async_on_remove(lambda: self.coordinator.health_users.discard(self.entity_id))

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None


def dig(data: dict[str, Any] | None, *path: str | int) -> Any:
    """Nested lookup that tolerates missing keys/None."""
    cur: Any = data
    for part in path:
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and isinstance(part, int) and 0 <= part < len(cur):
            cur = cur[part]
        else:
            return None
    return cur
