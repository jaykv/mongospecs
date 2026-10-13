# Security

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private reporting instead:
<https://github.com/jaykv/mongospecs/security/advisories/new>.

Say what you found, how to reproduce it, and which package and version it affects.

## Supported versions

Fixes are made on the latest release of each package. `mongospecs` 0.1.x is no longer developed.

## What the library is meant to guarantee

These are the properties to hold a report against:

- A plain mapping used as a filter cannot carry MongoDB operators unless the application wraps it
  in `trusted()`.
- In `mongospecs-web`, nothing a client sends reaches MongoDB as a raw filter or update: fields
  and operators are allowlisted by `Policy`, and values are cast to the field's type.
- A field marked `Policy(private=True)` is never part of a response or of the OpenAPI document.
- A resource's `scope` is part of the database query of every list, get, patch, put and delete.
- Request bodies are never read past `Api(max_body=...)`.
