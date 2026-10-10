"""One-use OAuth callback; codes never enter HA's ordinary service bus."""

from __future__ import annotations

from dataclasses import dataclass, field
import secrets
import time

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from .const import CALLBACK_PATH, CLIENT_PATH, DOMAIN
from .protocol import PanelError, normalize_mac


@dataclass(repr=False)
class PendingAuthorization:
    flow: object = field(repr=False)
    request_id: str = field(repr=False)
    session: str = field(repr=False)
    mac: str
    origin: str
    client_id: str
    expires: float


class AuthorizationRegistry:
    """Memory only: bind state to this exact, still-active transaction."""

    def __init__(self):
        self.pending = {}

    def issue(self, flow) -> str:
        self.discard(flow)
        session = flow.pairing
        token = secrets.token_urlsafe(32)
        self.pending[token] = PendingAuthorization(
            flow, session.request_id, session.session, flow._mac, session.origin,
            session.client_id, session.deadline,
        )
        return token

    def consume(self, state: str):
        if not isinstance(state, str) or len(state) > 128:
            return None
        pending = self.pending.pop(state, None)
        if pending is None:
            return None
        flow = pending.flow
        pairing = flow.pairing
        if (flow.removed or time.monotonic() >= pending.expires or pairing is None
                or pairing.phase != "approved" or flow._mac != pending.mac
                or pairing.client.mac != pending.mac
                or pairing.request_id != pending.request_id or pairing.session != pending.session
                or pairing.origin != pending.origin or pairing.client_id != pending.client_id):
            return None
        return flow

    def discard(self, flow):
        for token, pending in list(self.pending.items()):
            if pending.flow is flow or time.monotonic() >= pending.expires:
                del self.pending[token]


def _response(text: str, status=200):
    return web.Response(text=text, status=status, headers={
        "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    })


class AuthorizationCallbackView(HomeAssistantView):
    requires_auth = False
    url = CALLBACK_PATH
    name = "api:ad35_ha_panel:auth_callback"

    def __init__(self, hass, registry):
        self.hass = hass
        self.registry = registry

    async def get(self, request):
        query = request.query
        if (any(len(query.getall(key)) != 1 for key in query)
                or "state" not in query or ("code" in query) == ("error" in query)
                or query.keys() - {"state", "code", "error", "error_description"}):
            return _response("Invalid authorization callback.", 400)
        code = query.get("code", "")
        if "code" in query and not 0 < len(code.encode()) <= 4096:
            return _response("Invalid authorization callback.", 400)
        flow = self.registry.consume(query["state"])
        if flow is None:
            return _response("Authorization expired or already received.", 400)
        # Only a boolean marker reaches flow-manager input. The code stays in RAM.
        flow.receive_authorization(code if "code" in query else None)
        try:
            await self.hass.config_entries.flow.async_configure(
                flow_id=flow.flow_id, user_input={"oauth_callback": True},
            )
        except Exception:
            flow.clear_authorization()
            return _response("Unable to resume setup. Return to Home Assistant.", 400)
        return _response("Authorization received. Close this tab and return to Home Assistant.")


class ClientMetadataView(HomeAssistantView):
    requires_auth = False
    url = CLIENT_PATH + "{mac}"
    name = "api:ad35_ha_panel:client"

    async def get(self, request, mac):
        try:
            if normalize_mac(mac) != mac:
                return _response("Not found.", 404)
        except PanelError:
            return _response("Not found.", 404)
        return web.Response(text='<html><head><title>AD35 HA Panel</title></head>'
                            '<body><h1 class="p-name">AD35 HA Panel</h1>'
                            f'<p>Local screen {mac[-6:].upper()} authorization.</p></body></html>',
                            content_type="text/html", headers={"Cache-Control": "no-store"})


def ensure_views(hass) -> AuthorizationRegistry:
    data = hass.data.setdefault(DOMAIN, {})
    if "oauth" not in data:
        data["oauth"] = registry = AuthorizationRegistry()
        hass.http.register_view(AuthorizationCallbackView(hass, registry))
        hass.http.register_view(ClientMetadataView())
    return data["oauth"]
