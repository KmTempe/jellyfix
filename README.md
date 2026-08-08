# JellyFix

JellyFix adds issue reporting and ticket history to Jellyfin Web. A small browser injector talks to a FastAPI backend that validates Jellyfin users, stores tickets in SQLite, and synchronizes support conversations with LibreDesk.

## Features

- Server-verified Jellyfin identity and administrator permissions
- One active ticket per user and media item, with unlimited resolved history
- Five-minute cooldown after resolution with `429` and `Retry-After`
- User ticket history and administrator Ticket Manager
- Durable SQLite queues for Wizarr and LibreDesk outages
- Bidirectional LibreDesk messages and status synchronization
- Safe CSAT links and SQLite migration backups

### Request and data path

```mermaid
flowchart LR
    Browser["&lt;&lt;client&gt;&gt;<br/>Jellyfin Web<br/><b>injector.js</b>"]
    Jellyfin["&lt;&lt;external API&gt;&gt;<br/>Jellyfin<br/><b>GET /Users/Me</b>"]

    subgraph JellyFixContainer["&lt;&lt;container&gt;&gt; JellyFix :8000 — jellyfix_default"]
        direction TB
        API["&lt;&lt;component&gt;&gt;<br/>Ticket API<br/><b>/api/v1/tickets</b>"]
        SQLite[("&lt;&lt;database&gt;&gt;<br/>SQLite<br/>tickets · comments · mappings<br/>outbox · webhook inbox")]
        API -->|"persist + enqueue"| SQLite
    end

    Browser <-->|"create · reply · history<br/>tickets · status · CSAT"| API
    API -->|"validate bearer token"| Jellyfin

    classDef client fill:#e8f1ff,stroke:#2563eb,stroke-width:2px,color:#111827;
    classDef service fill:#eefbf3,stroke:#16803c,stroke-width:2px,color:#111827;
    classDef external fill:#fff7e6,stroke:#b7791f,stroke-width:2px,color:#111827;
    classDef database fill:#f4ecff,stroke:#7c3aed,stroke-width:2px,color:#111827;
    class Browser client;
    class API service;
    class Jellyfin external;
    class SQLite database;
```

### LibreDesk synchronization bridge

```mermaid
flowchart LR
    Wizarr["&lt;&lt;external API&gt;&gt;<br/>Wizarr<br/><b>GET /api/users?username=</b>"]

    subgraph JellyFixContainer["&lt;&lt;container&gt;&gt; JellyFix :8000"]
        direction TB
        Outbox[("SQLite outbox")]
        Worker["&lt;&lt;component&gt;&gt;<br/>Sync worker<br/>retry + reconciliation"]
        Webhook["&lt;&lt;component&gt;&gt;<br/>Signed webhook<br/><b>/api/v1/integrations/libredesk/webhook</b>"]
        Inbox[("SQLite webhook inbox")]

        Outbox -->|"pending work"| Worker
        Webhook -->|"durable 202"| Inbox
    end

    subgraph LibreDeskContainer["&lt;&lt;container&gt;&gt; LibreDesk :9000"]
        LibreDesk["&lt;&lt;component&gt;&gt;<br/>Support inbox<br/><b>libredesk_app:9000</b>"]
    end

    Worker -->|"exact reporter lookup"| Wizarr
    Worker ==>|"conversation · message · status APIs"| LibreDesk
    LibreDesk ==>|"message/status webhook<br/>http://jellyfix:8000"| Webhook

    Reporter["&lt;&lt;user&gt;&gt;<br/>Reporter email"]
    LibreDesk -.->|"agent replies + notifications"| Reporter

    classDef client fill:#e8f1ff,stroke:#2563eb,stroke-width:2px,color:#111827;
    classDef service fill:#eefbf3,stroke:#16803c,stroke-width:2px,color:#111827;
    classDef external fill:#fff7e6,stroke:#b7791f,stroke-width:2px,color:#111827;
    classDef database fill:#f4ecff,stroke:#7c3aed,stroke-width:2px,color:#111827;
    class Reporter client;
    class Worker,Webhook,LibreDesk service;
    class Wizarr external;
    class Outbox,Inbox database;
```

JellyFix is attached to both `jellyfix_default` and `libredesk_libredesk`. The thick arrows are private container-to-container traffic over `libredesk_libredesk`; no public LibreDesk route is required.

### Network topology

