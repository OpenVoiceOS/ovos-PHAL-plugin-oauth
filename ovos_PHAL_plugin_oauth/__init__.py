import os
import tempfile
import time
import uuid

import qrcode
import requests
from flask import Flask, request
from oauthlib.oauth2 import WebApplicationClient
from ovos_backend_client.database import (OAuthApplicationDatabase,
                                          OAuthTokenDatabase)
from ovos_bus_client.message import Message
from ovos_plugin_manager.phal import PHALPlugin
from ovos_utils import classproperty
from ovos_utils.log import LOG
from ovos_utils.network_utils import get_ip
from ovos_utils.process_utils import RuntimeRequirements

os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

app = Flask(__name__)


@app.route("/auth/callback/<munged_id>", methods=['GET'])
def oauth_callback(munged_id):
    """ user completed oauth, save token to db """
    params = dict(request.args)
    code = params["code"]

    data = OAuthPlugin.oauth_db[munged_id]
    client_id = data["client_id"]
    client_secret = data["client_secret"]
    token_endpoint = data["token_endpoint"]
    munged_id = data["oauth_service"]

    # Prepare and send a request to get tokens! Yay tokens!
    client = WebApplicationClient(client_id)
    token_url, headers, body = client.prepare_token_request(
        token_endpoint,
        authorization_response=request.url,
        redirect_url=request.base_url,
        code=code
    )
    if client_secret:
        # Uses client_secret for authentication
        token_response = requests.post(
            token_url,
            headers=headers,
            data=body,
            auth=(client_id, client_secret),
        ).json()

    else:
        # Uses basic auth for authentication
        token_response = requests.post(
            token_url,
            headers=headers,
            data=body,
        ).json()

    with OAuthTokenDatabase() as db:
        # Make sure expires_at entry exists
        if 'expires_at' not in token_response:
            token_response['expires_at'] = (
                    time.time() + token_response['expires_in']
            )
        db.add_token(munged_id, token_response)

    # Allow any registered app / skill to handle the token response urgently, if needed
    # For example temporary tokens to generate a long-lived token for the skill/plugin
    app.bus.emit(
        Message(f"oauth.token.response.{munged_id}", data=token_response))

    return params


class OAuthPluginValidator:
    @staticmethod
    def validate(config=None):
        """ this method is called before loading the plugin.
        If it returns False the plugin is not loaded.
        This allows a plugin to run platform checks"""
        return True


