# LibreDesk setup

JellyFix uses LibreDesk for support conversations, replies, status changes, and CSAT links.

## 1. Create API credentials

Create an API key for a LibreDesk agent with access to the target inbox and team. Store the key and secret in one file:

```text
api_key:api_secret
```

Mount the file through `secrets/` and set `LIBREDESK_CREDENTIAL_FILE` to its container path. Do not put credentials in `.env`.

## 2. Configure Docker networking

The Compose file attaches JellyFix to its default network and the external `libredesk_libredesk` network. Set the private API address:

```dotenv
LIBREDESK_BASE_URL=http://libredesk_app:9000
```

If your LibreDesk service or network has a different name, update both values in your deployment.

## 3. Configure JellyFix

Set the LibreDesk values in `.env`:

```dotenv
LIBREDESK_CREDENTIAL_FILE=/run/secrets/libredesk_api_credentials
LIBREDESK_WEBHOOK_SECRET_FILE=/run/secrets/libredesk_webhook_secret
LIBREDESK_INBOX_ID=1
LIBREDESK_TEAM_ID=1
LIBREDESK_TAG=jellyfix
LIBREDESK_SUBJECT_PREFIX=jellyfin-issue#
LIBREDESK_PUBLIC_URL=https://support.example.com
```

`LIBREDESK_PUBLIC_URL` is the public LibreDesk origin used for CSAT links.

## 4. Create the webhook

Create an enabled LibreDesk webhook with:

```text
URL: http://jellyfix:8000/api/v1/integrations/libredesk/webhook
Events: message.created, conversation.status_changed
Secret: the same value stored in libredesk_webhook_secret
```

## 5. Verify

Rebuild JellyFix, use LibreDesk's webhook test, and confirm:

- JellyFix remains healthy.
- A new ticket creates one LibreDesk conversation.
- One reply and one status change synchronize in each direction.
- The conversation has the configured team and tag.
