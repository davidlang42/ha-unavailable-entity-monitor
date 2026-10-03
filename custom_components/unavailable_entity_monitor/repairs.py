import asyncio
import logging
from datetime import datetime, timezone
import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, label_registry as lr, issue_registry as ir
from homeassistant.helpers import selector
from .const import DOMAIN, CONF_EXCLUDE_LABEL, DEFAULT_EXCLUDE_LABEL

_LOGGER = logging.getLogger(__name__)

async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: dict | None) -> RepairsFlow:
    """Create the fix flow for an unavailable entity issue."""
    return UnavailableEntityRepairFlow(issue_id, data)

class UnavailableEntityRepairFlow(RepairsFlow):
    """Handler for the repair flow dialogs."""

    def __init__(self, issue_id: str, data: dict | None) -> None:
        self.issue_id = issue_id
        self.data = data or {}
        self.entity_id = self.data.get("entity_id")
        self._selected_switch = None

    def _format_time_ago(self, state_obj) -> str:
        """Helper to format a state's last_changed time into a human-readable string."""
        if not state_obj or not state_obj.last_changed:
            return "an unknown time"

        diff = datetime.now(timezone.utc) - state_obj.last_changed
        seconds = int(diff.total_seconds())

        if seconds < 60:
            return f"{seconds} seconds"
        elif seconds < 3600:
            return f"{seconds // 60} minutes"
        elif seconds < 86400:
            return f"{seconds // 3600} hours"
        else:
            return f"{seconds // 86400} days"

    def _get_entity_duration_info(self) -> tuple[str, str]:
        """Helper to get state and human-readable downtime duration for the failed entity."""
        state_obj = self.hass.states.get(self.entity_id)
        state_str = state_obj.state if state_obj else "unavailable"
        time_str = self._format_time_ago(state_obj)
        return state_str, time_str

    def _preserve_issue(self) -> None:
        """Helper to re-create the issue after a 1-second delay so it stays active after the flow completes."""
        issue_reg = ir.async_get(self.hass)
        issue_entry = issue_reg.issues.get((DOMAIN, self.issue_id))
        if issue_entry:
            def recreate():
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    self.issue_id,
                    is_fixable=issue_entry.is_fixable,
                    severity=issue_entry.severity,
                    translation_key=issue_entry.translation_key,
                    translation_placeholders=issue_entry.translation_placeholders,
                    learn_more_url=issue_entry.learn_more_url,
                    data=issue_entry.data,
                )
            
            self.hass.loop.call_later(1.0, recreate)

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> RepairsFlowResult:
        """Present options via a form choice, with an inline check using the cached label ID."""
        if self.entity_id:
            target_label_id = self.hass.data.get(DOMAIN, {}).get("target_label_id")
            ent_reg = er.async_get(self.hass)
            entity_entry = ent_reg.async_get(self.entity_id)
            
            if target_label_id and entity_entry and entity_entry.labels and target_label_id in entity_entry.labels:
                ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
                return self.async_create_entry(title="", data={})

        if user_input is not None:
            action = user_input.get("action")
            if action == "exclude_entity":
                return await self.async_step_exclude_entity()
            elif action == "power_cycle":
                return await self.async_step_power_cycle()

        state_str, time_str = self._get_entity_duration_info()

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required("action", default="power_cycle"): vol.In({
                    "power_cycle": "Power cycle a corresponding switch",
                    "exclude_entity": "Add to exclusion list (ignore future unavailability)",
                })
            }),
            description_placeholders={
                "entity_id": self.entity_id,
                "state": state_str,
                "time_ago": time_str,
            },
        )

    def _get_configured_label_name(self) -> str:
        """Helper to fetch the configured exclusion label name."""
        entry = self.hass.config_entries.async_entries(DOMAIN)
        if entry:
            return entry[0].options.get(CONF_EXCLUDE_LABEL, entry[0].data.get(CONF_EXCLUDE_LABEL, DEFAULT_EXCLUDE_LABEL))
        return DEFAULT_EXCLUDE_LABEL

    async def async_step_exclude_entity(self, user_input: dict[str, str] | None = None) -> RepairsFlowResult:
        """Ask for confirmation before adding the exclusion label using the cached label ID."""
        if user_input is not None:
            if self.entity_id:
                ent_reg = er.async_get(self.hass)
                entity_entry = ent_reg.async_get(self.entity_id)
                
                if entity_entry:
                    target_label_id = self.hass.data.get(DOMAIN, {}).get("target_label_id")
                    if target_label_id:
                        current_labels = set(entity_entry.labels)
                        if target_label_id not in current_labels:
                            current_labels.add(target_label_id)
                            ent_reg.async_update_entity(self.entity_id, labels=current_labels)

            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
            return self.async_create_entry(title="", data={})

        label_name = self._get_configured_label_name()
        return self.async_show_form(
            step_id="exclude_entity",
            data_schema=vol.Schema({}),
            description_placeholders={
                "label_name": label_name,
                "entity_id": self.entity_id,
            },
        )

    async def async_step_power_cycle(self, user_input: dict[str, str] | None = None) -> RepairsFlowResult:
        """Step 1: Ask the user to select a switch entity, then branch based on its state."""
        if user_input is not None:
            self._selected_switch = user_input.get("switch_entity")
            switch_state = self.hass.states.get(self._selected_switch)
            
            if switch_state and switch_state.state == "off":
                return await self.async_step_confirm_turn_on()
            
            return await self.async_step_confirm_power_cycle()

        domain_data = self.hass.data.get(DOMAIN, {})
        power_switches = domain_data.get("power_switches", {})
        default_switch = power_switches.get(self.entity_id)

        return self.async_show_form(
            step_id="power_cycle",
            data_schema=vol.Schema({
                vol.Required("switch_entity", default=default_switch): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="switch")
                )
            }),
            description_placeholders={"entity_id": self.entity_id},
        )

    async def async_step_confirm_turn_on(self, user_input: dict[str, str] | None = None) -> RepairsFlowResult:
        """Step 2a: Confirm and simply turn on an already-off switch (leaves issue open)."""
        if user_input is not None:
            switch_entity_id = self._selected_switch
            if switch_entity_id and self.entity_id:
                domain_data = self.hass.data.get(DOMAIN, {})
                power_switches = domain_data.setdefault("power_switches", {})
                power_switches[self.entity_id] = switch_entity_id

                store = domain_data.get("store")
                if store:
                    await store.async_save({"power_switches": power_switches})

                _LOGGER.info("Turning on switch %s for unavailable entity %s", switch_entity_id, self.entity_id)
                await self.hass.services.async_call("switch", "turn_on", {"entity_id": switch_entity_id}, blocking=False)

            self._preserve_issue()
            return self.async_create_entry(title="", data={})

        switch_state = self.hass.states.get(self._selected_switch)
        time_str = self._format_time_ago(switch_state)

        return self.async_show_form(
            step_id="confirm_turn_on",
            data_schema=vol.Schema({}),
            description_placeholders={
                "switch_id": self._selected_switch,
                "time_ago": time_str,
            },
        )

    async def async_step_confirm_power_cycle(self, user_input: dict[str, str] | None = None) -> RepairsFlowResult:
        """Step 2b: Confirm and execute a full power cycle (leaves issue open)."""
        if user_input is not None:
            switch_entity_id = self._selected_switch
            if switch_entity_id and self.entity_id:
                domain_data = self.hass.data.get(DOMAIN, {})
                power_switches = domain_data.setdefault("power_switches", {})
                power_switches[self.entity_id] = switch_entity_id

                store = domain_data.get("store")
                if store:
                    await store.async_save({"power_switches": power_switches})

                async def _run_power_cycle():
                    _LOGGER.info("Power cycling %s via user-selected switch %s", self.entity_id, switch_entity_id)
                    await self.hass.services.async_call("switch", "turn_off", {"entity_id": switch_entity_id}, blocking=True)
                    await asyncio.sleep(10)
                    await self.hass.services.async_call("switch", "turn_on", {"entity_id": switch_entity_id}, blocking=True)

                self.hass.async_create_task(_run_power_cycle())

            self._preserve_issue()
            return self.async_create_entry(title="", data={})

        switch_state = self.hass.states.get(self._selected_switch)
        state_str = switch_state.state if switch_state else "unknown"
        time_str = self._format_time_ago(switch_state)

        return self.async_show_form(
            step_id="confirm_power_cycle",
            data_schema=vol.Schema({}),
            description_placeholders={
                "switch_id": self._selected_switch,
                "state": state_str,
                "time_ago": time_str,
            },
        )