class OAuthPlugin(PHALPlugin):
    validator = OAuthPluginValidator
    oauth_db = OAuthApplicationDatabase()

    def __init__(self, bus=None, config=None):
        self.config = config
        self.port = self.config.get("port", 36536)
        self.local_flask_host = None
        self.oauth_skills = {}
        super().__init__(bus=bus, name="ovos-PHAL-plugin-oauth", config=config)

        # self.bus can only be used after call to super()
        self.bus.on("oauth.register", self.handle_oauth_register)
        self.bus.on("oauth.start", self.handle_start_oauth)
        self.bus.on("oauth.get", self.handle_get_auth_url)
        self.bus.on("oauth.refresh", self.handle_oauth_refresh_token)
        self.bus.on("ovos.shell.oauth.register.credentials",
                    self.handle_client_secret)

        # QR Code Remote OAuth Process Support
        self.bus.on("oauth.get.app.host.info", self.handle_get_app_host_info)
        self.bus.on("oauth.generate.qr.request", self.handle_generate_qr)

        # trigger register events from oauth skills
        self.bus.emit(Message("oauth.ping"))

    @classproperty
    def runtime_requirements(self):
        return RuntimeRequirements(internet_before_load=True,
                                   network_before_load=True,
                                   gui_before_load=False,
                                   requires_internet=True,
                                   requires_network=True,
                                   requires_gui=True,
                                   no_internet_fallback=False,
                                   no_network_fallback=False,
                                   no_gui_fallback=True)

    def handle_client_secret(self, message):
        skill_id = message.data.get("skill_id")
        app_id = message.data.get("app_id")
        munged_id = f"{skill_id}_{app_id}"  # key for oauth db

        client_id = message.data.get("client_id")
        client_secret = message.data.get("client_secret")

        # update db
        with self.oauth_db as db:
            db[munged_id]["client_id"] = client_id
            db[munged_id]["client_secret"] = client_secret

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
        cb_endpoint = f"http://0.0.0.0:{self.port}/auth/callback/{munged_id}"
        scope = message.data.get("scope")

        # some skills may require users to input these, other may provide it
        # this will depend on the app TOS
        client_id = message.data.get("client_id")
        client_secret = message.data.get("client_secret")

        # For QR code based authentication, some skills/plugins might
        # have their own UI and QML flow to display the code
        # Skills / apps can mark this as false at registeration
        # if they want to handle the display
        shell_display = message.data.get("shell_integration", True)

        try:
            with self.oauth_db as db:
                db.add_application(oauth_service=munged_id,
                                   client_id=client_id,
                                   client_secret=client_secret,
                                   auth_endpoint=auth_endpoint,
                                   token_endpoint=token_endpoint,
                                   callback_endpoint=cb_endpoint,
                                   scope=scope,
                                   shell_integration=shell_display)

            if client_id and client_secret:
                # skill bundled app credentials
                needs_creds = False
            else:
                # extra GUI setup page needed to enter client_id and client_secret
                # e.g. spotify
                needs_creds = True

            self.oauth_skills[skill_id]["needs_creds"] = needs_creds
            response = message.response({"munged_id": munged_id,
                                         "client_id": client_id,
                                         "needs_creds": needs_creds})
        except PermissionError as e:
            LOG.error(f"Failed to write {self.oauth_db.path}")
            response = message.response({"munged_id": munged_id,
                                         "client_id": client_id,
                                         "error": e})
        except Exception as e:
            LOG.exception(e)
            response = message.response({"munged_id": munged_id,
                                         "client_id": client_id,
                                         "error": e})
        self.bus.emit(response)

    def handle_oauth_refresh_token(self, message):
        """Refresh oauth token.

        See:
        https://www.oauth.com/oauth2-servers/making-authenticated-requests/refreshing-an-access-token/

        for details on the procedure.
        """
        response_data = {}
        oauth_id = f"{message.data['skill_id']}_{message.data['app_id']}"
        # Load all needed data for refresh
        with self.oauth_db as db:
            app_data = db.get(oauth_id)
        with OAuthTokenDatabase() as db:
            token_data = db.get(oauth_id)

        if (app_data is None or
                token_data is None or 'refresh_token' not in token_data):
            LOG.warning("Token data doesn't contain a refresh token and "
                        "cannot be refreshed.")
            response_data["result"] = "Error"
        else:
            refresh_token = token_data["refresh_token"]

            # Fall back to token endpoint if no specific refresh endpoint
            # has been set
            token_endpoint = app_data["token_endpoint"]

            client_id = app_data["client_id"]
            client_secret = app_data["client_secret"]

            # Perform refresh
            client = WebApplicationClient(client_id, refresh_token=refresh_token)
            uri, headers, body = client.prepare_refresh_token_request(token_endpoint)
            refresh_result = requests.post(uri, headers=headers, data=body,
                                           auth=(client_id, client_secret))

            if refresh_result.ok:
                new_token_data = refresh_result.json()
                # Make sure 'expires_at' entry exists in token
                if 'expires_at' not in new_token_data:
                    new_token_data['expires_at'] = time.time() + token_data['expires_in']
                # Store token
                with OAuthTokenDatabase() as db:
                    token_data.update(new_token_data)
                    db.update_token(oauth_id, token_data)
                response_data = {"result": "Ok",
                                 "client_id": client_id,
                                 "token": token_data}
            else:
                LOG.error("Token refresh failed :(")
                response_data["result"] = "Error"

        response = message.response(response_data)
        self.bus.emit(response)

    def get_oauth_url(self, skill_id, app_id):
        munged_id = f"{skill_id}_{app_id}"  # key for oauth db

        callback_endpoint = f"http://0.0.0.0:{self.port}/auth/callback/{munged_id}"

        data = self.oauth_db[munged_id]
        client = WebApplicationClient(data["client_id"])
        return client.prepare_request_uri(data["auth_endpoint"],
                                          redirect_uri=data.get(
                                              "callback_endpoint") or callback_endpoint,
                                          show_dialog=True,
                                          state=data.get(
                                              'oauth_service') or munged_id,
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

    def handle_get_app_host_info(self, message):
        self.bus.emit(message.reply("oauth.app.host.info.response", {
            "host": get_ip(),
            "port": self.port
        }))

    #### QR Code Generation and Service Support ####
    def handle_generate_qr(self, message):
        skill_id = message.data.get("skill_id")
        app_id = message.data.get("app_id")
        munged_id = f"{skill_id}_{app_id}"  # key for oauth db
        data = self.oauth_db[munged_id]
        oauth_url = data.get("auth_endpoint")
        client_id = data.get("client_id", None)
        client_secret = data.get("client_secret", None)

        if not oauth_url:
            error = f"No auth endpoint found for oauth app {munged_id}"
            LOG.error(error)
            self.bus.emit(message.reply("oauth.generate.qr.response", {
                "skill_id": skill_id,
                "app_id": app_id,
                "error": error
            }))
            return

        plugin_service_url = self.build_plugin_service_url(
            oauth_url, skill_id, app_id, client_id, client_secret)
        qr_code = self.generate_qr(plugin_service_url, skill_id, app_id)
        self.bus.emit(message.reply("oauth.generate.qr.response", {
            "skill_id": skill_id,
            "app_id": app_id,
            "qr": qr_code
        }))

        # display the code in shell if registed app wants
        display_code_on_shell = data.get("shell_integration", True)
        if display_code_on_shell:
            LOG.info(f"Displaying QR code for:{munged_id}")
            self.bus.emit(Message("ovos.shell.oauth.display.qr.code", {
                "skill_id": skill_id,
                "app_id": app_id,
                "qr": qr_code
            }))

    def generate_qr(self, url, skill_id, app_id):
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        img_id = str(uuid.uuid4().hex)[:4]
        temp_dir = tempfile.gettempdir()
        img.save(f"{temp_dir}/{skill_id}_{app_id}_oauth_qr_{img_id}.png")
        return f"{temp_dir}/{skill_id}_{app_id}_oauth_qr_{img_id}.png"

    def build_plugin_service_url(self, oauth_url, skill_id, app_id, client_id=None, client_secret=None):
        self.local_flask_host = get_ip()  # Need to get local ip at runtime instead of boot
        redirect_uri = f"http://{self.local_flask_host}:{self.port}/auth/callback/{skill_id}_{app_id}"
        oauth_complete_url = f"{oauth_url}?redirect_uri={redirect_uri}"

        if client_secret:
            # Some services require client_id and client_secret
            oauth_complete_url = f"{oauth_url}?client_id={client_id}&client_secret={client_secret}&redirect_uri={redirect_uri}"

        if client_id:
            # Some require only client_id
            oauth_complete_url = f"{oauth_url}?client_id={client_id}&redirect_uri={redirect_uri}"

        return oauth_complete_url

    def run(self):
        # Needs to be the LAN IP address where remote devices can reach the app
        self.local_flask_host = get_ip()
        app.bus = self.bus
        app.run(host="0.0.0.0", port=self.port, debug=False)

    def shutdown(self):
        self.bus.remove("oauth.register", self.handle_oauth_register)
        self.bus.remove("oauth.get", self.handle_get_auth_url)
        self.bus.remove("oauth.start", self.handle_start_oauth)
        self.bus.remove("oauth.get.app.host.info",
                        self.handle_get_app_host_info)
        self.bus.remove("oauth.generate.qr.request", self.handle_generate_qr)
        super().shutdown()
