# Security policy

## Supported versions

The latest released version is supported.

## Reporting a vulnerability

Please report security problems privately rather than in a public issue. Use GitHub's
"Report a vulnerability" button on the Security tab of the repository, or email the
maintainer.

You should get an acknowledgement within a week. If the report is accepted, a fix
will be released and the advisory published; if it is declined, you will be told why.

## What favsav does with secrets

- The Jellyfin API key and the SMTP password are read from the environment
  (`FAVSAV_API_KEY`, `FAVSAV_SMTP_PASSWORD`) or from the configuration file.
- `/etc/favsav/favsav.env` is the recommended place for them, owned by `root` and
  readable only by the `favsav` group (`chmod 640`).
- Neither secret is ever written to the log, the report, the manifest or the email.
- favsav never writes to Jellyfin and only ever uses the API key to read.

## Trust boundaries

- **The media library is read-only.** favsav opens source files for reading only.
- **The destination is write-only in practice.** Nothing outside `dest_root` is
  written, and every destination path is checked to be inside it.
- **Pruning is limited to the manifest.** A file is only removed if favsav copied it
  and recorded it.
- **The mail connection is optional and fails soft.** A mail problem is reported and
  does not affect the backup, unless `--strict-notify` is given.
