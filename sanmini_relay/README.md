# fb_relay — Facebook pages that need a login

Some KOL pages can only be seen when logged in (ม้าเฉียว's
`facebook.com/machaodohoon` is age-restricted), so every scraper KOL Watch
pays for gets `not_available`. `fb_relay.py` runs on **sanmini** with a
**spare FB account**, reads those pages 4 times a day and pushes the posts
to KOL Watch (`POST /api/relay_posts`). The hourly scan then treats them like
any other channel: keyword match, LINE card, AI reviewer.

```
sanmini cron 08:20 12:20 16:20 20:20 (+0–15 min random)
  └─ fb_relay.py run ──push──▶ KOL Watch /api/relay_posts ──▶ relay_post table
                                       hourly scan ◀── collectors → relay.collect()
```

Which channels go through here: KOL Watch setting `relay_channels`
(JSON list of profile URLs). Status: `GET /api/relay_status`.

## Log in the spare account (first time, and whenever it logs out)

Never use anyone's real account. Run on a Mac with a screen (e.g. the Air):

```bash
cd ~/Desktop/Claude\ Code/kol_watch/sanmini_relay
python3 fb_relay.py login --copy-to sanmini
```

Chrome opens on facebook.com/login → log in with the spare account (do any
code / checkpoint FB asks). When the login is detected the cookies are saved
and copied to sanmini. Then check from sanmini:

```bash
ssh sanmini 'cd ~/so-fb-relay && /usr/bin/python3 fb_relay.py check'
ssh sanmini 'cd ~/so-fb-relay && /usr/bin/python3 fb_relay.py run --dry'
```

The spare account should **follow** the pages it reads.

## When it breaks

The channel shows under "❌ เช็กไม่ได้" in the hourly report with a reason
starting `relay:`:

| reason | fix |
|---|---|
| ยังไม่มีข้อมูลจากตัวดึงบน sanmini | the spare account has not logged in yet → log in (above) |
| บัญชี FB สำรองหลุดล็อกอิน | log in again (above) |
| ไม่ได้ส่งข้อมูลมา N ชม. | sanmini off or cron stopped → `ssh sanmini 'tail ~/so-fb-relay/run.log'` |
| เพจเปิดไม่ได้แม้ล็อกอินแล้ว | page deleted / hidden / blocked the spare account |
| อ่านโพสต์ไม่ออก | FB changed its page; fix `read_page` / scrapekit `_dig_posts` |

Files on sanmini: `~/so-fb-relay/fb_relay.py`, log `~/so-fb-relay/run.log`,
config `~/.kolwatch/fb_relay.json` (API key), cookies
`~/.kolwatch/fb_relay_cookies.json`, Chrome profile
`~/.scrapekit/fb-relay-profile`. Needs `~/scrapekit` (already there).
