# Hermes Multitenancy Installed

Next steps:

```bash
hermes plugins enable multitenancy
hermes plugins list
# Install the multitenancy-owned gateway systemd drop-ins (idempotent — re-run on
# every provision/deploy and after any host rebuild; they live in ~/.config, NOT
# in git). Currently pins the fast meegle binary for the feishu-project reader.
deploy/install-gateway-dropins.sh
hermes gateway restart
```

Then configure one shared Feishu app in your default Hermes gateway home, run
the Feishu authorization/UAT flow for each user, and add route rows with:

```bash
python ~/.hermes/plugins/multitenancy/sync.py apply users.json
```

Keep real `FEISHU_APP_SECRET`, user token JSON files, `ou_*` open_ids and chat
IDs out of git. See `README.md` for the full install and App ID reuse model.
