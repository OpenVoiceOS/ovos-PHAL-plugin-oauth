# PHAL OAuth Plugin

WIP

## Bus API

Listens for
```python
# skills register app on load or on oauth.ping
self.bus.on("oauth.register", self.handle_oauth_register)

# this triggers the ovos shell oauth flow
self.bus.on("oauth.start", self.handle_start_oauth)

# when ovos shell sends client_id/secret add it to db and continue oauth flow
self.bus.on("ovos.shell.oauth.register.credentials", self.handle_client_secret)

# this returns the oauth url for any external UI that wants to use it
self.bus.on("oauth.get", self.handle_get_auth_url)
```

Emits
```python
# on plugin load trigger register events from oauth skills that were loaded already
self.bus.emit(Message("oauth.ping"))

# on oauth.get send oauth.url
self.bus.emit(message.reply("oauth.url", {"url": url}))

# on oauth.start flow trigger ovos shell UI
self.bus.emit(message.forward(
        "ovos.shell.oauth.start.authentication",
        {"url": url, "needs_credentials": self.oauth_skills[skill_id]["needs_creds"]})
    )
```

## Registering OAuth app with the plugin

send OAuth info in `oauth.register`

```python
skill_id = message.data.get("skill_id")
app_id = message.data.get("app_id")
munged_id = f"{skill_id}_{app_id}"  # key for oauth db

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
```

---------------------------------------
## QR Code - Remote OAuth Integration Flow

* Note: This flow requires the a GUI to display the QR Code that can be scanned by the user using any external device. This also requires the port for the oauth app to be unblocked on the ufw.

### **Example Usage From A Skill / Plugin**

``` python
self.skill_id = "my_skill_id"
self.app_id = "my_app_id"
self.client_id = None
self.munged_id = f"{self.skill_id}_{self.app_id}"
self.bus.on("oauth.app.host.info.response", self.handle_host_response)
self.bus.on("oauth.generate.qr.response", self.handle_qr_generated)
self.bus.on("oauth.token.response.{self.munged_id}", self.handle_token_response)

def handle_host_response(self, message):
    # Some apps with OAuth Spec 2.0 require client_id to match the redirect_uri address and port, set the client id before registering the skill, send a request to "oauth.get.app.host.info" to get the host and port
    host = message.data.get("host", None)
    port = message.data.get("port", None)
    self.client_id = f"http://{host}:{port}"

def register_skill(self):
    client_secret = "my_client_secret"
    auth_endpoint = "https://example.com/auth"
    token_endpoint = "https://example.com/auth/token"
    self.bus.emit(Message("oauth.register", {
        "skill_id": self.skill_id, #Required
        "app_id": self.app_id, #Required
        "client_id": self.client_id, #Optional - Some apps may require this
        "client_secret": client_secret, #Optional - Some apps may require this
        "auth_endpoint": auth_endpoint, #Required
        "token_endpoint": token_endpoint, #Required
        "refresh_endpoint": "", #Optional - Some apps may require this
        "scope": "", #Optional - Some apps may require this
        "shell_integration": True #Optional - mark as false if app/skill handles displaying generated QR code. mark as true if shell should handle it.
    }))

def start_qr_generation(self):
    self.bus.emit(Message("oauth.generate.qr.request", {
        "app_id": self.app_id, # Required
        "skill_id": self.skill_id # Required
    }))

def handle_qr_generated(self, message):
    qr = message.data.get("qr", None)
    # Use GUI to display the generated QR Code
    # somewhere in your QML UI
    self.gui["qr_image_path"] = qr

def handle_token_response(self, message):
    response = message.data
    access_token = response.get("access_token", None)
    # Do something with access_token once oauth flow is complete

# Always register the skill first before requesting the QR Code to be generated
self.register_skill()
self.start_qr_flow()
```
