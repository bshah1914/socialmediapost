# CloudTrio Newsroom: security controls

What the system does today, and what is still open. Written to support a later SOC 2 review; it is not an audit report.
Last updated 2026-11-06 (Google sign-in, off-site backups, uptime monitor and sealed Instagram session added).

## Access
| Control | Status | Where |
| --- | --- | --- |
| Individual accounts, no shared login | Done | Settings, Team and roles |
| Roles: admin, approver, viewer | Done | Enforced for every change in `technews/__init__.py` (`guard`) |
| Passwords stored as salted hashes, at least 8 characters | Done | `technews/users.py` |
| Lock-out after 5 wrong attempts in 10 minutes | Done | `technews/auth.py` |
| 2-step sign-in with an authenticator app (off, optional or required) | Done | Settings, 2-step sign-in |
| Sessions end after 12 hours; a password change, block or role change signs the person out | Done | |
| Single sign-on with Google (only for people who already have an account with that email) | Done | `technews/sso.py`; set up under Settings, Sign in with Google. Microsoft is not built |
| Password complexity and expiry rules | Open | Only a length check |

## Logging and monitoring
| Control | Status | Where |
| --- | --- | --- |
| Audit log of sign-ins, failed sign-ins and every change, with who and when | Done | Settings, Activity log (table `audit`) |
| Alert when no cycle has finished for an hour | Done | `technews/watchdog.py`, sent by email, Telegram or webhook |
| Alert when the site is unreachable from outside | Done | `technews/monitor.py` checks the public address every 5 minutes; a heartbeat ping to an outside service (`HEARTBEAT_URL`) covers the whole server being off |
| Central log storage and retention | Open | Logs stay in the container |

## Data protection
| Control | Status | Where |
| --- | --- | --- |
| HTTPS to the public site | Done | Cloudflare tunnel |
| Passwords and tokens in the settings file stored encrypted; key kept outside the data folder | Done | `technews/secretbox.py`, `SECRETS_KEY` |
| Instagram session file and 2-step keys encrypted | Done | Sealed with the same `SECRETS_KEY`; plain ones are converted at start-up (`technews/instagram.py`, `technews/users.py`) |
| Automatic database backups, each copy opened and checked after it is taken | Done | Daily copy on the server's disk (`technews/backup.py`) |
| Off-site backup, sealed before it leaves the server | Done | To an S3-compatible bucket and/or a folder on another machine; set up under Settings, Copy away from this server. Not active until a bucket or folder is entered |
| PostgreSQL database | Done | Live since 2026-11-05; the old SQLite file is kept untouched as a fallback |

## Change management
| Control | Status | Where |
| --- | --- | --- |
| Source control | Done | Git repository |
| Automated tests before every deploy | Done | `deploy/deploy.sh staging` |
| Staging site that cannot post | Done | `192.168.50.183:7087` |
| Promote the tested image, automatic rollback if unhealthy, manual rollback | Done | `deploy/deploy.sh promote` and `rollback` |
| Code review by a second person | Open | One developer |

## Content safety
| Control | Status | Where |
| --- | --- | --- |
| Human approval before posting | Done | Approvals |
| Blocked words | Done | Settings, Newsroom |
| Numbers in a draft that the source story does not contain are flagged | Done | Approvals card warning |
| Image rights check | Open | Flyers are our own; news cards use text only |

## Availability
| Control | Status | Where |
| --- | --- | --- |
| Second AI provider if NVIDIA fails | Done | Settings, NVIDIA, second AI service |
| Separate scheduler process | Done | `cloudtrio-newsroom-worker` |
| More than one web server | Open | One container on one server |
