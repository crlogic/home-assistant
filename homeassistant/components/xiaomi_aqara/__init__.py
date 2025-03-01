"""Support for Xiaomi Gateways."""
from datetime import timedelta
import logging

import voluptuous as vol
from xiaomi_gateway import XiaomiGateway, XiaomiGatewayDiscovery

from homeassistant import config_entries, core
from homeassistant.const import (
    ATTR_BATTERY_LEVEL,
    ATTR_DEVICE_ID,
    ATTR_VOLTAGE,
    CONF_HOST,
    CONF_MAC,
    CONF_PORT,
    CONF_PROTOCOL,
    EVENT_HOMEASSISTANT_STOP,
    Platform,
)
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.entity import DeviceInfo, Entity
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util.dt import utcnow

from .const import (
    CONF_INTERFACE,
    CONF_KEY,
    CONF_SID,
    DEFAULT_DISCOVERY_RETRY,
    DOMAIN,
    GATEWAYS_KEY,
    LISTENER_KEY,
)

_LOGGER = logging.getLogger(__name__)

GATEWAY_PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.COVER,
    Platform.LIGHT,
    Platform.LOCK,
    Platform.SENSOR,
    Platform.SWITCH,
]
GATEWAY_PLATFORMS_NO_KEY = [Platform.BINARY_SENSOR, Platform.SENSOR]

ATTR_GW_MAC = "gw_mac"
ATTR_RINGTONE_ID = "ringtone_id"
ATTR_RINGTONE_VOL = "ringtone_vol"

TIME_TILL_UNAVAILABLE = timedelta(minutes=150)

SERVICE_PLAY_RINGTONE = "play_ringtone"
SERVICE_STOP_RINGTONE = "stop_ringtone"
SERVICE_ADD_DEVICE = "add_device"
SERVICE_REMOVE_DEVICE = "remove_device"

SERVICE_SCHEMA_PLAY_RINGTONE = vol.Schema(
    {
        vol.Required(ATTR_RINGTONE_ID): vol.All(
            vol.Coerce(int), vol.NotIn([9, 14, 15, 16, 17, 18, 19])
        ),
        vol.Optional(ATTR_RINGTONE_VOL): vol.All(
            vol.Coerce(int), vol.Clamp(min=0, max=100)
        ),
    }
)

SERVICE_SCHEMA_REMOVE_DEVICE = vol.Schema(
    {vol.Required(ATTR_DEVICE_ID): vol.All(cv.string, vol.Length(min=14, max=14))}
)


def setup(hass, config):
    """Set up the Xiaomi component."""

    def play_ringtone_service(call):
        """Service to play ringtone through Gateway."""
        ring_id = call.data.get(ATTR_RINGTONE_ID)
        gateway = call.data.get(ATTR_GW_MAC)

        kwargs = {"mid": ring_id}

        if (ring_vol := call.data.get(ATTR_RINGTONE_VOL)) is not None:
            kwargs["vol"] = ring_vol

        gateway.write_to_hub(gateway.sid, **kwargs)

    def stop_ringtone_service(call):
        """Service to stop playing ringtone on Gateway."""
        gateway = call.data.get(ATTR_GW_MAC)
        gateway.write_to_hub(gateway.sid, mid=10000)

    def add_device_service(call):
        """Service to add a new sub-device within the next 30 seconds."""
        gateway = call.data.get(ATTR_GW_MAC)
        gateway.write_to_hub(gateway.sid, join_permission="yes")
        hass.components.persistent_notification.async_create(
            "Join permission enabled for 30 seconds! "
            "Please press the pairing button of the new device once.",
            title="Xiaomi Aqara Gateway",
        )

    def remove_device_service(call):
        """Service to remove a sub-device from the gateway."""
        device_id = call.data.get(ATTR_DEVICE_ID)
        gateway = call.data.get(ATTR_GW_MAC)
        gateway.write_to_hub(gateway.sid, remove_device=device_id)

    gateway_only_schema = _add_gateway_to_schema(hass, vol.Schema({}))

    hass.services.register(
        DOMAIN,
        SERVICE_PLAY_RINGTONE,
        play_ringtone_service,
        schema=_add_gateway_to_schema(hass, SERVICE_SCHEMA_PLAY_RINGTONE),
    )

    hass.services.register(
        DOMAIN, SERVICE_STOP_RINGTONE, stop_ringtone_service, schema=gateway_only_schema
    )

    hass.services.register(
        DOMAIN, SERVICE_ADD_DEVICE, add_device_service, schema=gateway_only_schema
    )

    hass.services.register(
        DOMAIN,
        SERVICE_REMOVE_DEVICE,
        remove_device_service,
        schema=_add_gateway_to_schema(hass, SERVICE_SCHEMA_REMOVE_DEVICE),
    )

    return True


