"""
SMA Solar Webconnect interface.

For more details about this platform, please refer to the documentation at
https://home-assistant.io/components/sensor.sma/
"""
import asyncio
from datetime import timedelta
import logging

import voluptuous as vol

from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.const import (
    CONF_HOST, CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_SSL,
    EVENT_HOMEASSISTANT_STOP)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_track_time_interval

REQUIREMENTS = ['pysma==0.2']

_LOGGER = logging.getLogger(__name__)

CONF_CUSTOM = 'custom'
CONF_FACTOR = 'factor'
CONF_GROUP = 'group'
CONF_KEY = 'key'
CONF_SENSORS = 'sensors'
CONF_UNIT = 'unit'

GROUP_INSTALLER = 'installer'
GROUP_USER = 'user'
GROUPS = [GROUP_USER, GROUP_INSTALLER]

SENSOR_OPTIONS = [
    'current_consumption',
    'current_power',
    'total_consumption',
    'total_yield',
]


def _check_sensor_schema(conf):
    """Check sensors and attributes are valid."""
    valid = list(conf[CONF_CUSTOM].keys())
    valid.extend(SENSOR_OPTIONS)
    for sensor, attrs in conf[CONF_SENSORS].items():
        if sensor not in valid:
            raise vol.Invalid("{} does not exist".format(sensor))
        for attr in attrs:
            if attr in valid:
                continue
            raise vol.Invalid("{} does not exist [{}]".format(attr, sensor))
    return conf


CUSTOM_SCHEMA = vol.Any({
    vol.Required(CONF_KEY):
    vol.All(cv.string, vol.Length(min=13, max=13)),
    vol.Required(CONF_UNIT): cv.string,
    vol.Optional(CONF_FACTOR, default=1): vol.Coerce(float),
})

PLATFORM_SCHEMA = vol.All(PLATFORM_SCHEMA.extend({
    vol.Required(CONF_HOST): cv.string,
    vol.Optional(CONF_SSL, default=False): cv.boolean,
    vol.Required(CONF_PASSWORD): cv.string,
    vol.Optional(CONF_GROUP, default=GROUPS[0]): vol.In(GROUPS),
    vol.Required(CONF_SENSORS): vol.Schema({cv.slug: cv.ensure_list}),
    vol.Optional(CONF_CUSTOM, default={}):
        vol.Schema({cv.slug: CUSTOM_SCHEMA}),
}, extra=vol.PREVENT_EXTRA), _check_sensor_schema)


async def async_setup_platform(
        hass, config, async_add_entities, discovery_info=None):
    """Set up SMA WebConnect sensor."""
    import pysma

    # Sensor_defs from the library
    sensor_defs = dict(zip(SENSOR_OPTIONS, [
        (pysma.KEY_CURRENT_CONSUMPTION_W, 'W', 1),
        (pysma.KEY_CURRENT_POWER_W, 'W', 1),
        (pysma.KEY_TOTAL_CONSUMPTION_KWH, 'kWh', 1000),
        (pysma.KEY_TOTAL_YIELD_KWH, 'kWh', 1000)]))

    # Sensor_defs from the custom config
    for name, prop in config[CONF_CUSTOM].items():
        if name in sensor_defs:
            _LOGGER.warning("Custom sensor %s replace built-in sensor", name)
        sensor_defs[name] = (prop['key'], prop['unit'], prop['factor'])

    # Prepare all HASS sensor entities
    hass_sensors = []
    used_sensors = []
    for name, attr in config[CONF_SENSORS].items():
        hass_sensors.append(SMAsensor(name, attr, sensor_defs))
        used_sensors.append(name)
        used_sensors.extend(attr)

    # Remove sensor_defs not in use
    sensor_defs = {name: val for name, val in sensor_defs.items()
                   if name in used_sensors}

    async_add_entities(hass_sensors)

    # Init the SMA interface
    session = async_get_clientsession(hass)
    grp = {GROUP_INSTALLER: pysma.GROUP_INSTALLER,
           GROUP_USER: pysma.GROUP_USER}[config[CONF_GROUP]]

    url = "http{}://{}".format(
        "s" if config[CONF_SSL] else "", config[CONF_HOST])

    sma = pysma.SMA(session, url, config[CONF_PASSWORD], group=grp)

    # Ensure we logout on shutdown
    async def async_close_session(event):
        """Close the session."""
        await sma.close_session()

    hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, async_close_session)

    # Read SMA values periodically & update sensors
    names_to_query = list(sensor_defs.keys())
    keys_to_query = [sensor_defs[name][0] for name in names_to_query]

    backoff = 0

    async def async_sma(event):
        """Update all the SMA sensors."""
        nonlocal backoff
        if backoff > 1:
            backoff -= 1
            return

        values = await sma.read(keys_to_query)
        if values is None:
            backoff = 3
            return
        values = [0 if val is None else val for val in values]
        res = dict(zip(names_to_query, values))
        res = {key: val // sensor_defs[key][2] for key, val in res.items()}
        _LOGGER.debug("Update sensors %s %s %s", keys_to_query, values, res)
        tasks = []
        for sensor in hass_sensors:
            task = sensor.async_update_values(res)
            if task:
                tasks.append(task)
        if tasks:
            await asyncio.wait(tasks, loop=hass.loop)

    interval = config.get(CONF_SCAN_INTERVAL) or timedelta(seconds=5)
    async_track_time_interval(hass, async_sma, interval)


class SMAsensor(Entity):
    """Representation of a SMA sensor."""

    def __init__(self, sensor_name, attr, sensor_defs):
        """Initialize the sensor."""
        self._name = sensor_name
        self._key, self._unit_of_measurement, _ = sensor_defs[sensor_name]
        self._state = None
        self._sensor_defs = sensor_defs
        self._attr = {att: "" for att in attr}

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def state(self):
        """Return the state of the sensor."""
        return self._state

    @property
    def unit_of_measurement(self):
        """Return the unit the value is expressed in."""
        return self._unit_of_measurement

    @property
    def device_state_attributes(self):
        """Return the state attributes of the sensor."""
        return self._attr

    @property
    def poll(self):
        """SMA sensors are updated & don't poll."""
        return False

    def async_update_values(self, key_values):
        """Update this sensor using the data."""
        update = False

        for key, val in self._attr.items():
            newval = '{} {}'.format(key_values[key], self._sensor_defs[key][1])
            if val != newval:
                update = True
                self._attr[key] = newval

        new_state = key_values[self._name]
        if new_state != self._state:
            update = True
            self._state = new_state

        return self.async_update_ha_state() if update else None
