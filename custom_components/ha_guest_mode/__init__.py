import sqlite3
from datetime import timedelta, datetime, timezone
from typing import Any
import voluptuous as vol
import jwt
import os
import aiofiles
from homeassistant.core import HomeAssistant
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
from homeassistant.helpers.typing import ConfigType
from homeassistant.config_entries import ConfigEntry
from homeassistant.components import websocket_api
from homeassistant.util import dt as dt_util
from homeassistant.components.panel_custom import async_register_panel
from homeassistant.helpers import config_validation as cv

from .validateTokenView import ValidateTokenView
from .keyManager import KeyManager
from .const import DOMAIN, DATABASE

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)

@websocket_api.websocket_command({vol.Required("type"): "ha_guest_mode/list_users"})
@websocket_api.require_admin
@websocket_api.async_response
async def list_users(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    result = []
    now = dt_util.utcnow()

    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM tokens')
    tokenLists = cursor.fetchall()    

    for user in await hass.auth.async_get_users():
        ha_username = next((cred.data.get("username") for cred in user.credentials if cred.auth_provider_type == "homeassistant"), None)

        tokens = []
        for token in tokenLists[:]:
            if  datetime.fromisoformat(token[4]).replace(tzinfo=timezone.utc) < now:
                if token[6] != "":
                    # @Todo maybe try catch here if token are already deleted
                    tokenHa = hass.auth.async_get_refresh_token(token[5])
                    hass.auth.async_remove_refresh_token(tokenHa)
                cursor.execute('DELETE FROM tokens WHERE id = ?', (token[0],))
                tokenLists.remove(token)
            if token[1] == user.id:
                tokens.append({
                    "id": token[0],
                    "name": token[2],
                    "jwt_token": token[7],
                    "type": TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
                    "end_date": token[4],
                    "remaining": int((datetime.fromisoformat(token[4]).replace(tzinfo=timezone.utc) - now).total_seconds()),
                    "start_date": token[3],
                    "isUsed": token[6] != ""
                })

        result.append({
            "id": user.id,
            "username": ha_username,
            "name": user.name,
            "is_owner": user.is_owner,
            "is_active": user.is_active,
            "local_only": user.local_only,
            "system_generated": user.system_generated,
            "group_ids": [group.id for group in user.groups],
            "credentials": [{"type": c.auth_provider_type} for c in user.credentials],
            "tokens": tokens,
        })

    conn.commit()
    conn.close()
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "ha_guest_mode/create_token",
        vol.Required("user_id"): str,
        vol.Required("name"): str, # token name
        vol.Required("startDate"): int, # minutes
        vol.Required("expirationDate"): int, # minutes
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def create_token(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    try:
        now = datetime.now()
        startDate = now + timedelta(minutes=msg["startDate"])
        endDate = now + timedelta(minutes=msg["expirationDate"])
        
        private_key = hass.data.get("private_key")
        if private_key is None:
            connection.send_message(msg["id"],  websocket_api.const.ERR_NOT_FOUND, "private key not found")
        tokenGenerated = jwt.encode({"id": msg["id"],"startDate": startDate.isoformat(), "endDate": endDate.isoformat()}, private_key, algorithm="RS256")

        query = """
            INSERT INTO tokens (userId, token_name, start_date, end_date, token_ha_id, token_ha, token_ha_guest_mode)
            values (?, ?, ?, ?, ?, ?, ?)
        """
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        cursor.execute(query, (msg["user_id"], msg["name"], startDate.isoformat(), endDate.isoformat(), "", "", tokenGenerated))
        conn.commit()
        conn.close()

    except ValueError as err:
        connection.send_message(
            websocket_api.error_message(msg["id"], websocket_api.const.ERR_UNKNOWN_ERROR, str(err))
        )
        return

    connection.send_result(msg["id"], tokenGenerated)

@websocket_api.websocket_command(
    {
        vol.Required("type"): "ha_guest_mode/delete_token",
        vol.Required("token_id"): int
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def delete_token(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    # @Todo add try catch to catch error with slqite
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute('SELECT token_ha_id FROM tokens WHERE id = ?', (msg["token_id"],))
    token = cursor.fetchone()    

    if token[0] != "":
        tokenHa = hass.auth.async_get_refresh_token(token[0])
        hass.auth.async_remove_refresh_token(tokenHa)
    
    cursor.execute('DELETE FROM tokens WHERE id = ?', (msg["token_id"],))
    conn.commit()
    conn.close()
    connection.send_result(msg["id"], True)

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    websocket_api.async_register_command(hass, list_users)
    websocket_api.async_register_command(hass, create_token)
    websocket_api.async_register_command(hass, delete_token)

    key_manager = KeyManager()
    await key_manager.load_or_generate_key()
    hass.data["private_key"] = key_manager.get_private_key()
    hass.data["public_key"] = key_manager.get_public_key()

    hass.http.register_view(ValidateTokenView(hass))

    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        userId TEXT NOT NULL,
        token_name TEXT NOT NULL,
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        token_ha_id INTERGER,
        token_ha TEXT,
        token_ha_guest_mode TEXT NOT NULL
    )
    """)

    connection.commit()
    connection.close()

    source_path = hass.config.path("custom_components/ha_guest_mode/www/ha-guest-mode.js")
    dest_dir = hass.config.path("www/community/ha-guest-mode")
    dest_path = os.path.join(dest_dir, "ha-guest-mode.js")

    try:
        if not os.path.exists(dest_dir):
            os.makedirs(dest_dir)

        if os.path.exists(source_path):
            await async_copy_file(source_path, dest_path)

    except Exception as e:
        return False

    return True

async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Set up ha_guest_mode from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    tab_icon = config_entry.options.get("tab_icon", config_entry.data.get("tab_icon", "mdi:shield-key"))
    tab_name = config_entry.options.get("tab_name", config_entry.data.get("tab_name", "Guest"))
    path = config_entry.options.get("path_to_admin_ui", config_entry.data.get("path_to_admin_ui", "/guest-mode"))
    if path.startswith("/"):
        path = path[1:]

    panels = hass.data.get("frontend_panels", {})
    if path in panels:
        hass.components.frontend.async_remove_panel(path)

    hass.async_create_task(
        async_register_panel(
            hass,
            frontend_url_path=path,
            webcomponent_name="guest-mode-panel",
            module_url="/local/community/ha-guest-mode/ha-guest-mode.js",
            sidebar_title=tab_name,
            sidebar_icon=tab_icon,
            require_admin=True,
        )
    )

    return True

async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry):
    """Unload a config entry."""
    path = config_entry.options.get("path_to_admin_ui", config_entry.data.get("path_to_admin_ui", "/guest-mode"))
    if path.startswith("/"):
        path = path[1:]

    panels = hass.data.get("frontend_panels", {})
    if path in panels:
        hass.components.frontend.async_remove_panel(path)
    return True

async def async_copy_file(source_path, dest_path):
    async with aiofiles.open(source_path, 'rb') as src, aiofiles.open(dest_path, 'wb') as dst:
        while chunk := await src.read(1024):  # Adjust chunk size as needed
            await dst.write(chunk)