async def async_setup_entry(
    hass: core.HomeAssistant, entry: config_entries.ConfigEntry
):
    """Set up the xiaomi aqara components from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(GATEWAYS_KEY, {})

    # Connect to Xiaomi Aqara Gateway
    xiaomi_gateway = await hass.async_add_executor_job(
        XiaomiGateway,
        entry.data[CONF_HOST],
        entry.data[CONF_SID],
        entry.data[CONF_KEY],
        DEFAULT_DISCOVERY_RETRY,
        entry.data[CONF_INTERFACE],
        entry.data[CONF_PORT],
        entry.data[CONF_PROTOCOL],
    )
    hass.data[DOMAIN][GATEWAYS_KEY][entry.entry_id] = xiaomi_gateway

    gateway_discovery = hass.data[DOMAIN].setdefault(
        LISTENER_KEY,
        XiaomiGatewayDiscovery(hass.add_job, [], entry.data[CONF_INTERFACE]),
    )

    if len(hass.data[DOMAIN][GATEWAYS_KEY]) == 1:
        # start listining for local pushes (only once)
        await hass.async_add_executor_job(gateway_discovery.listen)

        # register stop callback to shutdown listining for local pushes
        def stop_xiaomi(event):
            """Stop Xiaomi Socket."""
            _LOGGER.debug("Shutting down Xiaomi Gateway Listener")
            gateway_discovery.stop_listen()

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop_xiaomi)

    gateway_discovery.gateways[entry.data[CONF_HOST]] = xiaomi_gateway
    _LOGGER.debug(
        "Gateway with host '%s' connected, listening for broadcasts",
        entry.data[CONF_HOST],
    )

    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.unique_id)},
        manufacturer="Xiaomi Aqara",
        name=entry.title,
        sw_version=entry.data[CONF_PROTOCOL],
    )

    if entry.data[CONF_KEY] is not None:
        platforms = GATEWAY_PLATFORMS
    else:
        platforms = GATEWAY_PLATFORMS_NO_KEY

    hass.config_entries.async_setup_platforms(entry, platforms)

    return True


async def async_unload_entry(
    hass: core.HomeAssistant, entry: config_entries.ConfigEntry
):
    """Unload a config entry."""
    if entry.data[CONF_KEY] is not None:
        platforms = GATEWAY_PLATFORMS
    else:
        platforms = GATEWAY_PLATFORMS_NO_KEY

    unload_ok = await hass.config_entries.async_unload_platforms(entry, platforms)
    if unload_ok:
        hass.data[DOMAIN][GATEWAYS_KEY].pop(entry.entry_id)

    if len(hass.data[DOMAIN][GATEWAYS_KEY]) == 0:
        # No gateways left, stop Xiaomi socket
        hass.data[DOMAIN].pop(GATEWAYS_KEY)
        _LOGGER.debug("Shutting down Xiaomi Gateway Listener")
        gateway_discovery = hass.data[DOMAIN].pop(LISTENER_KEY)
        await hass.async_add_executor_job(gateway_discovery.stop_listen)

    return unload_ok


class XiaomiDevice(Entity):
    """Representation a base Xiaomi device."""

    def __init__(self, device, device_type, xiaomi_hub, config_entry):
        """Initialize the Xiaomi device."""
        self._state = None
        self._is_available = True
        self._sid = device["sid"]
        self._model = device["model"]
        self._protocol = device["proto"]
        self._name = f"{device_type}_{self._sid}"
        self._device_name = f"{self._model}_{self._sid}"
        self._type = device_type
        self._write_to_hub = xiaomi_hub.write_to_hub
        self._get_from_hub = xiaomi_hub.get_from_hub
        self._extra_state_attributes = {}
        self._remove_unavailability_tracker = None
        self._xiaomi_hub = xiaomi_hub
        self.parse_data(device["data"], device["raw_data"])
        self.parse_voltage(device["data"])

        if hasattr(self, "_data_key") and self._data_key:  # pylint: disable=no-member
            self._unique_id = (
                f"{self._data_key}{self._sid}"  # pylint: disable=no-member
            )
        else:
            self._unique_id = f"{self._type}{self._sid}"

        self._gateway_id = config_entry.unique_id
        if config_entry.data[CONF_MAC] == format_mac(self._sid):
            # this entity belongs to the gateway itself
            self._is_gateway = True
            self._device_id = config_entry.unique_id
        else:
            # this entity is connected through zigbee
            self._is_gateway = False
            self._device_id = self._sid

    def _add_push_data_job(self, *args):
        self.hass.add_job(self.push_data, *args)

    async def async_added_to_hass(self):
        """Start unavailability tracking."""
        self._xiaomi_hub.callbacks[self._sid].append(self._add_push_data_job)
        self._async_track_unavailable()

    @property
    def name(self):
        """Return the name of the device."""
        return self._name

    @property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return self._unique_id

    @property
    def device_id(self):
        """Return the device id of the Xiaomi Aqara device."""
        return self._device_id

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info of the Xiaomi Aqara device."""
        if self._is_gateway:
            device_info = DeviceInfo(
                identifiers={(DOMAIN, self._device_id)},
                model=self._model,
            )
        else:
            device_info = DeviceInfo(
                connections={(dr.CONNECTION_ZIGBEE, self._device_id)},
                identifiers={(DOMAIN, self._device_id)},
                manufacturer="Xiaomi Aqara",
                model=self._model,
                name=self._device_name,
                sw_version=self._protocol,
                via_device=(DOMAIN, self._gateway_id),
            )

        return device_info

    @property
    def available(self):
        """Return True if entity is available."""
        return self._is_available

    @property
    def should_poll(self):
        """Return the polling state. No polling needed."""
        return False

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        return self._extra_state_attributes

    @callback
    def _async_set_unavailable(self, now):
        """Set state to UNAVAILABLE."""
        self._remove_unavailability_tracker = None
        self._is_available = False
        self.async_write_ha_state()

    @callback
    def _async_track_unavailable(self):
        if self._remove_unavailability_tracker:
            self._remove_unavailability_tracker()
        self._remove_unavailability_tracker = async_track_point_in_utc_time(
            self.hass, self._async_set_unavailable, utcnow() + TIME_TILL_UNAVAILABLE
        )
        if not self._is_available:
            self._is_available = True
            return True
        return False

    @callback
    def push_data(self, data, raw_data):
        """Push from Hub."""
        _LOGGER.debug("PUSH >> %s: %s", self, data)
        was_unavailable = self._async_track_unavailable()
        is_data = self.parse_data(data, raw_data)
        is_voltage = self.parse_voltage(data)
        if is_data or is_voltage or was_unavailable:
            self.async_write_ha_state()

    def parse_voltage(self, data):
        """Parse battery level data sent by gateway."""
        if "voltage" in data:
            voltage_key = "voltage"
        elif "battery_voltage" in data:
            voltage_key = "battery_voltage"
        else:
            return False

        max_volt = 3300
        min_volt = 2800
        voltage = data[voltage_key]
        self._extra_state_attributes[ATTR_VOLTAGE] = round(voltage / 1000.0, 2)
        voltage = min(voltage, max_volt)
        voltage = max(voltage, min_volt)
        percent = ((voltage - min_volt) / (max_volt - min_volt)) * 100
        self._extra_state_attributes[ATTR_BATTERY_LEVEL] = round(percent, 1)
        return True

    def parse_data(self, data, raw_data):
        """Parse data sent by gateway."""
        raise NotImplementedError()


def _add_gateway_to_schema(hass, schema):
    """Extend a voluptuous schema with a gateway validator."""

    def gateway(sid):
        """Convert sid to a gateway."""
        sid = str(sid).replace(":", "").lower()

        for gateway in hass.data[DOMAIN][GATEWAYS_KEY].values():
            if gateway.sid == sid:
                return gateway

        raise vol.Invalid(f"Unknown gateway sid {sid}")

    kwargs = {}
    if (xiaomi_data := hass.data.get(DOMAIN)) is not None:
        gateways = list(xiaomi_data[GATEWAYS_KEY].values())

        # If the user has only 1 gateway, make it the default for services.
        if len(gateways) == 1:
            kwargs["default"] = gateways[0].sid

    return schema.extend({vol.Required(ATTR_GW_MAC, **kwargs): gateway})
