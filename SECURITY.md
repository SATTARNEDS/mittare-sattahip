# Production security operations

## Required Render secrets

- `SECRET_KEY`: random value managed by Render; rotating it logs every session out.
- `ADMIN_PASSWORD`: at least 12 characters; use only for controlled bootstrap/reset.
- `LINE_CHANNEL_ACCESS_TOKEN` and `LINE_CHANNEL_SECRET`: keep only in Render secrets.
- `BACKUP_ENCRYPTION_KEY`: URL-safe base64 encoding of exactly 32 random bytes.

Generate the backup key once and store a recovery copy in a password manager:

```powershell
python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

Never commit the generated value. If this key is lost, encrypted backups cannot be recovered.

## Backup and recovery

Full backups download as `.mtbackup` and use authenticated AES-256-GCM encryption. Restore locally:

```powershell
$env:BACKUP_ENCRYPTION_KEY="value-from-password-manager"
python decrypt_backup.py mittare-full-backup.mtbackup restored-backup.zip
```

Test recovery at least quarterly. Keep at least one recovery copy outside Render.

## Monitoring and response

- Review admin audit logs weekly and after suspicious login activity.
- Render alerts should notify on deploy failure, repeated HTTP 5xx, high latency, and disk usage.
- Rotate admin and LINE credentials immediately after suspected exposure.
- Set `RESET_ADMIN_PASSWORD=1` only for one controlled deploy, then return it to `0`.
- Do not publish database files, backups, `.env` files, tokens, or customer attachments.

## Automated controls

GitHub Actions runs unit tests and `pip-audit` on pushes, pull requests, and weekly. Dependabot proposes Python and GitHub Actions security updates.

