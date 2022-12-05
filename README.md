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