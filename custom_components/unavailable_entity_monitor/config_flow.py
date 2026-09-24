import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from .const import DOMAIN, CONF_TIMEOUT, CONF_EXCLUDE_LABEL, DEFAULT_TIMEOUT, DEFAULT_EXCLUDE_LABEL

class UnavailableEntityConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Unavailable Entity Monitor."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial user step."""
        if user_input is not None:
            return self.async_create_entry(title="Unavailable Entity Monitor", data=user_input)

        schema = vol.Schema({
            vol.Required(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): int,
            vol.Required(CONF_EXCLUDE_LABEL, default=DEFAULT_EXCLUDE_LABEL): str,
        })

        return self.async_show_form(step_id="user", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return UnavailableEntityOptionsFlow(config_entry)

class UnavailableEntityOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow changes."""

    def __init__(self, config_entry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_timeout = self.config_entry.options.get(
            CONF_TIMEOUT, self.config_entry.data.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)
        )
        current_label = self.config_entry.options.get(
            CONF_EXCLUDE_LABEL, self.config_entry.data.get(CONF_EXCLUDE_LABEL, DEFAULT_EXCLUDE_LABEL)
        )

        schema = vol.Schema({
            vol.Required(CONF_TIMEOUT, default=current_timeout): int,
            vol.Required(CONF_EXCLUDE_LABEL, default=current_label): str,
        })

        return self.async_show_form(step_id="init", data_schema=schema)