# TCSpy for RASA36

TCSpy (Telescope Control System with Python) runs the RASA36 telescope: it schedules
the night, observes survey tiles, reacts to Target-of-Opportunity (ToO) requests, and
sends the images to the archive server.

> TCSpy was originally written by Hyeonho Choi for the 7DT telescope array:
> https://github.com/hhchoi1022/tcspy
>
> This repository holds the version adapted to a single RASA36 telescope.
> [RASA36_CHANGES.md](RASA36_CHANGES.md) describes how it differs.

## Contents

1. [How it works](#how-it-works)
2. [Requirements](#requirements)
3. [Installation](#installation)
4. [Configuration](#configuration)
5. [Running](#running)
6. [ToO requests](#too-requests)
7. [Code layout](#code-layout)
8. [Troubleshooting](#troubleshooting)

## How it works

Two programs run side by side on the main control computer.

| Program | Role |
|---|---|
| `applications/appscheduler.py` | Runs the night on the telescope |
| `applications/monitoring.py` | Receives ToO mails, transfers data, and watches GCN notices |

A scheduled night goes through these steps:

| Step | Application |
|---|---|
| Connect, home, slew, and cool the camera | `Startup` |
| Observe survey tiles and ToO targets | `NightObservation` |
| Take flats, darks, and bias frames | `FlatAcquisition`, `DarkAcquisition`, `BiasAcquisition` |
| Park and warm up the camera | `Shutdown` |
| Send the images to the archive server | `DataTransferManager` |

Targets come from a MySQL database. An annual table (`TOS`) holds the survey tiles, and a
nightly table (`Dynamic`) holds tonight's targets and the incoming ToO requests.

| Device | Interface |
|---|---|
| Camera, filter wheel, weather, safety monitor | ASCOM Alpaca |
| Mount | PlaneWave PWI4 HTTP API |
| Focuser | NINA Advanced API |

## Requirements

- Linux, with Python 3.13. Versions from 3.14 on start processes differently on Linux,
  which this code has not been tested with.
- A MySQL server with the `target` database.
- The devices above, reachable over the local network.
- `rsync` and HPN-SSH (`hpnssh`, `hpnscp`) for data transfer.
- A Slack bot that is a member of the alert channels, with the scopes `chat:write`,
  `files:write`, `channels:history`, `groups:history`, `channels:read`, `groups:read`.
- A Google Cloud project with the Gmail and Pub/Sub APIs, for ToO mails.

## Installation

```bash
git clone https://github.com/7DimensionalTelescope/tcspy_RASA36.git tcspy
cd tcspy
conda create -n tcs313 python=3.13
conda activate tcs313
pip install -r requirements.txt
pip install -e .
```

Do not install `mysql-connector-repackaged`. It writes an older version into the same
folder as `mysql-connector-python`.

## Configuration

`mainConfig` merges three sources. Later ones win.

1. `configuration/*.config`: settings shared by everything
2. `configuration/RASA36/*.config`: settings of the telescope (`unitnum=36`)
3. `configuration/keys/local.config`: private values of this computer, **not tracked by git**

The private file holds paths, device and server addresses, the site location, mail
addresses, Slack IDs, Gmail and Google Sheet resources, and the database login.
To create it:

```bash
mkdir -p configuration/keys
cp configuration/local.config.example configuration/keys/local.config
chmod 600 configuration/keys/local.config
```

Then replace every placeholder:

| Placeholder | Write |
|---|---|
| `"CHANGE_ME"` | a text value |
| `["CHANGE_ME@example.com"]` | a list of mail addresses |
| `null` | a number |
| the `RASA36` section | values of that telescope only |

Programs stop at startup with a `RuntimeError` that names the missing or invalid keys.
Never put such values in a tracked file, and keep a copy of `local.config` elsewhere.

### Mail access

1. Enable the Gmail API and Pub/Sub in a Google Cloud project, and download an OAuth
   client file to the path in `GMAIL_CREDENTIALS_PATH`.
2. Create the topic, and let `gmail-api-push@system.gserviceaccount.com` publish to it.
3. Create the subscription. Without `--expiration-period=never`, Google deletes it after
   31 days without use, and ToO mails are then no longer received.

   ```bash
   gcloud pubsub subscriptions create <GMAIL_PUBSUB_SUBSCRIPTION> \
     --topic=<GMAIL_PUBSUB_TOPIC> --ack-deadline=10 \
     --message-retention-duration=10m --expiration-period=never
   ```
4. Sign in once to create the token. This opens a browser.

   ```bash
   python -c "
   from tcspy.configuration import mainConfig
   from tcspy.utils.connector.gmailconnector import GmailPushReceiver
   c = mainConfig().config
   GmailPushReceiver(c['GMAIL_PUSH_TOKEN_PATH'], c['GMAIL_CREDENTIALS_PATH'], interactive=True)"
   ```

## Running

Start `monitoring.py` first and leave it running, then start the scheduler before sunset.

```bash
conda activate tcs313
python -u applications/monitoring.py
python applications/appscheduler.py
```

- `monitoring.py` is ready when it prints `Monitoring started.` and no
  `Pub/Sub pull error` follows.
- Both programs post to Slack. `nightobservation.py` starts observing as soon as it runs.
- Press Ctrl+C once to stop cleanly.
- A running program does not pick up code changes. Restart it after an update.

Manual tools:

| Script | Use |
|---|---|
| `utils/operation_report.py` | Night statistics, for example `--date 20260714` or `--year 2026` |
| `utils/transfer_folders.py` | Send selected image folders again |
| `utils/pointing_test.py` | Pointing test |

## ToO requests

A ToO request is a plain-text mail to the RASA36 alert inbox. Each field is on its own line:

```
Requester: you@example.com
Target: GRB 260101A
RA: 120.5
Dec: -30.25
Exposure: 60
Count: 5
Is_ToO: True
Is_rapid_ToO: False
```

- `Is_ToO` and `Is_rapid_ToO` count as false unless they are written as `True`.
- A rapid ToO interrupts the running survey observation.
- A request expires after 48 hours if it was not observed.

A mail is accepted only if all of these hold:

1. Gmail reports a DMARC pass for the sender's domain, backed by DKIM or SPF.
2. The requester is the sender. Accounts in `ALERTBROKER_RELAYUSERS`, such as the
   [GCN bot](https://github.com/7DimensionalTelescope/gcn_bot), may name somebody else.
3. The requester is in `ALERTBROKER_AUTHUSERS`.

Rejected mails that look like a ToO are reported to Slack. Mails that arrive while
`monitoring.py` is not running are not picked up later, so send them again.

## Code layout

| Folder | Content |
|---|---|
| `applications/` | Scheduler, night observation, calibration, monitoring |
| `action/` | Telescope actions, from single commands (`level1`) to observation modes (`level3`) |
| `devices/` | Camera, mount, focuser, filter wheel, weather, safety monitor |
| `configuration/` | Configuration files and their loader |
| `utils/alertmanager/` | ToO mails, sender check, GCN notices |
| `utils/connector/` | Slack, Gmail, MySQL, Google Sheet |
| `utils/databases/` | Target tables and sky tiles |
| `utils/target/` | Target visibility and scoring |
| `utils/datatransfer.py` | Data transfer |

[CLAUDE.md](CLAUDE.md) holds notes for developers, including the checks to run without
hardware.

## Troubleshooting

| Message | Meaning |
|---|---|
| `RuntimeError: Missing or invalid keys in ...local.config` | Fill in the keys that are named |
| `RuntimeError: Local configuration cannot be loaded` | `local.config` is missing or is not valid JSON |
| `Pub/Sub pull error: 404 Resource not found` | The subscription was deleted. Create it again ([Mail access](#mail-access)) and restart `monitoring.py` |
| `Gmail token refresh failed` | Sign in again ([Mail access](#mail-access)) and restart `monitoring.py` |
| `ToO mail rejected` in Slack | The reason is in the message |
| `WARNING: ... delivery could not be checked` | Slack could not confirm a post, so it may appear twice |
