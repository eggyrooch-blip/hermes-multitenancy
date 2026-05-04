# Hermes Multitenancy Installed

Next steps:

```bash
hermes plugins enable multitenancy
hermes plugins list
hermes gateway restart
```

Then configure one shared Feishu app in your default Hermes gateway home, run
the Feishu authorization/UAT flow for each user, and add route rows with:

```bash
python ~/.hermes/plugins/multitenancy/sync.py apply users.json
```

Keep real `FEISHU_APP_SECRET`, user token JSON files, `ou_*` open_ids and chat
IDs out of git. See `README.md` for the full install and App ID reuse model.
