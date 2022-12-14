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
        self.bus.on("oauth.start", self.handle_start_oauth)
        self.bus.on("oauth.get", self.handle_get_auth_url)
        self.bus.on("ovos.shell.oauth.register.credentials", self.handle_client_secret)

        # trigger register events from oauth skills
        self.bus.emit(Message("oauth.ping"))

        self.oauth_skills = {}

    def handle_client_secret(self, message):
        skill_id = message.data.get("skill_id")
        app_id = message.data.get("app_id")
        munged_id = f"{skill_id}_{app_id}"  # key for oauth db

        client_id = message.data.get("client_id")
        client_secret = message.data.get("client_secret")

        # update db
        with OAuthApplicationDatabase() as db:
            db[munged_id]["client_id"] = client_id
            db[munged_id]["client_secret"] = client_secret
            db.store()

        # trigger oauth flow
        url = self.get_oauth_url(skill_id, app_id)
        self.bus.emit(message.forward(
            "ovos.shell.oauth.start.authentication",
            {"url": url, "skill_id": skill_id, "app_id": app_id,
             "needs_credentials": self.oauth_skills[skill_id]["needs_creds"]})
        )

    def handle_oauth_register(self, message):
        skill_id = message.data.get("skill_id")
        app_id = message.data.get("app_id")
        munged_id = f"{skill_id}_{app_id}"  # key for oauth db

        if skill_id not in self.oauth_skills:
            self.oauth_skills[skill_id] = {"app_ids": []}
        self.oauth_skills[skill_id]["app_ids"].append(app_id)

        # these fields are app specific and provided by skills
        auth_endpoint = message.data.get("auth_endpoint")
        token_endpoint = message.data.get("token_endpoint")
        refresh_endpoint = message.data.get("refresh_endpoint")
        cb_endpoint = f"http://0.0.0.0:{self.port}/auth/callback/{munged_id}"
        scope = message.data.get("scope")

        # some skills may require users to input these, other may provide it
        # this will depend on the app TOS
        client_id = message.data.get("client_id")
        client_secret = message.data.get("client_secret")

        with OAuthApplicationDatabase() as db:
            db.add_application(oauth_service=munged_id,
                               client_id=client_id,
                               client_secret=client_secret,
                               auth_endpoint=auth_endpoint,
                               token_endpoint=token_endpoint,
                               refresh_endpoint=refresh_endpoint,
                               callback_endpoint=cb_endpoint,
                               scope=scope)

        if client_id and client_secret:
            # skill bundled app credentials
            self.oauth_skills[skill_id]["needs_creds"] = False
        else:
            # extra GUI setup page needed to enter client_id and client_secret
            # eg. spotify
            self.oauth_skills[skill_id]["needs_creds"] = True

    def get_oauth_url(self, skill_id, app_id):
        munged_id = f"{skill_id}_{app_id}"  # key for oauth db

        callback_endpoint = f"http://0.0.0.0:{self.port}/auth/callback/{munged_id}"

        data = OAuthApplicationDatabase()[munged_id]
        client = WebApplicationClient(data["client_id"])
        return client.prepare_request_uri(data["auth_endpoint"],
                                          redirect_uri=data.get("callback_endpoint") or callback_endpoint,
                                          show_dialog=True,
                                          state=data.get('oauth_service') or munged_id,
                                          scope=data["scope"])

    def handle_get_auth_url(self, message):
        skill_id = message.data.get("skill_id")
        app_id = message.data.get("app_id")
        url = self.get_oauth_url(skill_id, app_id)
        self.bus.emit(message.reply("oauth.url", {"url": url}))

    def handle_start_oauth(self, message):
        skill_id = message.data.get("skill_id")
        app_id = message.data.get("app_id")
        url = self.get_oauth_url(skill_id, app_id)
        self.bus.emit(message.forward(
            "ovos.shell.oauth.start.authentication",
            {"url": url, "skill_id": skill_id, "app_id": app_id,
             "needs_credentials": self.oauth_skills[skill_id]["needs_creds"]})
        )

    @app.route("/auth/callback/<munged_id>", methods=['GET'])
    def oauth_callback(self, munged_id):
        """ user completed oauth, save token to db """
        params = dict(request.args)
        code = params["code"]

        data = OAuthApplicationDatabase()[munged_id]
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
            db.add_token(munged_id, token_response)

        return params

    def run(self):
        app.run(port=self.port, debug=False)

    def shutdown(self):
        self.bus.remove("oauth.register", self.handle_oauth_register)
        self.bus.remove("oauth.get", self.handle_get_auth_url)
        self.bus.remove("oauth.start", self.handle_start_oauth)
        super().shutdown()
