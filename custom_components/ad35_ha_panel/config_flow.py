"""Discover -> explicit HA consent -> OAuth -> explicit selection -> save."""

from __future__ import annotations

import asyncio
from collections import Counter
import secrets
from urllib.parse import urlencode

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .bootstrap import BootstrapClient, pin_firmware_supported, validate_pin
from .commissioning import CommissioningClient, validate_token, verified_unbound_info
from .pending import journal, match_binding
from .const import (
    ALLOWLIST_FILE, CALLBACK_PATH, CLIENT_PATH, SELECTABLE_DOMAINS, DOMAIN,
    MODEL, PORT, COMMISSIONING_PORT, COMMISSIONING_TRANSPORT,
)
from .oauth import ensure_views
from .pairing_file import load_uploaded_pairing_file
from .protocol import (
    PanelClient, PanelError, PairingSession, build_device_config, load_device_config,
    normalize_mac, normalize_origin, validate_host,
)


class AD35ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        from .local_update import LocalOptionsFlow
        return LocalOptionsFlow()

    def __init__(self):
        self.client = None
        self.pairing = None
        self.removed = False
        self._host = ""
        self._mac = ""
        self._config = None
        self._bootstrap = None
        self._pin_credentials = False
        self._pin_verification_only = False
        self._imported = None
        self._discovered_mac = ""
        self._discovered_host = ""
        self._psk_reference = False
        self._bound = False
        self._recover_bound = False
        self._title = "AD35 HA Panel"
        self._origin = ""
        self._tasks = {}
        self._oauth_url = None
        self._code = None
        self._oauth_received = False
        self._oauth_denied = False
        self._selection = None
        self._selection_presented = False
        self._selection_confirmed = False
        self._weather_optional = False
        self._previous_selected_ids = []
        self._previous_weather_id = ""
        self._reconnect_scope = None
        self._reconnect_confirmed = False
        self._entry = None
        self._failure_reason = "cannot_connect"
        self._registry = None
        self._transport = None
        self._configuration_token = None
        self._binding_id = None
        self._binding_revision = 0
        self._binding_verified = False
        self._recovery_record = None

    def _default_origin(self):
        """Suggest this HA's supported internal origin, retaining existing bindings."""
        if self._entry is not None:
            return normalize_origin(self._entry.data.get("ha_origin", ""))
        try:
            return normalize_origin(get_url(
                self.hass, allow_external=False, allow_cloud=False,
                allow_ip=True, prefer_external=False,
            ))
        except (NoURLAvailableError, PanelError):
            pass
        # get_url prefers a configured internal hostname, which this firmware
        # cannot use over HTTP. Its detected local IP is the same safe fallback
        # as Core's network helper, provided HA is not serving HTTPS itself.
        api = getattr(self.hass.config, "api", None)
        if api is not None and not api.use_ssl:
            try:
                return normalize_origin(f"http://{api.local_ip}:{api.port}")
            except PanelError:
                pass
        return ""

    def _catalog_is_local(self):
        """Prove the catalog belongs to this HA, independently of the bound entry."""
        origins = []
        try:
            origins.append(get_url(self.hass, allow_cloud=False, allow_ip=True, prefer_external=False))
        except NoURLAvailableError:
            pass
        origins.extend(getattr(self.hass.config, name, None) for name in ("internal_url", "external_url"))
        api = getattr(self.hass.config, "api", None)
        if api is not None and not api.use_ssl:
            origins.append(f"http://{api.local_ip}:{api.port}")
        matches = False
        for origin in origins:
            try:
                if normalize_origin(origin) == normalize_origin(self._origin):
                    matches = True
                    break
            except PanelError:
                continue
        states = getattr(self.hass, "states", None)
        return bool(matches and states is not None and
                    all(states.get(item["entity_id"]) is not None for item in self.pairing.catalog))

    async def _entry_config(self, entry):
        if (normalize_mac(entry.data.get("mac", "")) != entry.unique_id
                or entry.data.get("model") != MODEL):
            raise PanelError("identity_mismatch")
        origin = normalize_origin(entry.data.get("ha_origin", ""))
        if "noise_psk" in entry.data:
            return build_device_config(entry.data["noise_psk"], origin,
                                       port=entry.data.get("port", PORT)), False
        if entry.data.get("psk_reference") != ALLOWLIST_FILE:
            raise PanelError("invalid_noise_psk")
        config = await self.hass.async_add_executor_job(
            load_device_config, self.hass.config.path(ALLOWLIST_FILE), entry.unique_id,
        )
        if config.ha_origin != origin:
            raise PanelError("origin_mismatch")
        return config, True

    async def _prepare(self, host, mac, config, *, psk_reference=False):
        if self.removed:
            raise PanelError("cancelled")
        # A retry must not leave the previous encrypted connection open.
        if self.client is not None:
            await self.client.close()
            self.client = None
        self._transport = None
        self._host = validate_host(host or config.host or "")
        self._mac = normalize_mac(mac)
        origin = normalize_origin(config.ha_origin)
        if self._entry is not None:
            if self._mac != self._entry.unique_id:
                raise PanelError("identity_mismatch")
            if origin != normalize_origin(self._entry.data.get("ha_origin", "")):
                raise PanelError("origin_mismatch")
        self._origin = origin
        client = PanelClient(self._host, config, self._mac)
        self.client = client
        try:
            info = await client.connect()
            if self.removed:
                raise PanelError("cancelled")
            if self._pin_credentials and not pin_firmware_supported(client.version):
                raise PanelError("bootstrap_unsupported_firmware")
            await self.async_set_unique_id(self._mac)
            if self.removed:
                raise PanelError("cancelled")
            if self._entry is not None:
                self._abort_if_unique_id_mismatch(reason="identity_mismatch")
                if not info["configured"]:
                    raise PanelError("not_bound")
            elif not self._pin_verification_only:
                # mDNS is only a hint. Updating an existing host is permitted
                # only after Noise, native identity and RPC identity all match.
                self._abort_if_unique_id_configured(updates={"host": self._host, "port": (
                    COMMISSIONING_PORT if any(e.unique_id == self._mac and e.data.get("transport") == COMMISSIONING_TRANSPORT
                                              for e in self._async_current_entries()) else PORT)})
            if info["busy"]:
                raise PanelError("busy")
        except BaseException:
            await client.close()
            self.client = None
            raise
        self._config = config
        self._psk_reference = psk_reference
        self._bound = info["configured"]
        self._previous_selected_ids, self._previous_weather_id = [], ""
        self._recover_bound = self._bound and self._entry is None
        self._title = client.friendly_name or client.name or f"AD35 {self._mac[-6:].upper()}"
        self.context["title_placeholders"] = {"name": self._title}
        self._registry = ensure_views(self.hass)

    async def _prepare_commissioning(self, host, mac, origin, *, credential=None, binding_id=None,
                                     recovering=False, reconnecting=False):
        if self.removed:
            raise PanelError("cancelled")
        host, mac, origin = validate_host(host), normalize_mac(mac), normalize_origin(origin)
        if self._entry is not None:
            if mac != self._entry.unique_id or self._entry.data.get("model") != MODEL:
                raise PanelError("identity_mismatch")
            if origin != normalize_origin(self._entry.data.get("ha_origin", "")):
                raise PanelError("origin_mismatch")
        if credential is not None:
            validate_token(credential)
            validate_token(binding_id)
        if reconnecting and (not self._reconnect_confirmed or self._entry is None
                             or credential is not None or binding_id is not None):
            raise PanelError("invalid_request")
        if self.client is not None:
            await self.client.close()
        self.client = client = CommissioningClient(host, mac, credential)
        try:
            info = await client.connect()
            if self.removed:
                raise PanelError("cancelled")
            if reconnecting and info["configured"]:
                raise PanelError("commissioning_already_bound")
            revision = 0
            previous_ids, previous_weather = [], ""
            if credential is not None:
                binding = await client.rpc("binding", {})
                if binding["ha_origin"] != origin or binding["binding_id"] != binding_id:
                    raise PanelError("origin_mismatch")
                revision = binding["revision"]
                previous_ids, previous_weather = list(binding["entity_ids"]), binding["weather_id"]
            if self.removed:
                raise PanelError("cancelled")
            await self.async_set_unique_id(mac)
            if self._entry is not None:
                self._abort_if_unique_id_mismatch(reason="identity_mismatch")
                if not info["configured"] and not reconnecting:
                    raise PanelError("not_bound")
            elif not recovering:
                # Certificate MAC and, for existing entries, its scope token
                # are authenticated before changing the discovered address.
                self._abort_if_unique_id_configured(updates={"host": host, "port": COMMISSIONING_PORT})
            if info["busy"]:
                raise PanelError("busy")
            if not info["configured"] and not info["commissioning"]:
                raise PanelError("commissioning_closed")
            if info["configured"] and credential is None:
                raise PanelError("commissioning_already_bound")
        except BaseException:
            await client.close()
            self.client = None
            raise
        self._transport = COMMISSIONING_TRANSPORT
        self._host, self._mac, self._origin = host, mac, origin
        self._configuration_token, self._binding_id = credential, binding_id
        self._binding_revision = revision
        self._previous_selected_ids, self._previous_weather_id = previous_ids, previous_weather
        self._binding_verified = False
        self._bound = info["configured"]
        self._recover_bound = False
        self._title = client.friendly_name or client.name or f"AD35 {mac[-6:].upper()}"
        self.context["title_placeholders"] = {"name": self._title}
        self._registry = ensure_views(self.hass)

    def _commissioning_entry_data(self):
        return {
            "mac": self._mac, "host": self._host, "port": COMMISSIONING_PORT,
            "ha_origin": self._origin, "name": self.client.name, "friendly_name": self._title,
            "model": MODEL, "firmware": self.client.version, "transport": COMMISSIONING_TRANSPORT,
            "configuration_token": validate_token(self._configuration_token),
            "binding_id": validate_token(self._binding_id),
        }

    async def async_step_auto(self, user_input=None, *, _error=None):
        errors = {"base": _error} if _error else {}
        if user_input is not None:
            try:
                if user_input.get("confirm") is not True:
                    raise PanelError("commissioning_confirmation_required")
                host = validate_host(user_input.get("host", self._discovered_host or self._host))
                mac = normalize_mac(self._discovered_mac or user_input.get("mac", self._mac))
                origin = normalize_origin(user_input.get("ha_origin"))
                existing = self._entry or next((e for e in self._async_current_entries() if e.unique_id == mac), None)
                if existing is not None and existing.data.get("transport") != COMMISSIONING_TRANSPORT:
                    raise PanelError("commissioning_already_bound")
                if existing is None and await journal(self.hass).get(mac) is not None:
                    self._host, self._mac = host, mac
                    return await self.async_step_recover()
                await self._prepare_commissioning(host, mac, origin,
                    credential=validate_token(existing.data.get("configuration_token")) if existing else None,
                    binding_id=validate_token(existing.data.get("binding_id")) if existing else None)
            except PanelError as error:
                errors["base"] = error.code
            else:
                if self._entry is not None:
                    return await self.async_step_reconfigure()
                return await self._start_pairing()
        fields = {}
        if not self._discovered_mac or self._entry is not None:
            fields[vol.Required("host", default=self._host)] = str
        if not self._discovered_mac and self._entry is None:
            fields[vol.Required("mac", default=self._mac)] = str
        fields[vol.Required("ha_origin", default=self._default_origin() if self._entry else self._origin or self._default_origin())] = str
        fields[vol.Required("confirm", default=False)] = bool
        return self.async_show_form(step_id="auto", errors=errors, data_schema=vol.Schema(fields))

    async def async_step_recover(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                if user_input.get("confirm") is not True:
                    raise PanelError("commissioning_confirmation_required")
                mac = normalize_mac(self._discovered_mac or user_input.get("mac", self._mac))
                record = await journal(self.hass).get(mac)
                if record is None:
                    raise PanelError("recovery_not_found")
                data = record["entry_data"]
                host = validate_host(user_input.get("host", self._discovered_host or self._host or data["host"]))
                if self.removed:
                    return self.async_abort(reason="cancelled")
                self._check_reconnect_entry()
                existing = next((e for e in self._async_current_entries() if e.unique_id == mac), None)
                if existing is not None:
                    if (existing.domain != DOMAIN or existing.data.get("model") != MODEL
                            or existing.data.get("transport") != COMMISSIONING_TRANSPORT
                            or normalize_origin(existing.data.get("ha_origin", "")) != data["ha_origin"]):
                        raise PanelError("identity_mismatch")
                    if self._entry is None:
                        existing.async_start_reauth(self.hass, context={"reconnect_host": host})
                        return self.async_abort(reason="reconnect_required")
                    if existing is not self._entry:
                        raise PanelError("identity_mismatch")
                    self._check_reconnect_entry()
                if user_input.get("restart_uncommitted") is True:
                    # A journal may have reached disk before an interrupted
                    # commit was sent. Restart only after authenticated proof
                    # that the screen is still unbound and its window is open.
                    if existing is not None:
                        return await self.async_step_reconnect()
                    await self._prepare_commissioning(host, mac, data["ha_origin"], recovering=True)
                    self._abort_if_unique_id_configured()
                    return await self._start_pairing()
                await self._prepare_commissioning(host, mac, data["ha_origin"],
                    credential=data["configuration_token"], binding_id=data["binding_id"], recovering=True)
                binding = await self.client.rpc("binding", {})
                match_binding(record, binding)
                if self.removed:
                    return self.async_abort(reason="cancelled")
                if existing is not None:
                    self._check_reconnect_entry()
                    self._binding_verified = True
                    return self.async_update_reload_and_abort(existing, data_updates=self._commissioning_entry_data(),
                        reason="reauth_successful" if self.context.get("source") == "reauth" else "reconnect_successful")
                self._abort_if_unique_id_configured(updates={"host": host, "port": COMMISSIONING_PORT})
                self._binding_verified = True
                return self.async_create_entry(title=self._title, data=self._commissioning_entry_data())
            except PanelError as error:
                errors["base"] = error.code
        fields = {vol.Required("host", default=self._discovered_host or self._host): str}
        if not self._discovered_mac:
            fields[vol.Required("mac", default=self._mac)] = str
        fields[vol.Required("confirm", default=False)] = bool
        fields[vol.Optional("restart_uncommitted", default=False)] = bool
        return self.async_show_form(step_id="recover", errors=errors, data_schema=vol.Schema(fields))

    async def _confirmation(self):
        if self._bound:
            return await self.async_step_reconfigure()
        return await self.async_step_confirm()

    @staticmethod
    def _entry_scope(data):
        return {key: data.get(key) for key in
                ("mac", "model", "ha_origin", "transport", "configuration_token", "binding_id")}

    def _reconnect_entry(self, entry):
        if entry is None:
            raise PanelError("identity_mismatch")
        mac = normalize_mac(entry.data.get("mac", ""))
        if (entry.entry_id != self.context.get("entry_id") or entry.domain != DOMAIN
                or entry.unique_id != mac or entry.data.get("model") != MODEL
                or entry.data.get("transport") != COMMISSIONING_TRANSPORT):
            raise PanelError("identity_mismatch")
        validate_token(entry.data.get("configuration_token"))
        validate_token(entry.data.get("binding_id"))
        self._entry, self._mac = entry, mac
        self._origin = normalize_origin(entry.data.get("ha_origin", ""))
        self._host = validate_host(self.context.get("reconnect_host", entry.data.get("host", "")))
        self._title = entry.title
        self._reconnect_scope = self._entry_scope(entry.data)

    def _check_reconnect_entry(self):
        if self._reconnect_scope is not None:
            current = self.hass.config_entries.async_get_entry(self._entry.entry_id)
            if (current is not self._entry or current.unique_id != self._mac
                    or current.domain != DOMAIN or self._entry_scope(current.data) != self._reconnect_scope):
                raise PanelError("binding_changed")

    async def async_step_reauth(self, entry_data):
        # Context identifies the existing entry; passed data is never authority.
        try:
            self._reconnect_entry(self._get_reauth_entry())
        except PanelError as error:
            return self.async_abort(reason=error.code)
        return await self.async_step_reconnect()

    async def async_step_reconnect(self, user_input=None):
        errors = {}
        try:
            self._check_reconnect_entry()
            if self._reconnect_scope is None:
                raise PanelError("identity_mismatch")
            host = validate_host(user_input.get("host", self._host) if user_input is not None else self._host)
            info = await verified_unbound_info(host, self._mac)
            if self.removed:
                raise PanelError("cancelled")
            self._host, self._title = host, info["friendly_name"] or self._title
            if user_input is not None:
                if user_input.get("confirm_reconnect") is not True:
                    raise PanelError("reconnect_confirmation_required")
                self._check_reconnect_entry()
                self._reconnect_confirmed = True
                # Fresh info on this connection catches a binding/maintenance
                # race after the public probe; no old credential is sent.
                await self._prepare_commissioning(host, self._mac, self._origin, reconnecting=True)
                self._check_reconnect_entry()
                return await self._start_pairing()
        except PanelError as error:
            if error.code == "commissioning_already_bound" and self._reconnect_scope is not None:
                # A fresh binding may be our previous commit whose response was
                # lost. Offer explicit read-only recovery on this same entry;
                # the journal credential must still prove the entire binding.
                try:
                    record = await journal(self.hass).get(self._mac)
                    if (record is not None and record["entry_data"]["mac"] == self._mac
                            and record["entry_data"]["ha_origin"] == self._origin
                            and self._entry_scope(record["entry_data"]) != self._reconnect_scope):
                        return await self.async_step_recover()
                except PanelError as failure:
                    error = failure
            errors["base"] = error.code
        return self.async_show_form(step_id="reconnect", errors=errors, data_schema=vol.Schema({
            vol.Required("host", default=self._host): str,
            vol.Required("confirm_reconnect", default=False): bool,
        }), description_placeholders={"name": self._title, "origin": self._origin})

    async def async_step_zeroconf(self, discovery_info):
        try:
            properties = discovery_info.properties
            mac = normalize_mac(properties.get("mac", ""))
            if (properties.get("model") != MODEL or properties.get("proto") != "1"
                    or discovery_info.port != PORT):
                return self.async_abort(reason="identity_mismatch")
            self._host = validate_host(discovery_info.host)
            self._mac = self._discovered_mac = mac
            self._discovered_host = self._host
            self.context["title_placeholders"] = {"name": f"AD35 {mac[-6:].upper()}"}
        except PanelError as error:
            return self.async_abort(reason=error.code)
        try:
            # Reserve the flow identity without trusting it enough to mutate an
            # entry. The encrypted checks in _prepare are still mandatory.
            await self.async_set_unique_id(mac)
            entry = next((entry for entry in self._async_current_entries()
                          if entry.unique_id == mac), None)
            if entry is not None:
                if entry.data.get("transport") == COMMISSIONING_TRANSPORT:
                    await self._prepare_commissioning(self._host, mac, entry.data.get("ha_origin"),
                        credential=validate_token(entry.data.get("configuration_token")), binding_id=validate_token(entry.data.get("binding_id")))
                else:
                    config, reference = await self._entry_config(entry)
                    await self._prepare(self._host, mac, config, psk_reference=reference)
        except PanelError as error:
            if entry is not None and entry.data.get("transport") == COMMISSIONING_TRANSPORT:
                if error.code == "invalid_credential":
                    try:
                        await verified_unbound_info(self._host, mac)
                    except PanelError:
                        pass
                    else:
                        if not self.removed:
                            entry.async_start_reauth(self.hass, context={"reconnect_host": self._host})
                            return self.async_abort(reason="reconnect_required")
                # An existing entry must not leave an unrelated new-entry flow
                # reserved for this MAC, blocking its Core-deduplicated reauth.
                return self.async_abort(reason=error.code)
            return await self.async_step_credentials(_error=error.code)
        if (properties.get("commissioning") == COMMISSIONING_TRANSPORT
                and str(properties.get("commissioning_port", "")) == str(COMMISSIONING_PORT)):
            try:
                if await journal(self.hass).get(mac) is not None:
                    return await self.async_step_recover()
            except PanelError as error:
                return self.async_abort(reason=error.code)
            return await self.async_step_auto()
        if (properties.get("bootstrap") == "security2-v1"
                and str(properties.get("bootstrap_port", "")) == "8081"):
            return await self.async_step_pin_start()
        return await self.async_step_pairing_file()

    def _credential_schema(self, *, manual):
        fields = {vol.Required("host", default=self._host): str}
        if manual:
            fields[vol.Required("mac")] = str
        # In particular, do not use add_suggested_values_to_schema for this
        # form: it would put a failed or stored key back into the response.
        fields[vol.Required("noise_psk")] = selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD),
        )
        fields[vol.Required("ha_origin", default=self._origin or self._default_origin())] = str
        return vol.Schema(fields)

    async def _credentials(self, step, user_input=None, *, error=None):
        errors = {"base": error} if error else {}
        if user_input is not None:
            try:
                mac = normalize_mac(user_input.get("mac", "") if step == "manual" else self._mac)
                host = validate_host(user_input.get("host", self._host))
                config = build_device_config(user_input.get("noise_psk"), user_input.get("ha_origin"))
                await self._prepare(host, mac, config)
            except PanelError as failure:
                errors["base"] = failure.code
            else:
                return await self._confirmation()
        return self.async_show_form(
            step_id=step, data_schema=self._credential_schema(manual=step == "manual"),
            errors=errors, description_placeholders={"mac": self._mac, "host": self._host},
        )

    async def async_step_user(self, user_input=None):
        # Keep direct manual submissions compatible with the previous flow API.
        if user_input and "noise_psk" in user_input:
            return await self.async_step_manual(user_input)
        return self.async_show_menu(step_id="user", menu_options=["auto", "recover", "pin_start", "pairing_file", "manual"])

    async def async_step_manual(self, user_input=None):
        self._pin_credentials = self._pin_verification_only = False
        return await self._credentials("manual", user_input)

    async def async_step_credentials(self, user_input=None, *, _error=None):
        return await self._credentials("credentials", user_input, error=_error)

    async def async_step_pin_start(self, user_input=None, *, _error=None):
        errors = {"base": _error} if _error else {}
        if user_input is not None:
            try:
                host = validate_host(self._discovered_host or user_input.get("host", ""))
                mac = normalize_mac(self._discovered_mac or user_input.get("mac", ""))
                origin = normalize_origin(user_input.get("ha_origin"))
                if self.removed:
                    return self.async_abort(reason="cancelled")
                if self._bootstrap is not None:
                    await self._bootstrap.close()
                self._bootstrap = BootstrapClient(host, mac)
                await self._bootstrap.start()
                if self.removed:
                    await self._bootstrap.close()
                    return self.async_abort(reason="cancelled")
                self._host, self._mac, self._origin = host, mac, origin
            except PanelError as error:
                if self._bootstrap is not None:
                    await self._bootstrap.close()
                errors["base"] = error.code
            else:
                return await self.async_step_pin()
        fields = {}
        if not self._discovered_mac:
            fields[vol.Required("host", default=self._host)] = str
            fields[vol.Required("mac", default=self._mac)] = str
        fields[vol.Required("ha_origin", default=self._origin or self._default_origin())] = str
        return self.async_show_form(step_id="pin_start", errors=errors, data_schema=vol.Schema(fields),
                                    description_placeholders={"mac": self._discovered_mac or self._mac})

    async def async_step_pin(self, user_input=None):
        errors = {}
        if self._bootstrap is None:
            return await self.async_step_pin_start(_error="bootstrap_expired")
        if user_input is not None:
            try:
                pin = validate_pin(user_input.get("pin"))
            except PanelError as error:
                errors["base"] = error.code
            else:
                try:
                    credentials = await self._bootstrap.claim(pin)
                except PanelError as error:
                    await self._bootstrap.close()
                    self._bootstrap = None
                    return await self.async_step_pin_start(_error=error.code)
                if self.removed:
                    await self._bootstrap.close()
                    self._bootstrap = None
                    return self.async_abort(reason="cancelled")
                await self._bootstrap.close()
                self._bootstrap = None
                self._pin_credentials = True
                self._pin_verification_only = any(
                    entry.unique_id == self._mac for entry in self._async_current_entries()
                )
                self._imported = credentials
                # Credentials remain only in RAM until the existing OAuth and
                # explicit entity-selection transaction commits successfully.
                return await self._connect_imported(self._host, self._origin)
        return self.async_show_form(step_id="pin", errors=errors, data_schema=vol.Schema({
            vol.Required("pin"): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD),
            ),
        }))

    async def async_step_pairing_file(self, user_input=None):
        self._pin_credentials = self._pin_verification_only = False
        errors = {}
        if user_input is not None:
            # Unlike a progress continuation {}, an uploaded-file submission
            # carries a required one-use file identifier, never a default.
            if not isinstance(user_input.get("pairing_file"), str):
                errors["base"] = "invalid_pairing_file"
            else:
                self._imported = None
                try:
                    imported = await self.hass.async_add_executor_job(
                        load_uploaded_pairing_file, self.hass, user_input["pairing_file"],
                    )
                    if self.removed:
                        return self.async_abort(reason="cancelled")
                    if self._discovered_mac and imported.mac != self._discovered_mac:
                        raise PanelError("identity_mismatch")
                except PanelError as error:
                    errors["base"] = error.code
                else:
                    self._imported = imported
                    self._mac = imported.mac
                    # A pairing file never overrides an address just discovered
                    # for this MAC. An explicit connection retry may edit it.
                    self._host = self._discovered_host or imported.host
                    return await self._connect_imported(self._host, user_input.get("ha_origin"))
        return self.async_show_form(
            step_id="pairing_file", errors=errors, last_step=False, data_schema=vol.Schema({
                vol.Required("pairing_file"): selector.FileSelector(
                    selector.FileSelectorConfig(accept=".ad35,.json,application/json"),
                ),
                vol.Required("ha_origin", default=self._origin or self._default_origin()): str,
            }),
        )

    async def _connect_imported(self, host, origin):
        if self._imported is None:
            return self.async_abort(reason="invalid_pairing_file")
        try:
            config = build_device_config(self._imported.noise_psk, origin)
            await self._prepare(host, self._imported.mac, config)
        except PanelError as error:
            if self.removed:
                return self.async_abort(reason="cancelled")
            return await self.async_step_connection(_error=error.code)
        if self._pin_verification_only:
            return self.async_abort(reason="bootstrap_verified")
        if self._bound:
            return await self._confirmation()
        # Choosing the private file and submitting its form is explicit consent
        # to configure an unbound screen. Do not add a redundant blank form.
        return await self._start_pairing()

    async def async_step_connection(self, user_input=None, *, _error=None):
        if self._imported is None:
            return self.async_abort(reason="invalid_pairing_file")
        if user_input is not None:
            if user_input.get("replace_pairing_file") is True:
                if self.client is not None:
                    await self.client.close()
                    self.client = None
                self._imported = self._config = None
                self._psk_reference = self._bound = self._recover_bound = False
                # Only discovery or an existing HA entry constrains replacement
                # identity. The previous file itself is never a trusted source.
                self._mac = self._discovered_mac or (self._entry.unique_id if self._entry else "")
                self._host = self._discovered_host or (self._entry.data.get("host", "") if self._entry else "")
                if self._entry is not None:
                    self._origin = normalize_origin(self._entry.data.get("ha_origin", ""))
                return await self.async_step_pairing_file()
            return await self._connect_imported(user_input.get("host"), user_input.get("ha_origin"))
        return self.async_show_form(
            step_id="connection", errors={"base": _error} if _error else {},
            data_schema=vol.Schema({
                vol.Required("host", default=self._host): str,
                vol.Required("ha_origin", default=self._origin or self._default_origin()): str,
                vol.Optional("replace_pairing_file", default=False): bool,
            }),
        )

    async def _start_pairing(self):
        if self.removed:
            return self.async_abort(reason="cancelled")
        if self.client is None or (self._config is None and self._transport != COMMISSIONING_TRANSPORT):
            return self.async_abort(reason="cannot_connect")
        self.pairing = PairingSession(
            self.client, self._origin, self._origin + CLIENT_PATH + self._mac,
            operation="reconfigure" if self._bound else "configure",
        )
        return await self.async_step_wait_screen()

    async def async_step_confirm(self, user_input=None):
        if user_input is not None:
            return await self._start_pairing()
        return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}),
                                    description_placeholders={"origin": self._origin, "name": self._title})

    async def async_step_reconfigure(self, user_input=None):
        if self._entry is None and not self._recover_bound:
            try:
                entry = self._get_reconfigure_entry()
                mac = normalize_mac(entry.data.get("mac", ""))
                if (entry.entry_id != self.context.get("entry_id") or entry.domain != DOMAIN
                        or entry.unique_id != mac):
                    raise PanelError("identity_mismatch")
                self._entry = entry
                self._mac = mac
                self._host = validate_host(entry.data.get("host", ""))
                self._origin = normalize_origin(entry.data.get("ha_origin", ""))
                if entry.data.get("transport") == COMMISSIONING_TRANSPORT:
                    await self._prepare_commissioning(self._host, mac, self._origin,
                        credential=validate_token(entry.data.get("configuration_token")), binding_id=validate_token(entry.data.get("binding_id")))
                else:
                    config, reference = await self._entry_config(entry)
                    await self._prepare(self._host, mac, config, psk_reference=reference)
            except PanelError as error:
                if self._entry is not None and self._mac and self._host:
                    if self._entry.data.get("transport") == COMMISSIONING_TRANSPORT:
                        if error.code in {"invalid_credential", "not_bound"}:
                            try:
                                self._reconnect_entry(self._entry)
                            except PanelError as failure:
                                return self.async_abort(reason=failure.code)
                            return await self.async_step_reconnect()
                        return await self.async_step_auto(_error=error.code)
                    return await self.async_step_credentials(_error=error.code)
                return self.async_abort(reason=error.code)
            # Display the confirmation even for an empty HA continuation input.
            user_input = None
        if user_input is not None:
            return await self.async_step_confirm(user_input)
        return self.async_show_form(step_id="reconfigure", data_schema=vol.Schema({}),
                                    description_placeholders={"origin": self._origin, "name": self._title})

    async def _progress(self, step, action, worker, next_step):
        if step not in self._tasks:
            self._tasks[step] = self.hass.async_create_task(worker(), f"AD35 {step}")
        task = self._tasks[step]
        if not task.done():
            return self.async_show_progress(step_id=step, progress_action=action, progress_task=task)
        try:
            task.result()
        except PanelError as error:
            self._failure_reason = "save_uncertain" if step == "save" and self.pairing.commit_sent else error.code
            return self.async_show_progress_done(next_step_id="failure")
        except (Exception, asyncio.CancelledError):
            self._failure_reason = "save_uncertain" if step == "save" else "cannot_connect"
            return self.async_show_progress_done(next_step_id="failure")
        return self.async_show_progress_done(next_step_id=next_step)

    async def async_step_failure(self, user_input=None):
        return self.async_abort(reason=self._failure_reason)

    async def async_step_wait_screen(self, user_input=None):
        return await self._progress("wait_screen", "wait_screen", self.pairing.wait_local, "auth")

    async def async_step_auth(self, user_input=None):
        if self._oauth_received:
            if self._oauth_denied:
                self._failure_reason = "authorization_denied"
                return self.async_external_step_done(next_step_id="failure")
            return self.async_external_step_done(next_step_id="catalog")
        if self._oauth_url is None:
            state = self._registry.issue(self)
            self._oauth_url = self._origin + "/auth/authorize?" + urlencode({
                "response_type": "code", "client_id": self.pairing.client_id,
                "redirect_uri": self._origin + CALLBACK_PATH, "state": state,
            })
        return self.async_external_step(step_id="auth", url=self._oauth_url)

    @callback
    def receive_authorization(self, code):
        self._code = code
        self._oauth_received = True
        self._oauth_denied = code is None
        self._oauth_url = None

    @callback
    def clear_authorization(self):
        self._code = None
        self._oauth_url = None

    async def _authorize(self):
        code, self._code = self._code, None
        await self.pairing.authorize(code)

    async def async_step_catalog(self, user_input=None):
        return await self._progress("catalog", "load_catalog", self._authorize, "select")

    def _home_order(self, entities, user_input):
        """Explicit slots first; blanks retain persisted order, then new selections."""
        # The selected set is authoritative. Clearing it must not require
        # clearing six stale ordering selectors left in the current form.
        if not entities:
            return []
        # Membership sets are only lookup aids: neither a set nor the candidate
        # catalog is ever used as the source of the persisted display order.
        selected = set(entities)
        current = [item for item in self._previous_selected_ids if item in selected]
        current.extend(item for item in entities if item not in current)
        slots = [user_input.get(f"home_slot_{index + 1}", "") for index in range(6)]
        if any(not isinstance(item, str) or item and item not in selected for item in slots):
            raise PanelError("home_order_not_selected")
        explicit = [item for item in slots if item]
        if len(explicit) != len(set(explicit)):
            raise PanelError("home_order_duplicate")
        if any(slots[min(6, len(current)):]):
            raise PanelError("home_order_position_unavailable")
        remaining = [item for item in current if item not in explicit]
        ordered = []
        for item in slots[:min(6, len(current))]:
            ordered.append(item if item else remaining.pop(0))
        return ordered + remaining

    async def async_step_select(self, user_input=None):
        errors = {}
        if not self._selection_presented:
            # Core forwards progress continuation input (often {}) to the next
            # step. It is not a selection submission: show the form first.
            self._selection_presented = True
            user_input = None
        if user_input is not None:
            try:
                if user_input.get("confirm_selection") is not True:
                    raise PanelError("selection_confirmation_required")
                entities = user_input.get("entity_ids", [])
                weather = user_input.get("weather_id", "")
                if self._weather_optional and weather is None:
                    weather = ""
                self.pairing.validate_selection(entities, weather)
                ordered = self._home_order(entities, user_input)
            except PanelError as error:
                errors["base"] = error.code
            else:
                self._selection = (ordered, weather)
                self._selection_confirmed = True
                return await self.async_step_save()
        selectable_options, weather = [], [{"value": "", "label": "不选择天气 / No weather"}]
        chinese = getattr(self.hass.config, "language", "en").startswith("zh")
        domains = {"light": "灯光", "switch": "开关", "cover": "窗帘", "climate": "空调", "sensor": "传感器",
                   "binary_sensor": "状态传感器", "lock": "门锁", "camera": "摄像头", "vacuum": "扫地机器人", "weather": "天气"} if chinese else {
            "light": "Light", "switch": "Switch", "cover": "Cover", "climate": "Climate", "sensor": "Sensor",
            "binary_sensor": "Binary sensor", "lock": "Lock", "camera": "Camera", "vacuum": "Vacuum", "weather": "Weather"}
        names = Counter(item["name"] for item in self.pairing.catalog)
        for item in self.pairing.catalog:
            label = f'{item["name"]} · {domains[item["domain"]]}'
            if names[item["name"]] > 1:
                label += f' ({item["entity_id"]})'
            option = {"value": item["entity_id"], "label": label}
            (selectable_options if item["domain"] in SELECTABLE_DOMAINS else weather).append(option)
        local = self._catalog_is_local()
        control_selector = selector.SelectSelector(selector.SelectSelectorConfig(
            options=selectable_options, multiple=True, mode=selector.SelectSelectorMode.DROPDOWN))
        weather_selector = selector.SelectSelector(selector.SelectSelectorConfig(
            options=weather, mode=selector.SelectSelectorMode.DROPDOWN))
        if local and selectable_options:
            control_selector = selector.EntitySelector(selector.EntitySelectorConfig(
                include_entities=[item["value"] for item in selectable_options], multiple=True,
                filter={"domain": sorted(SELECTABLE_DOMAINS)}))
        self._weather_optional = local and len(weather) > 1
        if self._weather_optional:
            weather_selector = selector.EntitySelector(selector.EntitySelectorConfig(
                include_entities=[item["value"] for item in weather[1:]], multiple=False,
                filter={"domain": "weather"}))
        allowed = {item["value"] for item in selectable_options}
        previous = [item for item in self._previous_selected_ids if item in allowed]
        weather_values = {item["value"] for item in weather}
        previous_weather = self._previous_weather_id if self._previous_weather_id in weather_values else ""
        # Suggestions preselect weather without reinserting it when the user
        # clears either optional selector and Core submits no field.
        weather_field = (vol.Optional("weather_id", description={"suggested_value": previous_weather})
                         if previous_weather else vol.Optional("weather_id"))
        # Preselect existing items in the UI only. If the optional selector is
        # cleared and omitted from the submission, preserve that empty choice
        # instead of restoring the old list through a schema default.
        schema = {
            vol.Optional("entity_ids", description={"suggested_value": previous}): control_selector,
            weather_field: weather_selector,
        }
        home_selector = selector.SelectSelector(selector.SelectSelectorConfig(
            options=[{"value": "", "label": "自动补齐 · 保留当前顺序" if chinese else "Automatic · keep current order"},
                     *selectable_options], mode=selector.SelectSelectorMode.DROPDOWN))
        # Empty defaults never retain a stale entity when the selection changes.
        # Their meaning is the current authenticated binding order, not catalog
        # order. Flat optional fields also preserve existing submission payloads.
        for index in range(6):
            schema[vol.Optional(f"home_slot_{index + 1}", default="")] = home_selector
        schema[vol.Required("confirm_selection", default=False)] = bool
        return self.async_show_form(step_id="select", errors=errors, data_schema=vol.Schema(schema))

    async def _save(self):
        if not self._selection_confirmed or self._selection is None:
            raise PanelError("selection_confirmation_required")
        self._check_reconnect_entry()
        if self._transport != COMMISSIONING_TRANSPORT:
            await self.pairing.commit(*self._selection)
            return
        first = not self._bound
        if first:
            self._configuration_token = secrets.token_hex(32)
            self._binding_id = self.pairing.request_id
        record = {
            "entry_data": self._commissioning_entry_data(),
            "entity_ids": self._selection[0], "weather_id": self._selection[1],
            "expected_revision": self._binding_revision + 1 if self._bound else 0,
        }
        await journal(self.hass).put(record)
        if self.removed:
            raise PanelError("cancelled")
        self._check_reconnect_entry()
        self._recovery_record = record
        await self.pairing.commit(*self._selection,
            configuration_token=self._configuration_token if first else None)
        self.client.credential = self._configuration_token
        binding = await self.client.rpc("binding", {})
        match_binding(record, binding)
        self._binding_verified = True

    async def async_step_save(self, user_input=None):
        return await self._progress("save", "save", self._save, "finish")

    async def async_step_finish(self, user_input=None):
        if (not self._selection_confirmed or self.pairing is None
                or self.pairing.phase != "committed"):
            return self.async_abort(reason="save_uncertain")
        if self._transport == COMMISSIONING_TRANSPORT:
            if not self._binding_verified:
                return self.async_abort(reason="save_uncertain")
            data = self._commissioning_entry_data()
            if self._entry is not None:
                try:
                    self._check_reconnect_entry()
                except PanelError as error:
                    return self.async_abort(reason=error.code)
                return self.async_update_reload_and_abort(self._entry, data_updates=data,
                    reason=("reauth_successful" if self.context.get("source") == "reauth" else "reconnect_successful")
                    if self._reconnect_scope is not None else "reconfigure_successful")
            return self.async_create_entry(title=self._title, data=data)
        data = {
            "mac": self._mac, "host": self._host, "port": PORT,
            "ha_origin": self._origin, "name": self.client.name,
            "friendly_name": self._title, "model": MODEL, "firmware": self.client.version,
        }
        if self._psk_reference:
            data["psk_reference"] = ALLOWLIST_FILE
        else:
            data["noise_psk"] = self._config.noise_psk
        if self._entry is not None:
            return self.async_update_reload_and_abort(self._entry, data_updates=data,
                                                       reason="reconfigure_successful")
        return self.async_create_entry(title=self._title, data=data)

    @callback
    def async_remove(self):
        if self.removed:
            return
        self.removed = True
        self.clear_authorization()
        if self._registry is not None:
            self._registry.discard(self)
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        self.hass.async_create_task(self._close(), "AD35 flow cleanup")

    async def _close(self):
        if self._bootstrap is not None:
            await self._bootstrap.close()
        self._imported = None
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        if self.pairing is not None:
            await self.pairing.close()
        elif self.client is not None:
            await self.client.close()
