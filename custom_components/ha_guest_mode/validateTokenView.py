import jwt
import sqlite3
from datetime import timedelta, datetime
from aiohttp import web

from homeassistant.core import HomeAssistant
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.translation import async_get_translations

from .const import DATABASE

class ValidateTokenView(HomeAssistantView):
    url = "/guest-mode/login"
    name = "guest-mode:login"
    requires_auth = False

    def __init__(self, hass: HomeAssistant):
        self.hass = hass

    def get_translatons(translations, label: str):
        return translations["entity"]["guest_error"][label]["name"]

    async def get(self, request):
        language = self.hass.config.language 
        translations = await async_get_translations(self.hass, language)

        token_param = request.query.get("token")
        if not token_param:
            return web.Response(status=400, text=self.get_translatons(translations, "missing_token"))

        try:
            public_key = self.hass.data.get("public_key")
            if public_key is None:
                return web.Response(status=500, text=self.get_translatons(translations, "internal_server_error"))
            decoded_token = jwt.decode(token_param, public_key, algorithms=["RS256"])
            start_date = datetime.fromisoformat(decoded_token.get("startDate"))
            end_date = datetime.fromisoformat(decoded_token.get("endDate"))
        except jwt.ExpiredSignatureError:
            return web.Response(status=401, text=self.get_translatons(translations, "expired_token"))
        except jwt.InvalidTokenError:
            return web.Response(status=401, text=self.get_translatons(translations, "invalid_token"))
        except Exception as e:
            return web.Response(status=400, text=str(e))

        now = datetime.now()
        if now < start_date or now > end_date:
            return web.Response(status=403, text=self.get_translatons(translations, "not_yet_or_expired"))

        conn = sqlite3.connect(self.hass.config.path(DATABASE))
        cursor = conn.cursor()
        cursor.execute(
            'SELECT * FROM tokens WHERE token_ha_guest_mode = ?',
            (token_param,)
        )
        result = cursor.fetchone()

        if result is None:
            return web.Response(status=404, text=self.get_translatons(translations, "token_not_found"))
        
        token = result[6]
        
        if token == "" and now > start_date:
            users = await self.hass.auth.async_get_users()

            user = next((u for u in users if u.id == result[1]), None)
            if user is None:
                return web.Response(status=404, text=self.get_translatons(translations, "user_not_found"))
            endDateInSeconds = (end_date - now).total_seconds()
            refresh_token = await self.hass.auth.async_create_refresh_token(
                user,
                client_name=result[2],
                token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
                access_token_expiration=timedelta(seconds=endDateInSeconds),
            )
            token = self.hass.auth.async_create_access_token(refresh_token)

            query = """
                UPDATE tokens SET token_ha_id = ?, token_ha = ? WHERE id = ?
            """
            cursor.execute(query, (refresh_token.id, token, result[0]))
            conn.commit()


        conn.close()

        html_content = f"""
        <!DOCTYPE html>
        <html>
          <body>
            <script type="text/javascript">
              const hassUrl = window.location.protocol + '//' + window.location.host;
              const access_token = '{token}';
              localStorage.setItem('hassTokens', JSON.stringify({{ access_token: access_token, hassUrl: hassUrl }}));
              window.location.href = hassUrl;
            </script>
          </body>
        </html>
        """
        return web.Response(content_type="text/html", text=html_content)
