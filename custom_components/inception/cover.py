"""Cover platform for inception: doors that drive garage door openers."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityDescription,
    CoverEntityFeature,
)
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN, MANUFACTURER, cover_door_ids
from .entity import InceptionEntity, panel_identifiers
from .pyinception.schemas.door import DoorControlType, DoorPublicState

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import InceptionUpdateCoordinator
    from .data import InceptionConfigEntry
    from .pyinception.schemas.door import DoorSummaryEntry

_LOGGER = logging.getLogger(__name__)

COVER_KEY = "garage_cover"
# How long to show "opening"/"closing" if the sensor never changes state
TRAVEL_TIMEOUT_SECS = 30

# Door entities that a cover replaces, as (platform, unique_id suffix).
# The lock uses the bare door ID as its unique_id.
_REPLACED_ENTITIES = (
    ("lock", ""),
    ("lock", "_door_lock"),
    ("select", "_door_unlock_mechanism"),
    ("number", "_timed_unlock_duration"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: InceptionConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up garage door covers for the doors chosen in options."""
    coordinator: InceptionUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    cover_ids = cover_door_ids(entry.options)
    doors = coordinator.data.doors.get_items()

    _remove_stale_entities(hass, entry, cover_ids, [d.entity_info.id for d in doors])

    async_add_entities(
        InceptionGarageDoor(
            coordinator=coordinator,
            entity_description=CoverEntityDescription(
                key=COVER_KEY, device_class=CoverDeviceClass.GARAGE
            ),
            data=door,
        )
        for door in doors
        if door.entity_info.id in cover_ids
    )


def _remove_stale_entities(
    hass: HomeAssistant,
    entry: InceptionConfigEntry,
    cover_ids: set[str],
    door_ids: list[str],
) -> None:
    """Drop entities left over from switching a door between lock and cover."""
    registry = er.async_get(hass)
    stale: set[tuple[str, str]] = set()
    for door_id in door_ids:
        if door_id in cover_ids:
            stale.update(
                (platform, f"{door_id}{suffix}")
                for platform, suffix in _REPLACED_ENTITIES
            )
        else:
            stale.add(("cover", f"{door_id}_{COVER_KEY}"))

    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (reg_entry.domain, reg_entry.unique_id) in stale:
            _LOGGER.debug("Removing replaced entity %s", reg_entry.entity_id)
            registry.async_remove(reg_entry.entity_id)


class InceptionGarageDoor(InceptionEntity, CoverEntity):
    """
    A door whose lock output pulses a garage door opener's button terminals.

    Open and close both send Grant Access (one button press). The state comes
    from the door's open/closed sensor, so a press is only sent when it would
    move the door towards the requested position.
    """

    data: DoorSummaryEntry

    _attr_supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE
    _attr_device_class = CoverDeviceClass.GARAGE

    def __init__(
        self,
        coordinator: InceptionUpdateCoordinator,
        entity_description: CoverEntityDescription,
        data: DoorSummaryEntry,
    ) -> None:
        """Initialize the cover."""
        self.data = data
        self._travel: str | None = None  # "opening" / "closing"
        self._travel_from_open: bool | None = None
        self._cancel_travel_timeout = None
        super().__init__(
            coordinator, entity_description=entity_description, inception_object=data
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, data.entity_info.id)},
            name=re.sub(
                r"[^a-zA-Z\s]*(Lock|Strike)", "", data.entity_info.name
            ).strip(),
            manufacturer=MANUFACTURER,
            via_device=panel_identifiers(coordinator),
        )

    @property
    def name(self) -> str | None:
        """Use the device (door) name."""
        return None

    @property
    def _is_open(self) -> bool | None:
        state = self.data.public_state
        if state is None:
            return None
        return bool(state & DoorPublicState.OPEN)

    @property
    def is_closed(self) -> bool | None:
        """Return true if the door sensor reports closed."""
        is_open = self._is_open
        return None if is_open is None else not is_open

    @property
    def is_opening(self) -> bool:
        """Return true while an open command is in progress."""
        return self._travel == "opening"

    @property
    def is_closing(self) -> bool:
        """Return true while a close command is in progress."""
        return self._travel == "closing"

    async def async_open_cover(self, **_kwargs: Any) -> None:
        """Open the garage door (one button press) unless already open."""
        if self._is_open or self.is_opening:
            _LOGGER.debug("%s already open/opening; not pressing", self.entity_id)
            return
        await self._press("opening")

    async def async_close_cover(self, **_kwargs: Any) -> None:
        """Close the garage door (one button press) unless already closed."""
        if self.is_closed or self.is_closing:
            _LOGGER.debug("%s already closed/closing; not pressing", self.entity_id)
            return
        await self._press("closing")

    async def _press(self, travel: str) -> None:
        """Send Grant Access to the door, pulsing its lock output."""
        _LOGGER.debug("Granting access to %s (%s)", self.entity_id, travel)
        await self.coordinator.api.request(
            method="post",
            path=f"/control/door/{self.data.entity_info.id}/activity",
            data={"Type": "ControlDoor", "DoorControlType": DoorControlType.OPEN},
        )
        self._start_travel(travel)

    def _start_travel(self, travel: str) -> None:
        self._clear_travel_timeout()
        self._travel = travel
        self._travel_from_open = self._is_open
        self._cancel_travel_timeout = async_call_later(
            self.hass, TRAVEL_TIMEOUT_SECS, self._travel_timed_out
        )
        self.async_write_ha_state()

    @callback
    def _travel_timed_out(self, _now: Any) -> None:
        self._cancel_travel_timeout = None
        self._travel = None
        self.async_write_ha_state()

    def _clear_travel_timeout(self) -> None:
        if self._cancel_travel_timeout is not None:
            self._cancel_travel_timeout()
            self._cancel_travel_timeout = None

    def _update_attrs(self) -> None:
        """Finish the opening/closing state once the sensor changes."""
        if self._travel is not None and self._is_open != self._travel_from_open:
            self._clear_travel_timeout()
            self._travel = None

    async def async_will_remove_from_hass(self) -> None:
        """Cancel any pending timer."""
        self._clear_travel_timeout()
        await super().async_will_remove_from_hass()