```mermaid
flowchart TB
    Browser["&lt;&lt;client&gt;&gt;<br/>Jellyfin Web UI<br/><b>injector.js</b>"]
    Gateway["&lt;&lt;public endpoint&gt;&gt;<br/>Jellyfin host / reverse proxy<br/><b>HTTPS :443</b>"]

    Browser <-->|"browser HTTPS"| Gateway

    subgraph PublicRoutes["Same-origin routes"]
        JellyfinWeb["&lt;&lt;route&gt;&gt;<br/><b>/web</b><br/>Jellyfin UI"]
        JellyFixRoute["&lt;&lt;route&gt;&gt;<br/><b>/jellyfix/api/v1</b><br/>ticket API"]
    end

    Gateway --> JellyfinWeb
    Gateway --> JellyFixRoute

    subgraph JellyFixNetwork["Docker network: jellyfix_default"]
        JellyFix["&lt;&lt;container&gt;&gt;<br/>JellyFix<br/><b>jellyfix:8000</b>"]
        Database[("&lt;&lt;volume&gt;&gt;<br/>SQLite data<br/><b>/data</b>")]
        JellyFix --> Database
    end

    JellyFixRoute -->|"proxied API request"| JellyFix

    JellyfinAPI["&lt;&lt;server API&gt;&gt;<br/>Jellyfin<br/><b>/Users/Me</b>"]
    WizarrAPI["&lt;&lt;server API&gt;&gt;<br/>Wizarr<br/><b>/api/users?username=</b>"]

    JellyFix -->|"token validation"| JellyfinAPI
    JellyFix -->|"reporter email lookup"| WizarrAPI

    subgraph LibreDeskNetwork["Docker network: libredesk_libredesk"]
        LibreDesk["&lt;&lt;container&gt;&gt;<br/>LibreDesk<br/><b>libredesk_app:9000</b>"]
    end

    JellyFix <-->|"private container bridge<br/>REST sync → · signed webhook ←"| LibreDesk
    LibreDesk -.->|"configured inbox delivery"| Email["&lt;&lt;external service&gt;&gt;<br/>Email provider"]
    Email -.-> Reporter["&lt;&lt;user&gt;&gt;<br/>Reporter"]

    classDef client fill:#e8f1ff,stroke:#2563eb,stroke-width:2px,color:#111827;
    classDef service fill:#eefbf3,stroke:#16803c,stroke-width:2px,color:#111827;
    classDef external fill:#fff7e6,stroke:#b7791f,stroke-width:2px,color:#111827;
    classDef database fill:#f4ecff,stroke:#7c3aed,stroke-width:2px,color:#111827;
    class Browser,Reporter client;
    class Gateway,JellyfinWeb,JellyFixRoute,JellyFix,LibreDesk service;
    class JellyfinAPI,WizarrAPI,Email external;
    class Database database;
```

Browser traffic stays on the Jellyfin HTTPS origin. JellyFix performs authentication, Wizarr lookup, and LibreDesk synchronization as server-to-server requests; only the LibreDesk bridge uses the shared private Docker network.

## Ticket lifecycle

### Creation and delivery

```mermaid
flowchart TD
    Start(["Reporter submits a ticket"]) --> Auth["Validate Jellyfin identity"]
    Auth --> Active{"Active ticket exists<br/>for this user and media?"}

    Active -->|"Yes"| Conflict(["409 Conflict<br/>return existing ticket"])
    Active -->|"No"| Cooldown{"Resolved less than<br/>5 minutes ago?"}
    Cooldown -->|"Yes"| RateLimit(["429 Too Many Requests<br/>return Retry-After"])
    Cooldown -->|"No"| Create["Persist ticket with NEW status"]

    Create --> Queue["Add conversation creation<br/>to durable outbox"]
    Queue --> Accepted(["Ticket returned to reporter"])
    Queue -.-> Worker["Background sync attempt"]
    Worker --> Email{"Exact Wizarr email found?"}
    Email -->|"No or unavailable"| Pending(["Keep pending<br/>retry with backoff"])
    Email -->|"Yes"| Search["Search LibreDesk by<br/>jellyfin-issue#ticket_uuid"]
    Search --> Matches{"Matching conversations"}
    Matches -->|"None"| Remote["Create conversation"]
    Matches -->|"Exactly one"| Reuse["Reuse conversation"]
    Matches -->|"More than one"| Ambiguous(["Keep pending<br/>manual review required"])
    Remote --> Synced(["Conversation mapped"])
    Reuse --> Synced

    classDef decision fill:#fff7e6,stroke:#b7791f,stroke-width:2px,color:#111827;
    classDef blocked fill:#fff0f0,stroke:#c53030,stroke-width:2px,color:#111827;
    classDef terminal fill:#f4ecff,stroke:#7c3aed,stroke-width:2px,color:#111827;
    class Active,Cooldown,Email,Matches decision;
    class Conflict,RateLimit,Pending,Ambiguous blocked;
    class Start,Accepted,Synced terminal;
```

