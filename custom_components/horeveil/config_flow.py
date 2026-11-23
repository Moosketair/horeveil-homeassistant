"""Create local Horéveil receivers from the Home Assistant UI."""

from secrets import token_hex

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigFlow

from .const import DOMAIN, RELAY_TOKEN, WEBHOOK_ID


class HoreveilConfigFlow(ConfigFlow, domain=DOMAIN):
    """Show the generated relay credentials before creating the receiver."""

    VERSION = 1
    _credentials: dict[str, str] | None = None

    async def async_step_user(self, user_input=None):
        """Confirm the setup of a receiver."""
        if self._credentials is None:
            self._credentials = {
                WEBHOOK_ID: webhook.async_generate_id(),
                RELAY_TOKEN: token_hex(32),
            }
        elif user_input is not None:
            return self.async_create_entry(title="Horéveil", data=self._credentials)

        return self.async_show_form(
            step_id="user",
            description_placeholders={
                "path": webhook.async_generate_path(self._credentials[WEBHOOK_ID]),
                "token": self._credentials[RELAY_TOKEN],
            },
        )
