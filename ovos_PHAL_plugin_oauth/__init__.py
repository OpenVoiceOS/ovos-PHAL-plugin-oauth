import os

import requests
from flask import Flask, request
from mycroft_bus_client import Message
from oauthlib.oauth2 import WebApplicationClient
from ovos_backend_client.database import OAuthApplicationDatabase, OAuthTokenDatabase
from ovos_plugin_manager.phal import PHALPlugin

os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

app = Flask(__name__)


class OAuthPluginValidator:
    @staticmethod
    def validate(config=None):
        """ this method is called before loading the plugin.
        If it returns False the plugin is not loaded.
        This allows a plugin to run platform checks"""
        return True


class OAuthPlugin(PHALPlugin):
    validator = OAuthPluginValidator

    def __init__(self, bus=None, config=None):
        super().__init__(bus=bus, name="ovos-PHAL-plugin-oauth", config=config)
        self.port = config.get("port", 36536)

        self.bus.on("oauth.register", self.handle_oauth_register)

        # trigger register events from oauth skills
        self.bus.emit(Message("oauth.ping"))

        self.oauth_skills = {}

    def handle_oauth_register(self, message):
        # these fields are app specific and provided by skills
        skill_id = message.data.get("skill_id")
        app_id = message.data.get("app_id")  # key for oauth db
        auth_endpoint = message.data.get("auth_endpoint")
        token_endpoint = message.data.get("token_endpoint")
        refresh_endpoint = message.data.get("refresh_endpoint")
        cb_endpoint = f"http://0.0.0.0:{self.port}/auth/callback/{app_id}"

        # some skills may require users to input these, other may provide it
        # this will depend on the app TOS
        client_id = message.data.get("client_id")
        client_secret = message.data.get("client_secret")

        with OAuthApplicationDatabase() as db:
            db[app_id] = {
                "auth_endpoint": auth_endpoint,
                "token_endpoint": token_endpoint,
                "refresh_endpoint": refresh_endpoint,
                "callback_endpoint": cb_endpoint,
                "oauth_service": app_id,
                "client_id": client_id,
                "client_secret": client_secret
            }
        if skill_id not in self.oauth_skills:
            self.oauth_skills[skill_id] = []
        self.oauth_skills[skill_id].append(app_id)

    @app.route("/auth/callback/<app_id>", methods=['GET'])
    def oauth_callback(self, app_id):
        """ user completed oauth, save token to db """
        params = dict(request.args)
        code = params["code"]

        data = OAuthApplicationDatabase()[app_id]
        client_id = data["client_id"]
        client_secret = data["client_secret"]
        token_endpoint = data["token_endpoint"]

        # Prepare and send a request to get tokens! Yay tokens!
        client = WebApplicationClient(client_id)
        token_url, headers, body = client.prepare_token_request(
            token_endpoint,
            authorization_response=request.url,
            redirect_url=request.base_url,
            code=code
        )
        token_response = requests.post(
            token_url,
            headers=headers,
            data=body,
            auth=(client_id, client_secret),
        ).json()

        with OAuthTokenDatabase() as db:
            db.add_token(app_id, token_response)

        return params

    def start_callback_server(self):
        app.run(port=self.port, debug=False)

    def shutdown(self):
        self.bus.remove("oauth.register", self.handle_oauth_register)
        super().shutdown()