Each pending delivery is retried in a later worker cycle. A failed external integration never removes the local ticket.

### Status and conversation flow

```mermaid
flowchart LR
    New["NEW"] -->|"agent reply<br/>or remote open"| Progress["IN PROGRESS"]
    New -->|"resolved directly"| Resolved["RESOLVED"]
    Progress -->|"resolve or close"| Resolved
    Resolved --> Outcome{"Next action"}

    New -.-> Messages["Messages sync<br/>in both directions"]
    Progress -.-> Messages

    Outcome -->|"CSAT received"| CSAT["Show safe CSAT action"]
    Outcome -->|"reopened without<br/>an active conflict"| Reopened["IN PROGRESS<br/>(reopened)"]
    Outcome -->|"5-minute cooldown elapsed"| Next(["Another ticket may be created"])
    Outcome -->|"administrator deletes locally"| Deleted(["Local ticket deleted"])
    Reopened -->|"resolve again"| ResolvedAgain["RESOLVED"]
    Deleted -.-> Preserved["LibreDesk conversation remains"]

    classDef state fill:#eefbf3,stroke:#16803c,stroke-width:2px,color:#111827;
    classDef action fill:#e8f1ff,stroke:#2563eb,stroke-width:2px,color:#111827;
    classDef decision fill:#fff7e6,stroke:#b7791f,stroke-width:2px,color:#111827;
    classDef terminal fill:#f4ecff,stroke:#7c3aed,stroke-width:2px,color:#111827;
    class New,Progress,Resolved,Reopened,ResolvedAgain state;
    class Messages,CSAT action;
    class Outcome decision;
    class Next,Deleted terminal;
```

## Quick start

1. Copy `.env.example` to `.env` and set your deployment values.
2. Create the credential files described in the integration guides.
3. Ensure the external LibreDesk Docker network exists when LibreDesk is enabled.
4. Build and start JellyFix:

```powershell
docker compose up --build -d
docker compose ps
```

The Compose service exposes JellyFix on host port `18000`. Runtime data is stored in `backend/data` and mounted at `/data`.

Integration setup:

- [LibreDesk setup](docs/libredesk_setup.md)
- [Wizarr setup](docs/wizarr_setup.md)

## Injector

Install `frontend/injector.js` in Jellyfin Web using a JavaScript injector plugin. JellyFix must be reachable from the Jellyfin browser origin under `/jellyfix` because the injector uses:

```javascript
window.location.origin + "/jellyfix/api/v1"
```

After replacing the script, clear the Jellyfin Web cache or hard-refresh the browser.

## API

All routes except health and the signed LibreDesk webhook require a Jellyfin bearer token.

```text
GET    /api/v1/healthz
GET    /api/v1/me
GET    /api/v1/items/{item_id}/ticket
POST   /api/v1/tickets
GET    /api/v1/tickets/mine
GET    /api/v1/tickets/{ticket_id}
POST   /api/v1/tickets/{ticket_id}/comments
PATCH  /api/v1/tickets/{ticket_id}/status
PATCH  /api/v1/tickets/status
DELETE /api/v1/tickets/{ticket_id}
DELETE /api/v1/tickets
GET    /api/v1/admin/tickets
POST   /api/v1/integrations/libredesk/webhook
```

## Development

Use the repository virtual environment:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
python -m unittest discover -s backend -p "test_*.py" -v
```

Run locally:

```powershell
Set-Location backend
$env:JELLYFIX_ENV='development'
$env:JELLYFIN_URL='http://localhost:8096'
$env:PUBLIC_ORIGIN='http://localhost:8000'
$env:TRUSTED_HOSTS='localhost:8000'
python -m uvicorn main:app --reload --port 8000
```

Release checks:

```powershell
python -m coverage run -m unittest discover -s backend -p "test_*.py"
python -m coverage report --show-missing
python -m compileall -q backend\app backend\main.py
node --check frontend\injector.js
docker compose config --quiet
git diff --check
```

## Security and data

Do not commit `.env`, `secrets/`, SQLite databases, migration backups, or credential exports. JellyFix derives user identity and administrator status from Jellyfin and renders browser content with DOM text APIs.
