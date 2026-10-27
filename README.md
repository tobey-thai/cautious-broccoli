# Kana & Hangul Practice

Romanization practice for Japanese kana and Korean Hangul. The app is a single
static `index.html` with no build step. An optional sync backend (below) can
keep your practice history in step across devices.

## Features

- Japanese tab: Hiragana, Katakana, Both, and Most Wrong
- Korean tab: Consonants, Vowels, Syllables, and Most Wrong
- Listen button for Korean characters (browser text-to-speech)
- Most Wrong: characters missed 3 or more times, with priority decay on correct answers
- Wrong chars panel (all-time or last session) and a Reference panel listing
  every character with its accepted answers
- Batch Complete screen with missed characters, review, and retry rounds
- Optional sync of practice history between devices (off by default)

Practice history is stored in the browser's `localStorage`, so by default it is
per device and per origin. A new URL starts with empty history.

## Run locally

```bash
python3 -m http.server 8080
```

Then open `http://localhost:8080`.

## Host on SWAG

These steps assume a running [SWAG](https://github.com/linuxserver/docker-swag)
container named `swag`. Replace:

- `<swag-config>` with SWAG's config directory on the host (`/config` inside the
  container)
- `<repo-url>` with this repository's clone URL
- `<app-name>` with the folder and URL name you choose
- `<your-domain>` with your domain

