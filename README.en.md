# any2fedi

**[Русская версия](README.md)**

Mirror third-party feeds into the fediverse: public Telegram channels and RSS/Atom feeds are reposted by bot accounts on your own server (GoToSocial, Mastodon, Akkoma, Pleroma — anything with the Mastodon API).

- **Account** — one bot on your server, one access token.
- **Source** — one feed: a Telegram channel or an RSS/Atom feed.
- An account can aggregate any number of sources, and you can run any number of accounts. For example, `@news` collects three news channels while `@blog` reposts a site's RSS.

Telegram is read through the public web preview `https://t.me/s/<channel>`. No bot, no account, no API keys: it works with any public channel that has the preview enabled.

A single Python file, three dependencies available from distro repositories, sqlite for state, and a systemd timer. No containers, no daemons.

## Features

- Formatting (bold, italic, links, quotes, code) is converted to Markdown.
- Photos and albums are re-uploaded to your server.
- Videos, files and polls are not copied. Instead, the video preview frame is attached along with a note and a link to the original. Posts that are only a video with no caption are skipped by default.
- Reposts are marked.
- Every post carries the source name and a link to the original.
- **All wording that ends up in posts lives in the config** (the `texts` section) and can be overridden per account or per source.
- A newly added source doesn't flood the timeline with its history: existing posts are skipped (or only the latest `backfill` are published).
- No duplicates: state is kept in sqlite, and every post is sent with an `Idempotency-Key`.
- If the server is down or answers 5xx, the post is retried on the next run and order is preserved. If the server rejects a post (4xx), it is logged and skipped so the queue never gets stuck.
- Edits and deletions in the original are not tracked.

## Installation

These steps are for Debian/Ubuntu.

```bash
sudo apt install python3-requests python3-bs4 python3-yaml git

sudo useradd --system --home /srv/any2fedi --shell /usr/sbin/nologin any2fedi
sudo git clone https://github.com/<you>/any2fedi.git /srv/any2fedi
sudo mkdir -p /srv/any2fedi/state
sudo cp /srv/any2fedi/config.example.yaml /srv/any2fedi/config.yaml
sudo chown -R any2fedi:any2fedi /srv/any2fedi
sudo chmod 600 /srv/any2fedi/config.yaml
```

### 1. A bot account

Create a regular account for the bot. On GoToSocial with closed registrations, use the CLI:

```bash
./gotosocial --config-path ./config.yaml admin account create \
  --username news --email news@your.domain --password 'a-long-password'
./gotosocial --config-path ./config.yaml admin account confirm --username news
```

Then log in as the bot and, in its profile settings:
- tick **"This is a bot account"**;
- turn off manual approval of followers so anyone can follow;
- say in the bio where the posts come from ("Unofficial mirror of …").

### 2. Access token

```bash
python3 /srv/any2fedi/get_token.py https://your.instance
```

The script prints a link. Open it in a browser, **log in as the bot** (a private window is handy), approve access, and paste the code back into the terminal. Put the resulting token into `config.yaml`.

### 3. Config

Everything is documented inline in [`config.example.yaml`](config.example.yaml). A complete Russian-language example is in [`examples/georgia-news.ru.yaml`](examples/georgia-news.ru.yaml).

### 4. Dry run

```bash
sudo -u any2fedi python3 /srv/any2fedi/any2fedi.py --dry-run -c /srv/any2fedi/config.yaml
```

This fetches the feeds and prints the 3 latest posts of each source exactly as they would be posted. Nothing is published and no state is touched.

### 5. Timer

```bash
sudo cp /srv/any2fedi/systemd/any2fedi.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now any2fedi.timer
journalctl -u any2fedi -f
```

It runs every 10 minutes. The first run only records what is already in the feeds; publishing starts with the next new posts.

## Post wording

A post is assembled from blocks using a template. Empty blocks disappear together with the blank lines around them.

```yaml
texts:
  template: "{text}\n\n{forwarded}\n\n{media_note}\n\n{footer}\n\n{hashtags}"
  footer: "— {source}: {url}"
  forwarded: "↪ forwarded from: {name}"
  unsupported_media: "🎬 video / attachment — see the original"
  truncated: "…"
  rss_title: "**{title}**"
```

Any key can be overridden in `accounts.<name>.texts` or `sources[].texts`. Only the keys you set are replaced; the rest are inherited. Precedence: built-in defaults → `texts` → account → source.

## Extending

- **Another source for an account**: add a line to `sources`.
- **Another account**: create a bot, get a token, and add a block to `accounts`.
- **A new source type** (a site without RSS, another service): subclass `Source` in `any2fedi.py`, implement `fetch()` returning a list of `Item`, and register the class in `SOURCE_TYPES`.

The internal account name (its key under `accounts`) and the source address are used as keys in the state database. If you rename them, the source is treated as new: nothing is duplicated, mirroring simply restarts from the current moment.

## Limitations

- `t.me/s/` shows roughly the last 20 posts. If a channel posts more than 20 times within one timer interval, the extra posts are lost. Shorten the interval for such channels.
- This is HTML scraping: if Telegram changes its markup, the parser will need fixing.
- Within a single run, posts from different sources go out grouped by source, not strictly by time.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The tests run offline against fixtures in `tests/fixtures/`.

## License

MIT, see [LICENSE](LICENSE).
