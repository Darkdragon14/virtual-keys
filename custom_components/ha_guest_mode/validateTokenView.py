import jwt
import sqlite3
from datetime import timedelta, datetime
from aiohttp import web
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

DATABASE = "/config/custom_components/ha_guest_mode/ha_guest_mode.db"
SECRET_KEY = "information"

class ValidateTokenView(HomeAssistantView):
    url = "/guest-mode/login"
    name = "guest-mode:login"
    requires_auth = False

    def __init__(self, hass: HomeAssistant):
        self.hass = hass

    async def get(self, request):
        token_param = request.query.get("token")
        if not token_param:
            return web.Response(status=400, text="Token is missing")

        try:
            public_key = self.hass.data.get("public_key")
            if public_key is None:
                return web.Response(status=500, text="Internal Server Error")
            decoded_token = jwt.decode(token_param, public_key, algorithms=["RS256"])
            start_date = datetime.fromisoformat(decoded_token.get("startDate"))
            end_date = datetime.fromisoformat(decoded_token.get("endDate"))
        except jwt.ExpiredSignatureError:
            return web.Response(status=401, text="Token has expired")
        except jwt.InvalidTokenError:
            return web.Response(status=401, text="Invalid token")
        except Exception as e:
            return web.Response(status=400, text=str(e))

        now = datetime.now()
        if now < start_date or now > end_date:
            return web.Response(status=403, text="Token not yet valid or expired")

        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        cursor.execute(
            'SELECT * FROM tokens WHERE token_ha_guest_mode = ?',
            (token_param,)
        )
        result = cursor.fetchone()

        if result is None:
            return web.Response(status=404, text="Token not found or invalid for this user")
        
        token = result[6]
        
        # @Todo case where token is not set
        if token == "" and now > start_date:
            users = await self.hass.auth.async_get_users()

            user = next((u for u in users if u.id == result[1]), None)
            if user is None:
                return web.Response(status=404, text="User not found or not active")
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
