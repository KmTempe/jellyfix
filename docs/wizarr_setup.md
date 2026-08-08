# Wizarr setup

JellyFix uses Wizarr to obtain the ticket reporter's email before creating a LibreDesk conversation.

## Compatibility

The configured Wizarr instance must provide a user lookup compatible with:

```text
GET /api/users?username=<Jellyfin username>
X-API-Key: <API key>
```

The result must contain one exact Jellyfin username match with a valid email address. Wizarr API behavior can vary by version, so verify this read-only endpoint before enabling required email lookup.

## 1. Create the secret

Create an API key with permission to read users. Store only the key in:

```text
secrets/wizarr_api_key
```

Do not place the key in `.env`.

## 2. Configure JellyFix

Set:

```dotenv
WIZARR_BASE_URL=http://wizarr:5690
WIZARR_TOKEN_FILE=/run/secrets/wizarr_api_key
WIZARR_TIMEOUT_SECONDS=5
WIZARR_CACHE_TTL_SECONDS=300
WIZARR_EMAIL_REQUIRED=true
```

Use an address reachable from the JellyFix container. If Wizarr is not on a shared Docker network, use an appropriate private host address.

When `WIZARR_EMAIL_REQUIRED=true`, ticket notifications remain queued until Wizarr returns a verified email.

## 3. Verify

- Confirm Jellyfin and Wizarr usernames match exactly.
- Confirm each supported user has one valid email address.
- Perform a read-only API lookup without printing the response or API key.
- Rebuild JellyFix and confirm its health endpoint succeeds.
