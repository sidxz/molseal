# Security Policy

## Supported versions

Security fixes go into the latest release only (the newest `v*` tag; see `CHANGELOG.md`).
If you run an older release, upgrade.

## Reporting a vulnerability

Report privately through GitHub's private vulnerability reporting:
<https://github.com/sidxz/molseal/security/advisories/new>

Do not open a public issue or pull request for a security problem.
You will get an acknowledgement within 5 working days, and a fix or a
mitigation plan once the report is confirmed.

## Scope

What Molseal protects, and what it deliberately leaves visible to the database, is set out in
section 4 of the README ("Security model and boundaries"). Reports that a database operator can
see something listed under "Observable information" are expected behaviour, not vulnerabilities.
