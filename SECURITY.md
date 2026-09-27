# Security policy

## Supported versions

Only the latest release is supported; security fixes are not backported to older tags.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: open the "Security" tab on this repository and
select "Report a vulnerability". Please do not open a public issue for a security problem.

## What to expect

This is a single-maintainer project, so responses are best effort, not a guarantee:

- Acknowledgement within 7 days.
- A fix or a considered response within 30 days.

## Exposure model

likearr is built for LAN or VPN use only - it is not designed to be port-forwarded or put behind
a public tunnel. See ["Exposure and reverse proxy"](docs/DEPLOY.md#exposure-and-reverse-proxy) in
the deploy docs for the full model and how to add a reverse proxy safely.