The app itself is public: it is static and holds no secrets. Only the optional
sync API (see [Optional sync backend](#optional-sync-backend)) needs a login.

Pick **one** of the two options below. Each origin has its own `localStorage`
and its own sync data, so serving the app from both places gives you two
separate histories.

### Option A: main domain, subpath

The app is served at `https://<your-domain>/<app-name>/`. Your existing main page
is not affected.

1. **Clone into SWAG's web root:**

   ```bash
   git clone <repo-url> <swag-config>/www/<app-name>
   ```

2. **Block dotfiles.** SWAG's default site serves everything under `www/`,
   including `.git/`. Add this inside the `server` block (the 443 one) in
   `<swag-config>/nginx/site-confs/default.conf`:

   ```nginx
   location ~ /\.(?!well-known/) { deny all; }
   ```

   The exception keeps `/.well-known/` reachable for certificate validation.

3. **Reload nginx:**

   ```bash
   docker exec swag nginx -t && docker exec swag nginx -s reload
   ```

4. **Verify** (see the note below on what counts as a pass):
   - `https://<your-domain>/<app-name>/` loads the app.
   - `https://<your-domain>/<app-name>/.git/HEAD` returns 403.
   - `https://<your-domain>/<app-name>/.git/config` returns 403.

### Option B: subdomain

The app is served at `https://<app-name>.<your-domain>/`. Keep the clone outside
`www/`, because SWAG's default site serves everything under `www/` on the main
domain.

1. **Clone into an `apps` folder:**

   ```bash
   git clone <repo-url> <swag-config>/apps/<app-name>
   ```

2. **Create `<swag-config>/nginx/proxy-confs/<app-name>.subdomain.conf`:**

   ```nginx
   server {
       listen 443 ssl;
       listen [::]:443 ssl;
       server_name <app-name>.*;
       include /config/nginx/ssl.conf;

       root /config/apps/<app-name>;
       index index.html;

       location ~ /\.(?!well-known/) { deny all; }

       location / {
           try_files $uri $uri/ =404;
       }
   }
   ```

3. **Certificate and DNS.** Add `<app-name>` to `SUBDOMAINS` in SWAG's compose
   environment (not needed with a wildcard certificate), and create a DNS record
   for `<app-name>.<your-domain>` pointing at the host.

4. **Apply the change:**

   ```bash
   # if you changed SUBDOMAINS
   docker compose up -d swag

   # otherwise
   docker exec swag nginx -t && docker exec swag nginx -s reload
   ```

5. **Verify:**
   - `https://<app-name>.<your-domain>/` loads the app.
   - `https://<app-name>.<your-domain>/.git/HEAD` returns 403.
   - `https://<your-domain>/<app-name>/` returns 404.

### Checking that `.git` is really blocked

Request the files, not the directory. A request for `/.git/` also returns 403
when no deny rule exists, because nginx refuses to list a directory with no
index file (the error log says `directory index of ... is forbidden`). That 403
says nothing about whether `.git/HEAD` can be read. A pass means 403 for both
`.git/HEAD` and `.git/config`; a 200 means the repository history is
downloadable.

### Updating

On the SWAG host:

```bash
git -C <swag-config>/www/<app-name> pull     # option A
git -C <swag-config>/apps/<app-name> pull    # option B
```

No reload is needed because the app is a static file.

## Optional sync backend

Without a backend, each browser keeps its own history. The backend in `server/`
stores one copy per user and merges it with each device's copy, so your Most
Wrong list follows you between a phone and a desktop. Skip this section if you
don't want it. Sync is off in the app until you turn it on.

### How it works

- `server/server.py` is a small Python (standard library only) server that keeps
  one document per user in SQLite. Endpoints: `GET` and `PUT /api/data`, and
  `GET /healthz`.
- The app keeps using `localStorage` as its working copy. When sync is on, it
  sends its data about 1.5 seconds after a change, and again when the tab is
  hidden, and merges the reply back in. It also syncs when the page loads, when
  the browser comes back online, and when the tab becomes visible again.
- If the server can't be reached, the app keeps working locally and retries
  later.
- Merging is per character: the miss count is the larger of the two, the last
  miss time is the newer one, and the Most Wrong weight comes from the newer
  entry. The last-session snapshot is whichever is newer. If two devices each
  miss the same character before syncing, it counts once, not twice.
- **Reset all data** with sync on also clears the server copy. A device with old
  data can't bring it back afterwards.
- The server trusts the `X-Remote-User` header to say who is calling. SWAG sets
  it from its own login, replacing any value a client sends. Never publish the
  backend's port or reach it any way other than through SWAG, because anyone who
  can reach it directly could send that header themselves. Without a login the
  header is empty and the backend returns 401.

### 1. Create the login

```bash
docker exec -it swag htpasswd -c /config/nginx/.htpasswd <user>
```

`-c` creates the file, and overwrites an existing one. To add a second user later,
run the command again without `-c`. Passwords are stored as salted hashes, and
basic auth should only be used over HTTPS, which SWAG provides.

### 2. Run the backend

Add a service to the compose file that runs SWAG, or use a second compose file
that joins the same network. Replace `<swag-network>` with the name of SWAG's
Docker network (`docker inspect swag` lists it), and use the option you chose for
the path of `server/` (`www` for option A, `apps` for option B):

```yaml
services:
  <app-name>-api:
    image: python:3-alpine
    container_name: <app-name>-api
    restart: unless-stopped
    user: "1000:1000"
    read_only: true
    tmpfs:
      - /tmp
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    command: python /app/server.py
    environment:
      DB_PATH: /data/sync.db
    volumes:
      - <swag-config>/apps/<app-name>/server:/app:ro
      - <swag-config>/app-data/<app-name>:/data
    networks:
      - swag
    healthcheck:
      test: ["CMD", "wget", "-qO-", "http://127.0.0.1:8000/healthz"]
      interval: 30s
      timeout: 5s
      retries: 3

networks:
  swag:
    external: true
    name: <swag-network>
```

Create the data folder first, owned by the user in the file. Keep it outside the
folder nginx serves:

```bash
mkdir -p <swag-config>/app-data/<app-name>
chown 1000:1000 <swag-config>/app-data/<app-name>
docker compose up -d <app-name>-api
```

The service publishes no ports. The database is one file, so backing it up is a
copy of `<swag-config>/app-data/<app-name>/`. With the data on a bind mount, use
`sqlite3 sync.db ".backup copy.db"` if you want a copy that is safe while the
server is running.

### 3. Route `/api/` through SWAG

The login lives only on this location, so the app itself stays public.

**Option B (subdomain):** add this inside the `server` block of
`<app-name>.subdomain.conf`:

```nginx
location /api/ {
    auth_basic "Restricted";
    auth_basic_user_file /config/nginx/.htpasswd;

    include /config/nginx/resolver.conf;
    set $upstream_app <app-name>-api;
    set $upstream_port 8000;
    set $upstream_proto http;

    proxy_set_header X-Remote-User $remote_user;
    proxy_pass $upstream_proto://$upstream_app:$upstream_port;
}
```

**Option A (subpath):** add this inside the same `server` block of `default.conf`
as the dotfile rule. It strips the `/<app-name>` prefix before passing the
request on:

```nginx
location /<app-name>/api/ {
    auth_basic "Restricted";
    auth_basic_user_file /config/nginx/.htpasswd;

    include /config/nginx/resolver.conf;
    set $upstream_app <app-name>-api;
    set $upstream_port 8000;
    set $upstream_proto http;

    rewrite ^/<app-name>/(api/.*)$ /$1 break;
    proxy_set_header X-Remote-User $remote_user;
    proxy_pass $upstream_proto://$upstream_app:$upstream_port;
}
```

Using variables for the upstream lets nginx start even when the backend
container is down. Reload nginx as in the hosting steps above.

### 4. Turn sync on

Open the app, tap **Wrong chars**, then **Sync: off** to switch it on. Do this
once per device. Your browser asks for the login the first time. If the status
line says "Login required." instead, open `/api/data` (or `/<app-name>/api/data`
for option A) in a browser tab, sign in, and tap **Sync** again.

### Verify

- Without a password, `/api/data` returns 401.
- With the password, `/api/data` returns a JSON document.
- In the app, the status line reads "Synced." after you turn sync on.
- Miss a character on one device, then reload another: it shows up there.

### Turn sync off or remove the backend

- **One device:** tap **Sync: on** to turn it off. The server copy is left
  alone, and the device keeps everything it has locally.
- **Everywhere:** stop the container and delete the `location /api/` block. The
  app keeps working from `localStorage`, and a device that still has sync on
  shows "No sync server found." until you turn it off.

### Using Authelia instead of basic auth

Authelia redirects unauthenticated requests to a login page, and a background
`fetch` can't follow that. If you move to Authelia, keep the app public and
protect `/api/` only, with a rule that returns 401 instead of redirecting. Check
Authelia's documentation for the exact include and rule for your version, since I
haven't tested it with this app.

## Tests

The server and its merge rules have unit tests (Python standard library only):

```bash
python3 -m unittest discover -s server
```
